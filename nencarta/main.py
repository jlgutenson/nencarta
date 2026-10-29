# build-in imports
import argparse
import json
import sys
from pathlib import Path

from nencarta.core.configs import NencartaConfig
from nencarta.core.defaults import CONFIG_OPTIONS, CONFIG_SECTIONS
from nencarta.download_forecasts import (
    Download_USGS_DEM_Data_Using_WarningFlag_Data,
)

# local imports
from nencarta.logger import LOG
from nencarta.pipeline import build_pipeline
from nencarta.workspace import Workspace, check_workspaces_do_not_share_folders


def _resolve_workspace_dems(configs: NencartaConfig) -> list:
    """The DEMs to map: those in dem_dir (or downloaded into it), else dem."""
    if configs.dem_dir:
        if configs.use_warning_flags_to_download_dem:
            if configs.forensic_forecast_date:
                return Download_USGS_DEM_Data_Using_WarningFlag_Data(
                    configs.geoglows_vpu,
                    configs.dem_dir,
                    configs.forensic_forecast_date,
                )
            return []
        
        return sorted(Path(configs.dem_dir).glob(configs.dem_filter))

    if configs.dem:
        return [configs.dem]

    return []

def _setup_workspace(input_dict: dict) -> list[Workspace]:
    """One workspace per DEM of a watershed, or one for bbox with source_dems."""
    configs = NencartaConfig(input_dict)
    dem_list = _resolve_workspace_dems(configs)

    if not dem_list:
        if configs.source_dems and configs.bbox:
            dem_list = [None]
        else:
            LOG.warning("No DEMs found in the specified folder and no source DEMs provided; cannot run pipeline.")
            return []

    workspaces = [Workspace(configs, dem) for dem in dem_list]
    return workspaces

def process_watershed(input_dict: dict):
    """
    Map one watershed: each of its DEMs, from the inputs to the flood maps.

    The ``cli`` command runs this. Files that already exist are reused unless ``overwrite``, so running it again
    only makes what is missing. A DEM that fails doesn't stop the others: their errors are printed at the end.

    Parameters
    ----------
    input_dict : dict
        The watershed's settings, by their keys in the configuration reference (`CONFIG_OPTIONS`). Paths may be
        strings or ``Path`` objects, and a key left out or set to None takes its default.

    Raises
    ------
    KeyError
        When ``name``, ``output_dir``, a DEM or the streams are missing.
    ValueError
        When a setting is invalid, or two settings conflict.
    """
    workspaces = _setup_workspace(input_dict)
    if not workspaces:
        return
    run_pipeline(workspaces)
    return

def process_many_watersheds(input_dicts: list[dict]):
    """
    Map several watersheds together, as `process_watershed` maps one.

    Their DEMs run as one batch: the first watershed's ``parallel`` and ``num_workers`` apply to all of them.

    Parameters
    ----------
    input_dicts : list of dict
        Each watershed's settings, as `process_watershed` takes them.

    Raises
    ------
    KeyError
        When a watershed lacks ``name``, ``output_dir``, a DEM or the streams.
    ValueError
        When a setting is invalid, two settings conflict, or the watersheds' ``outlet_exchange`` differ.
    """
    workspaces = []
    for input_dict in input_dicts:
        workspaces.extend(_setup_workspace(input_dict))

    if not workspaces:
        return
    run_pipeline(workspaces)

def _print_unencodable(obj) -> None:
    """
    Print text the console may not be able to encode, such as the emoji in the error snapshot, replacing what it
    can't. Without this, a cp1252 Windows console raises UnicodeEncodeError and hides the pipeline's real error.
    """
    text = str(obj)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _make_console_lenient() -> None:
    """
    Make stdout and stderr replace the characters they can't encode instead of raising. The progress display
    prints emoji, which a cp1252 Windows console can't encode, and would otherwise crash a finished run.
    """
    # Only these actually substitute an unencodable character on the way out. "surrogateescape"
    # is the Windows default and looks lenient, but it only round-trips lone surrogates -- an
    # emoji still raises through it.
    substituting = {"replace", "ignore", "xmlcharrefreplace", "backslashreplace", "namereplace"}
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if (getattr(stream, "errors", None) or "") in substituting:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def run_pipeline(workspaces: list[Workspace], executor=None):
    """
    Run workspaces through the pipeline (see pipeline.py), in parallel if the first one's config says so. A
    failing workspace doesn't stop the others; the errors are printed at the end.
    """
    _make_console_lenient()
    check_workspaces_do_not_share_folders(workspaces)
    profile = workspaces[0].configs.profile and not workspaces[0].configs.parallel
    parallel = workspaces[0].configs.parallel
    num_workers = workspaces[0].configs.num_workers
    passes = {workspace.configs.outlet_exchange for workspace in workspaces}
    if len(passes) > 1:
        raise ValueError(f"The watersheds run together must share outlet_exchange, not {sorted(passes)}.")
    
    pipeline = build_pipeline(profile, passes.pop())
    should_shutdown_executor = False

    if not parallel and num_workers is not None:
        LOG.warning(f"Ignoring num_workers={num_workers} because parallel is False.")
    elif executor is not None and num_workers is not None:
        LOG.warning(f"Ignoring num_workers={num_workers} because an executor was provided.")
    elif executor is None and num_workers is not None:
        from concurrent.futures import ProcessPoolExecutor
        executor = ProcessPoolExecutor(max_workers=num_workers)
        should_shutdown_executor = True
        LOG.info(f"Using ProcessPoolExecutor with {num_workers} workers.")

    try:
        pipeline.map(
            {'workspace': workspaces},
            parallel=parallel,
            scheduling_strategy='eager',
            show_progress="rich",
            executor=executor,
            error_handling='continue'
            )

        if profile:
            pipeline.print_profiling_stats()
    except Exception:
        if not pipeline.error_snapshot:
            raise
    finally:
        if should_shutdown_executor:
            executor.shutdown()
        if pipeline.error_snapshot:
            _print_unencodable(pipeline.error_snapshot.traceback)
            _print_unencodable(pipeline.error_snapshot)

    LOG.info("Finished processing")


def _apply_run_overrides(watersheds, parallel=None, num_workers=None):
    configured = []
    for watershed in watersheds:
        item = dict(watershed)
        if parallel is not None:
            item["parallel"] = bool(parallel)
        if num_workers is not None:
            item["num_workers"] = int(num_workers)
        configured.append(item)
    return configured


def process_json_input(json_file, parallel=None, num_workers=None):
    """
    Map the watersheds of a JSON file together, as the ``json`` command does.

    Parameters
    ----------
    json_file : str or os.PathLike
        A JSON file of the form ``{"watersheds": [{...}, ...]}``, each watershed's settings as `process_watershed`
        takes them. A top-level ``"parallel"`` or ``"num_workers"`` applies to every watershed.
    parallel : bool, optional
        Whether to map the DEMs in parallel, overriding the file's ``parallel``.
    num_workers : int, optional
        Number of processes when parallel, overriding the file's ``num_workers``.

    Raises
    ------
    KeyError
        When a watershed lacks ``name``, ``output_dir``, a DEM or the streams.
    ValueError
        When a setting is invalid, two settings conflict, or the watersheds' ``outlet_exchange`` differ.
    """
    with open(json_file, 'r') as file:
        LOG.info(f"Reading input file: {json_file}")
        data = json.load(file)

    watersheds = data.get("watersheds", [])
    if not watersheds:
        LOG.warning("No watersheds found.")
        return

    parallel = data.get("parallel") if parallel is None else parallel
    num_workers = data.get("num_workers") if num_workers is None else num_workers
    watersheds = _apply_run_overrides(watersheds, parallel, num_workers)
    process_many_watersheds(watersheds)


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object, e.g. '{\"key\": 1}'")
    
    return value


def _cli_help(option) -> str:
    text = option.help
    if option.choices and len(option.choices) <= 6:
        text += f" One of: {', '.join(option.choices)}."
    if option.default not in (None, "", [], {}) and option.kind != "dict":
        default = ", ".join(map(str, option.default)) if isinstance(option.default, list) else option.default
        text += f" Default: {default}."
    # argparse formats help with %, so a literal percent sign has to be doubled
    return text.replace("%", "%%")


def add_config_options(parser: argparse.ArgumentParser) -> None:
    """
    Add a --<key> option for every config key. Switches also get --no-<key>, lists take several values, and
    dictionaries take JSON. Options left out keep their default.
    """
    for section, options in CONFIG_SECTIONS.items():
        group = parser.add_argument_group(section)
        for key, option in options.items():
            kwargs = {"dest": key, "default": None, "help": _cli_help(option)}
            if key in ("name", "output_dir"):
                kwargs["required"] = True
            if option.kind == "bool":
                kwargs["action"] = argparse.BooleanOptionalAction
            elif option.kind == "dict":
                kwargs["type"] = _json_object
                kwargs["metavar"] = "JSON"
            else:
                kind = option.kind.removesuffix("_list")
                kwargs["type"] = {"int": int, "float": float}.get(kind, str)
                kwargs["metavar"] = {"path": "PATH", "int": "INT", "float": "NUMBER"}.get(kind, "TEXT")
                if option.kind.endswith("_list"):
                    kwargs["nargs"] = "+"
            group.add_argument(f"--{key}", **kwargs)


def process_cli_arguments(args):
    """Map the watershed given by the cli subcommand's options."""
    input_dict = {key: value for key, value in vars(args).items() if key in CONFIG_OPTIONS and value is not None}
    process_watershed(input_dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flood-mapping", description="Riverine flood mapping with ARC and Curve2Flood.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    json_parser = subparsers.add_parser("json", help="Map the watersheds in a JSON file")
    json_parser.add_argument("json_file", type=str, help="Path to the JSON file")
    # Let CLI override JSON for parallel choice
    grp = json_parser.add_mutually_exclusive_group()
    grp.add_argument("--parallel", dest="parallel", action="store_const", const=True, default=None,
                     help="Force parallel processing (overrides JSON).")
    grp.add_argument("--serial", dest="parallel", action="store_const", const=False,
                     help="Force serial processing (overrides JSON).")
    json_parser.add_argument("--num_workers", type=int, default=None,
                             help="Number of workers when running in parallel (overrides JSON).")

    cli_parser = subparsers.add_parser(
        "cli", help="Map one watershed given as options", allow_abbrev=False,
        usage="flood-mapping cli --name NAME --output_dir PATH [--<key> VALUE ...]",
        description="Map one watershed. Every key of a JSON watershed is an option of the same name.",
    )
    add_config_options(cli_parser)

    subparsers.add_parser("gui", help="Open the GUI")
    return parser


def main():
    args = build_parser().parse_args()

    if args.command == "json":
        LOG.info('Processing ' + str(args.json_file))
        process_json_input(args.json_file, parallel=args.parallel, num_workers=args.num_workers)
    elif args.command == "cli":
        process_cli_arguments(args)
    elif args.command == "gui":
        from nencarta.gui_app import run_gui
        run_gui()


if __name__ == "__main__":
    main()
