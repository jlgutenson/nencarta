"""Combine ARC representative coefficients after all DEM workspaces finish."""

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from nencarta.logger import LOG


def make_combined_representative_xs_data(
    coefficient_files: Iterable[Path], output_file: Path
) -> Path:
    """Write one coefficients CSV with one row per COMID across all inputs.

    Duplicate COMIDs receive a Cross_Section_Count-weighted arithmetic mean
    for each coefficient: sum(coefficient * count) / sum(count). The output
    count is the sum of input counts. Nonfinite coefficients (including ARC's
    failed-fit NaNs) are excluded from that coefficient's weighted mean;
    coefficients with no finite observations remain blank in the output.

    All input files must have COMID, positive integer Cross_Section_Count,
    and the same coefficient columns. Repeated input paths are read only once.
    Missing inputs or invalid schemas raise before writing the output. An
    existing output is rebuilt from the inputs, never included as an input.

    Parameters
    ----------
    coefficient_files : Iterable[Path]
        Per-DEM representative_cross_section_coefficients_file paths.
    output_file : Path
        Destination for the combined CSV in the watershed's workspace.output_dir.
    """
    paths = list(dict.fromkeys(Path(path).resolve() for path in coefficient_files))
    output_file = Path(output_file)
    if not paths:
        raise ValueError("No representative cross-section coefficient files were supplied.")
    if output_file.resolve() in paths:
        raise ValueError("The combined coefficients output must differ from every input file.")

    frames = []
    columns = None
    for path in paths:
        frame = pd.read_csv(path, dtype={"COMID": "Int64"})
        if not {"COMID", "Cross_Section_Count"}.issubset(frame.columns):
            raise ValueError(f"{path} must contain COMID and Cross_Section_Count columns.")
        if columns is None:
            columns = frame.columns.tolist()
            if len(columns) < 3:
                raise ValueError(f"{path} has no coefficient columns.")
        elif set(frame.columns) != set(columns):
            raise ValueError(f"Coefficient columns in {path} do not match the other inputs.")
        if frame["COMID"].isna().any():
            raise ValueError(f"{path} contains missing COMID values.")
        counts = pd.to_numeric(frame["Cross_Section_Count"], errors="raise")
        if not (np.isfinite(counts) & (counts > 0) & (counts % 1 == 0)).all():
            raise ValueError(f"{path} requires positive integer Cross_Section_Count values.")
        frame["Cross_Section_Count"] = counts.astype("int64")
        for column in columns:
            if column not in {"COMID", "Cross_Section_Count"}:
                frame[column] = pd.to_numeric(frame[column], errors="raise")
        frames.append(frame)

    data = pd.concat(frames, ignore_index=True)
    combined = data.groupby("COMID", sort=True)[["Cross_Section_Count"]].sum()
    for column in columns:
        if column in {"COMID", "Cross_Section_Count"}:
            continue
        # Each coefficient has its own valid-weight denominator so a failed
        # fit does not dilute another DEM's valid estimate toward zero.
        valid = data.loc[np.isfinite(data[column])]
        weighted_sum = (valid[column] * valid["Cross_Section_Count"]).groupby(valid["COMID"]).sum()
        total_weight = valid.groupby("COMID")["Cross_Section_Count"].sum()
        combined[column] = weighted_sum / total_weight

    combined = combined.reset_index()[columns]
    output_file.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(output_file, index=False)
    LOG.info(f"Combined {len(paths)} representative coefficient files into {output_file} ({len(combined)} COMIDs).")
    return output_file
