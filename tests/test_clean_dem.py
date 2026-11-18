"""The DEM cleaner outlines the channels with the initial flood map."""
import pytest

from nencarta.core.configs import NencartaConfig
from nencarta.tasks import clean_dem
from nencarta.workspace import Workspace


@pytest.mark.parametrize("use_specified_depth", [True, False])
def test_the_cleaner_reads_the_initial_flood_map(workspace_tmp, monkeypatch, use_specified_depth):
    dem = workspace_tmp / "dem.tif"
    workspace = Workspace(NencartaConfig({
        "name": "example", "dem": str(dem), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "clean_dem": True,
        "use_specified_depth_for_bathy_mask": use_specified_depth, "specify_depths_for_bathy_mask": [0.3, 1.0],
    }), dem)
    workspace.Curve_File_Initial.parent.mkdir(parents=True)
    workspace.Curve_File_Initial.touch()
    mapped, cleaned = [], []

    def flood_streams_to_depth(workspace, dem, depth, flood_map, config):
        mapped.append((dem, depth, config))
        flood_map.parent.mkdir(parents=True, exist_ok=True)
        flood_map.touch()
        return flood_map

    monkeypatch.setattr(clean_dem, "flood_streams_to_depth", flood_streams_to_depth)
    monkeypatch.setattr(clean_dem, "_make_flood_flow_file_from_base_max_file", lambda **kwargs: None)
    monkeypatch.setattr(clean_dem.dem_cleaner, "DEM_Cleaner_Program", lambda *args: cleaned.append(args))

    clean_dem.make_clean_dem(workspace)

    flood_map = cleaned[0][8]
    if use_specified_depth:
        # The first depth, on the DEM the cleaner's ARC run had, in Curve2Flood's own input file
        assert mapped == [(workspace.assigned_dem, 0.3, workspace.Mapper_FileName_InitialFlood)]
        assert flood_map == workspace.FloodMapFile_Initial
    else:
        assert mapped == []
        assert flood_map == workspace.bathy_water_mask
