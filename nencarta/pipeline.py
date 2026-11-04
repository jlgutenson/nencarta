from pipefunc import PipeFunc, Pipeline

from nencarta import tasks


def build_pipeline(profile: bool, outlet_exchange: str = "off") -> Pipeline:
    """
    The steps every workspace runs through: prepare the inputs, run ARC, add the bathymetry to the DEM, build the
    FLDPLN library, map the floods, crop buffered maps, and write the FIST inputs and consequences. With
    outlet_exchange pass1, only prepare the inputs and write the water the neighbouring DEMs need (see
    tasks/outlet_exchange.py).
    """
    if outlet_exchange == "pass1":
        return Pipeline([
            PipeFunc(
                tasks.prepare_inputs_for_dem,
                "model_configs",
                mapspec="workspace[i] -> model_configs[i]",
            ),
            PipeFunc(
                tasks.run_boundary_pass,
                "boundary_wse",
                renames={'model_config': 'model_configs'},
                mapspec="model_configs[i], workspace[i] -> boundary_wse[i]",
            ),
        ], profile=profile)

    funcs = [
        PipeFunc(
            tasks.prepare_inputs_for_dem,
            "model_configs",
            mapspec="workspace[i] -> model_configs[i]",
        ),
        PipeFunc(
            tasks.run_arc_bathymetry,
            "mapper_arc_config",
            renames={'model_config': 'model_configs'},
            mapspec="model_configs[i], workspace[i] -> mapper_arc_config[i]",
        ),
        PipeFunc(
            tasks.run_mapper_bathymetry,
            "mapper_bathy_config",
            renames={'model_config': 'mapper_arc_config'},
            mapspec="mapper_arc_config[i], workspace[i] -> mapper_bathy_config[i]",
        ),
        PipeFunc(
            tasks.run_fldpln_library,
            "mapper_ready_config",
            renames={'model_config': 'mapper_bathy_config'},
            mapspec="mapper_bathy_config[i], workspace[i] -> mapper_ready_config[i]",
        ),
        PipeFunc(
            tasks.run_mapper_floodmaps,
            "mapper_outputs",
            renames={'model_config': 'mapper_ready_config'},
            mapspec="mapper_ready_config[i], workspace[i] -> mapper_outputs[i]",
        ),
        PipeFunc(
            tasks.unbuffer_maps,
            "unbuffered_floodmaps",
            renames={'floodmapper_output': 'mapper_outputs'},
            mapspec="mapper_outputs[i], workspace[i] -> unbuffered_floodmaps[i]",
        ),
        PipeFunc(
            tasks.run_fist,
            "fist_outputs",
            renames={'model_config': 'model_configs', 'flag': 'mapper_arc_config'}, # Flag to indicate we need to run ARC first before FIST
            mapspec="model_configs[i], mapper_arc_config[i] -> fist_outputs[i]",
        ),
        PipeFunc(
            tasks.run_consequences,
            "consequences_outputs",
            renames={'floodmapper_bulk_output': 'mapper_outputs'},
            mapspec="mapper_outputs[i], workspace[i] -> consequences_outputs[i]",
        )
    ]

    return Pipeline(
        funcs,
        profile=profile
    )
