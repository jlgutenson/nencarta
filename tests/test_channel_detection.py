"""
Where a DEM shows its rivers' channels (nencarta.tasks.channel_detection), on DEMs that flatten their water to any
height, that store whole metres, or that leave the water as rough as the land.
"""
import numpy as np
import pytest

from nencarta.tasks.channel_detection import ChannelSettings, flat_cells, river_channel

CELL = 30.0


def detect(dem, streams, area_km2, **kwargs):
    return river_channel(dem, np.isfinite(dem), streams, area_km2, CELL, CELL, **kwargs)


def sloping_land(rows=41, cols=60):
    """Land with no two cells alike: no flats anywhere."""
    r, c = np.mgrid[0:rows, 0:cols]
    return 100.0 + 0.0137 * r + 0.0171 * c


def test_water_flattened_to_any_height_is_flat_water():
    # Not a whole half metre, as USGS 3DEP flattens its water
    dem = sloping_land() + 2.0
    dem[18:23, :] = 101.137
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 5
    found = detect(dem, streams, {5: 100.0})
    assert found.flat_water[18:23].all()
    assert not found.flat_water[:17].any() and not found.flat_water[24:].any()
    assert (found.channel == found.flat_water).all()


def test_a_flat_pond_touching_no_stream_is_left_out_unless_asked():
    # 300 to 420 m from a river expected 246 m wide, near enough for its flat water
    dem = sloping_land() + 2.0
    dem[18:23, :] = 101.137
    dem[30:35, 20:31] = 101.5
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 5
    assert not detect(dem, streams, {5: 50000.0}).flat_water[30:35, 20:31].any()
    assert detect(dem, streams, {5: 50000.0}, connected=False).flat_water[30:35, 20:31].all()


@pytest.mark.parametrize("step", [1.0, 0.1])
def test_on_a_coarsely_rounded_dem_no_flat_is_water_and_the_channel_is_the_low_ground_by_the_stream(step):
    # Whole metres (SRTM) or tenths (GEDTM) make flats of the floodplain and of a plateau up the valley side alike:
    # flatness says nothing
    r, c = np.mgrid[0:61, 0:40]
    off = np.abs(r - 30)
    dem = np.floor((100 + 0.25 * np.minimum(off, 12) + 1.5 * np.maximum(off - 12, 0)) / step) * step
    dem[off >= 18] = 110.0
    dem = np.round(dem, 1)
    streams = np.zeros(dem.shape, np.int64)
    streams[30, :] = 3
    found = detect(dem, streams, {3: 3000.0})  # 60 m wide: low ground within 90 m, 3 cells
    assert flat_cells(dem, np.ones(dem.shape, bool))[off >= 18].all()
    assert not found.flat_water.any()
    assert found.channel[off <= 3].all() and not found.channel[off >= 4].any()


def test_on_a_dem_rounded_to_centimetres_flats_near_the_rivers_level_are_water():
    # As FABDEM: its water at whole half metres, and a flat a third of a metre above it, up to 210 m from the stream of
    # a river expected 246 m wide
    dem = np.round(sloping_land() * 100) / 100 + 2.0
    dem[18:23, :] = 101.0
    dem[23:28, :] = 101.33
    dem[30:35, :] = 104.0  # flat, but 3 m up
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 5
    found = detect(dem, streams, {5: 50000.0})
    assert found.flat_water[18:28].all()
    assert not found.flat_water[30:35].any()


@pytest.mark.parametrize("area_km2", [50.0, 20000.0, 600000.0])
def test_flat_water_reaches_as_far_from_the_stream_as_the_rivers_width_allows(area_km2):
    # A level marsh, as FABDEM's Everglades: flat for 1.5 km either side of the stream, at the river's level. Flat water
    # reaches flat_widths of the river's expected width from the stream (a small river's at least flat_min_m, a big
    # one's no farther than the corridor), and without flat_widths across the whole corridor
    s = ChannelSettings()
    dem = np.full((101, 30), 101.37)
    streams = np.zeros(dem.shape, np.int64)
    streams[50, :] = 4
    off = np.abs(np.arange(101) - 50)[:, None] * CELL * np.ones((1, 30))
    reach = min(max(s.flat_widths * s.width_per_sqrt_km2 * np.sqrt(area_km2), s.flat_min_m), s.corridor_m)
    for connected in (True, False):
        found = detect(dem, streams, {4: area_km2}, connected=connected, channel=False).flat_water
        assert found[off <= reach].all() and not found[off > reach].any()
        assert (detect(dem, streams, {4: area_km2}, connected=connected).flat_water == found).all()
    unbounded = detect(dem, streams, {4: area_km2}, settings=ChannelSettings(flat_widths=None)).flat_water
    assert unbounded.all()


def test_low_ground_by_a_stream_is_channel_only_within_one_and_a_half_widths_of_it():
    # Rough water three cells wide, and a floodplain 0.6 m above it: as low as the river within 1.5 widths of the
    # stream (60 m wide here, at 3000 km2), and not channel beyond
    dem = sloping_land()
    dem[(np.abs(np.arange(41) - 20) > 1)] += 0.6
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 9
    found = detect(dem, streams, {9: 3000.0})
    assert not found.flat_water.any()
    assert found.channel[17:24, 5:-5].all()
    assert not found.channel[:16].any() and not found.channel[25:].any()


def test_on_a_dem_that_flattens_a_river_the_low_ground_by_a_narrow_one_isnt_channel():
    # As FABDEM along the Jordan: a big river's water flattened, and a river expected 60 m wide (3000 km2) that is
    # narrower than a cell, left unflattened, beside a floodplain 0.6 m above it, which is no channel
    dem = sloping_land(rows=61)
    dem[(np.abs(np.arange(61) - 20) > 1)] += 0.6
    dem[50:55, :] = 99.0
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 9
    streams[52, :] = 4
    found = detect(dem, streams, {9: 3000.0, 4: 50000.0})
    assert found.flat_water[50:55].all()
    assert (found.channel == found.flat_water).all()
    assert not found.channel[:45].any()


@pytest.mark.parametrize("height, flattens_rivers, low_ground", [
    (99.0, None, False),    # FABDEM's water, at whole half metres: it flattens every river wide enough to show
    (99.137, None, True),   # USGS 3DEP's, at any height: it flattens only some of its water
    (99.137, True, False),  # told the DEM flattens every river
    (99.0, False, True),    # told it doesn't
])
def test_the_low_ground_by_an_unflattened_river_is_channel_unless_the_dem_flattens_every_river(height, flattens_rivers,
                                                                                              low_ground):
    # A big river's water flattened, and a river expected 60 m wide (3000 km2) left as rough as the land, beside a
    # floodplain 0.6 m above it: as low as the river within 1.5 widths (3 cells) of its stream
    dem = sloping_land(rows=61)
    dem[(np.abs(np.arange(61) - 20) > 1)] += 0.6
    dem[50:55, :] = height
    streams = np.zeros(dem.shape, np.int64)
    streams[20, :] = 9
    streams[52, :] = 4
    found = detect(dem, streams, {9: 3000.0, 4: 50000.0}, settings=ChannelSettings(flattens_rivers=flattens_rivers))
    assert found.flat_water[50:55].all() and not found.flat_water[:45].any()
    if low_ground:
        assert found.channel[17:24, 5:-5].all() and not found.channel[:16].any() and not found.channel[25:45].any()
        assert found.channel[50:55].all()
    else:
        assert (found.channel == found.flat_water).all()


def test_no_streams_no_channel():
    dem = sloping_land()
    found = detect(dem, np.zeros(dem.shape, np.int64), {})
    assert not found.channel.any() and not found.flat_water.any()


@pytest.mark.parametrize("dtype, double", [(np.float32, False), (np.float64, False), (np.float32, True)])
def test_the_rivers_level_is_scipys_minimum_and_gaussian_filters_to_the_last_bit(dtype, double):
    # Worked out at the stream cells alone, but as a minimum filter and gaussian_filter over the whole grid would: with
    # reflected edges, on grids smaller than the kernel, and rounded to float32 between passes on a float32 DEM, unless
    # told to work in double precision, as on a float64 copy of it
    from scipy.ndimage import gaussian_filter, minimum_filter

    from nencarta.tasks.channel_detection import ChannelSettings, _stream_levels

    rng = np.random.default_rng(3)
    # The last two have cells far enough from the edges for the kernel not to reflect, where only the pairs of cells
    # with a stream cell are added
    for rows, cols, smooth_m in ((40, 57, 150.0), (7, 5, 600.0), (3, 90, 40.0), (130, 150, 150.0), (90, 120, 60.0)):
        dem = (np.round(rng.normal(100, 3, (rows, cols)) * 2) / 2).astype(dtype)
        valid = rng.random((rows, cols)) > 0.1
        on_stream = valid & (rng.random((rows, cols)) > 0.7)
        s = ChannelSettings(snap_m=45.0, smooth_m=smooth_m)
        k = max(int(round(s.snap_m / CELL)), 1)
        lowest = minimum_filter(np.where(valid, dem.astype(np.float64) if double else dem, np.inf), size=2 * k + 1)
        have = on_stream & np.isfinite(lowest)
        sigma = max(s.smooth_m / CELL, 1.0)
        level = gaussian_filter(np.where(have, lowest, 0.0), sigma)[have] \
            / np.maximum(gaussian_filter(have.astype(np.float64), sigma)[have], 1e-12)

        r, c, found = _stream_levels(dem, valid, on_stream, CELL, s, double)
        assert np.array_equal(np.flatnonzero(have), r * cols + c)
        assert np.array_equal(found, level)


def test_the_nearest_stream_cell_and_its_distance_are_scipys_to_the_last_bit():
    # The feature transform of scipy's distance_transform_edt, ties and all, on square and oblong cells, and the
    # distances it works out from it
    from scipy.ndimage import distance_transform_edt

    from nencarta.tasks.channel_detection import nearest_cell, within_distance

    rng = np.random.default_rng(4)
    for rows, cols, share, sampling in ((50, 70, 0.01, (1.0, 1.0)), (61, 43, 0.1, (CELL, CELL)),
                                        (33, 80, 0.002, (30.87, 23.6)), (1, 40, 0.1, (9.0, 7.0)),
                                        (40, 1, 0.1, (9.0, 7.0))):
        cells = rng.random((rows, cols)) < share
        cells[rng.integers(rows), rng.integers(cols)] = True
        indices = distance_transform_edt(~cells, sampling=sampling, return_distances=False, return_indices=True)
        assert np.array_equal(nearest_cell(cells, sampling), indices[0] * cols + indices[1])
        distance = distance_transform_edt(~cells, sampling=sampling)
        for radius in (0.0, 45.0, float(np.median(distance)), float(distance.max())):
            assert np.array_equal(within_distance(cells, sampling, radius), distance <= radius)


def test_the_patches_touching_a_stream_are_scipys_labels_beside_one():
    from scipy.ndimage import binary_dilation, label

    from nencarta.tasks.channel_detection import touching

    rng = np.random.default_rng(5)
    mask = rng.random((80, 90)) < 0.45
    streams = np.where(rng.random(mask.shape) < 0.01, 7, 0)
    labels, _ = label(mask, np.ones((3, 3), int))
    near = binary_dilation(streams > 0, np.ones((3, 3), bool))
    expected = np.isin(labels, labels[near & (labels > 0)]) & mask
    assert 0 < expected.sum() < mask.sum()
    assert np.array_equal(touching(mask, streams), expected)
