"""
Smoothing the stair steps out of a burned DEM's channels (nencarta.tasks.move_streams.smooth_burned_dem).

FABDEM flattens a river into terraces at whole multiples of 0.5 m, and the burn lowers stream lines in 0.5 m steps.
ARC measures stream slopes on these cells, so each terrace has to become a ramp: the mean of the two terraces where
they meet, its own elevation in its middle. Every test here is a river flowing east down a small grid.
"""
import numpy as np

from nencarta.tasks.move_streams import smooth_burned_dem

ROWS, COLS = 20, 60
BANK = 105.3  # land, off any 0.5 m step


def terraced_river(levels=(101.0, 100.5, 100.0), top=5, bottom=15):
    """A river in rows ``top`` to ``bottom`` (exclusive), in equal-length terraces at ``levels`` from west to east."""
    dem = np.full((ROWS, COLS), BANK, np.float32)
    mask = np.zeros((ROWS, COLS), bool)
    mask[top:bottom] = True
    length = COLS // len(levels)
    for i, level in enumerate(levels):
        dem[top:bottom, i * length:(i + 1) * length] = level
    return dem, mask


def test_terraces_meet_at_their_mean_and_keep_their_level_in_the_middle():
    dem, mask = terraced_river()
    out = smooth_burned_dem(dem.copy(), mask)
    edge = out[5]  # next to the bank, where the middle of the channel isn't lowered

    assert abs(edge[19] - 100.75) < 0.05 and abs(edge[20] - 100.75) < 0.05
    assert abs(edge[39] - 100.25) < 0.05 and abs(edge[40] - 100.25) < 0.05
    assert edge[29] == 100.5 or abs(edge[29] - 100.5) < 0.03
    # The ends meet only one other terrace, and keep their level where they are farthest from it
    assert abs(edge[0] - 101.0) < 0.03 and abs(edge[-1] - 100.0) < 0.03
    assert np.all(np.diff(edge) <= 1e-6)
    assert np.all(out[~mask] == BANK)


def test_the_middle_of_the_channel_is_lowest_by_at_most_a_tenth_of_a_metre():
    dem, mask = terraced_river()
    out = smooth_burned_dem(dem.copy(), mask)
    section = out[5:15, 29]

    assert section.argmin() in (4, 5)
    assert 0.08 < section.max() - section.min() <= 0.1 + 1e-4


def test_the_burns_steps_and_levees_in_the_water_are_smoothed_away():
    original, mask = terraced_river()
    burned = original.copy()
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 1
    burned[9] += 0.5  # the cells the burn raises beside the line it burns through flat water
    burned[11] += 0.5
    burned[10, 25:] = np.minimum(burned[10, 25:], 100.0)  # cut deeper where the line was already lower upstream

    out = smooth_burned_dem(burned.copy(), mask, streams, original)

    # No ridge beside the line: across the river the water stays within ARC's 0.1 m of flat
    water = np.delete(out[5:15], 5, axis=0)
    assert np.all(water.max(axis=0) - water.min(axis=0) <= 0.1 + 1e-4)
    # The line is the lowest cell across the river, and keeps falling downstream without a stair step
    assert np.all(out[10] <= out[5:15].min(axis=0) + 1e-6)
    assert np.all(np.diff(out[10]) <= 1e-6)
    assert np.max(np.abs(np.diff(out[10, :25]))) < 0.1


def test_a_burned_line_never_rises_along_its_path_where_the_water_ramps_up():
    # FABDEM's water isn't always lower downstream. The burn keeps the line level through a higher terrace, but the
    # water's ramp up toward it lifted the line, which is kept no higher than the water's middle, into a pool there
    original, mask = terraced_river(levels=(100.5, 101.0, 100.5))
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 1
    burned = original.copy()
    burned[10] = 100.5
    path = (np.empty(0, np.int64), np.arange(10 * COLS, 11 * COLS, dtype=np.int64))  # west to east, as burned

    assert np.any(np.diff(smooth_burned_dem(burned.copy(), mask, streams, original)[10]) > 0)
    out = smooth_burned_dem(burned.copy(), mask, streams, original, paths=[path])

    assert np.all(np.diff(out[10]) <= 0)
    assert np.all(out[10] <= out[5:15].min(axis=0) + 1e-6)


def test_a_burned_line_on_land_is_smoothed_along_itself_and_its_banks_kept_above_it():
    original = np.full((ROWS, COLS), BANK, np.float32)
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 7
    original[10] = np.linspace(101.9, 100.2, COLS)
    original[9, 30] = 100.4  # a bank lower than the line will be once smoothed
    burned = original.copy()
    burned[10] = np.repeat([101.5, 101.0, 100.5, 100.0], 15)
    mask = streams > 0

    out = smooth_burned_dem(burned.copy(), mask, streams, original)

    line = out[10]
    assert np.all(np.diff(line) <= 1e-6)
    assert np.max(np.abs(np.diff(line))) < 0.1
    assert abs(line[22] - 101.0) < 0.03
    assert out[9, 30] > line[29:32].max()


def test_the_banks_of_a_line_falling_down_a_slope_arent_raised_above_its_cells_upstream():
    # Ground falling 3 m a cell east, level across: the ground beside each of the line's cells is lower than the
    # line's cell above it, but its water falls more steeply onto the line's next cell. Banks used to be raised above
    # every line cell beside them, a wall 3.5 m high along both sides of the line
    original = np.tile(200.0 - 3.0 * np.arange(COLS), (ROWS, 1)).astype(np.float32)
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 1

    out = smooth_burned_dem(original.copy(), streams > 0, streams, original)

    # Beside the line's last cell nothing is lower, and the banks level with it are raised, as beside a level line
    assert np.array_equal(out[:, :-1], original[:, :-1])
    assert np.all(out[[9, 11], -1] == original[10, -1] + 0.5)


def burned_line(levels, length=20):
    """A line burned west to east along row 10 in terraces ``length`` cells long at ``levels``, over land."""
    original = np.full((ROWS, COLS), BANK, np.float32)
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 1
    original[10] = np.linspace(103.9, 100.2, COLS)  # the ground the burn cut into, off any 0.5 m step
    burned = original.copy()
    burned[10] = np.repeat(np.asarray(levels, np.float32), length)
    path = (np.empty(0, np.int64), np.arange(10 * COLS, 11 * COLS, dtype=np.int64))  # west to east, as burned
    return burned, original, streams, path


def test_a_burned_terrace_ending_in_a_2_m_drop_falls_to_it():
    # The burn keeps a line no higher than where it has been upstream, so it can run level for a whole reach and
    # then drop metres where the ground does. Ramped only toward steps up to 0.5 m, such a terrace stayed level
    burned, original, streams, path = burned_line((102.0, 101.5, 99.5))

    level = smooth_burned_dem(burned.copy(), streams > 0, streams, original)[10]
    assert np.all(level[40:] == 99.5)

    line = smooth_burned_dem(burned.copy(), streams > 0, streams, original, paths=[path])[10]
    # Never level, never rising, and the 2 m drop split between the terraces either side of it
    assert np.all(np.diff(line) < 0)
    assert abs(line[39] - 100.5) < 0.06 and abs(line[40] - 100.5) < 0.03
    assert line[39] - line[40] < 0.1
    # The terraces keep their own level in their middles, and the last at its end, farthest from the drop
    assert abs(line[29] - 101.5) < 0.02 and abs(line[30] - 101.5) < 0.06
    assert line[0] == 102.0 and line[-1] == 99.5


def test_a_tributary_ending_on_a_river_doesnt_lift_the_river_toward_it():
    burned, original, streams, river = burned_line((101.0, 101.0, 100.5))
    streams[:10, 20] = 2
    burned[:10, 20] = 103.0  # a tributary hanging 2 m above the river, burned first, ending on the river's cell
    tributary = (np.empty(0, np.int64), np.append(np.arange(10) * COLS + 20, 10 * COLS + 20).astype(np.int64))

    out = smooth_burned_dem(burned.copy(), streams > 0, streams, original, paths=[tributary, river])

    assert np.all(np.diff(out[10]) < 0)
    assert np.all(np.diff(out[:11, 20]) < 0)


def test_steps_bigger_than_half_a_metre_are_left_alone():
    dem, mask = terraced_river(levels=(101.0, 100.0))
    out = smooth_burned_dem(dem.copy(), mask)

    assert np.all(out[5] == dem[5])


def test_a_dem_without_terraces_is_unchanged():
    rng = np.random.default_rng(0)
    dem = (100 + rng.random((ROWS, COLS)) * 0.49).astype(np.float32)
    dem[dem % 0.5 == 0] += 0.01
    mask = np.zeros((ROWS, COLS), bool)
    mask[5:15] = True
    streams = np.zeros((ROWS, COLS), np.int32)
    streams[10] = 1

    out = smooth_burned_dem(dem.copy(), mask, streams)

    assert np.array_equal(out, dem)
