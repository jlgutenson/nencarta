"""
The models' own settings nencarta passes on: arc_args (every ARC run), curve2flood_args (the flood maps) and
fldpln_library_args (curve2flood.build_fldpln_library). Each model documents its keys; nencarta merges the given ones
over its starting values (core.defaults), checks them with the model's own key check, and keeps out the keys it sets
itself.
"""
import difflib
import inspect

from nencarta.core.defaults import (
    RESERVED_ARC_KEYS,
    RESERVED_CURVE2FLOOD_KEYS,
    RESERVED_FLDPLN_LIBRARY_ARGS,
)


def merge_args(start: dict, given: dict | None) -> dict:
    """``given`` over ``start``, matching the keys whatever their case, as the models do: a key of ``given`` replaces
    ``start``'s, and one set to None drops it, so the model's default holds."""
    merged = dict(start)
    for key, value in (given or {}).items():
        for same in [old for old in merged if str(old).lower() == str(key).lower()]:
            del merged[same]
        if value is not None:
            merged[key] = value
    return merged


def get_arg(args: dict, key: str, default=None):
    """``args``' value of ``key``, whatever its case, or ``default``."""
    return next((value for name, value in args.items() if str(name).lower() == key.lower()), default)


def check_arc_args(args: dict, given: dict) -> dict:
    """
    ``args`` (arc_args merged over nencarta's) by ARC's field names, typed, after checking each key ``given`` is ARC's
    and not one nencarta sets. Raises ValueError otherwise.
    """
    from arc.config import Configs

    return _check_model_args("arc_args", "ARC", Configs.check_keys, args, given, RESERVED_ARC_KEYS)


def check_curve2flood_args(args: dict, given: dict) -> dict:
    """The same as `check_arc_args`, for curve2flood_args and Curve2Flood."""
    from curve2flood.config import Config

    return _check_model_args("curve2flood_args", "Curve2Flood", Config.check_keys, args, given,
                             RESERVED_CURVE2FLOOD_KEYS)


def _check_model_args(name: str, model: str, check_keys, args: dict, given: dict, reserved: set) -> dict:
    for key, value in given.items():
        if value is None:
            continue
        try:
            fields = check_keys({key: value})
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
        if set(fields) & reserved:
            raise ValueError(f"{name} can't give {key}: nencarta sets it in {model}'s input files itself.")

    try:
        return check_keys(args)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None


def check_fldpln_library_args(args: dict) -> dict:
    """``args``, after checking each is an argument of curve2flood.build_fldpln_library that nencarta doesn't give
    itself. Raises ValueError otherwise."""
    if not args:
        return {}

    from curve2flood import build_fldpln_library

    parameters = sorted(inspect.signature(build_fldpln_library).parameters)
    for key in args:
        if key in RESERVED_FLDPLN_LIBRARY_ARGS:
            raise ValueError(f"fldpln_library_args can't give {key}: nencarta gives it to build_fldpln_library itself.")
        if key not in parameters:
            close = difflib.get_close_matches(key, parameters, n=1)
            raise ValueError(f"fldpln_library_args: build_fldpln_library has no argument {key!r}"
                             + (f" (did you mean {close[0]!r}?)" if close else "") + ".")
        
    return dict(args)
