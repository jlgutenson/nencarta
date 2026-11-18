from pathlib import Path

from nencarta.logger import LOG
from nencarta.workspace import Workspace

# Manning's n of each ESA WorldCover class
ESA_MANNINGS_N = (
    (10, 'Tree Cover', '0.120'),
    (20, 'Shrubland', '0.050'),
    (30, 'Grassland', '0.030'),
    (40, 'Cropland', '0.035'),
    (50, 'Builtup', '0.075'),
    (60, 'Bare', '0.030'),
    (70, 'SnowIce', '0.030'),
    (80, 'Water', '0.030'),
    (90, 'Emergent_Herb_Wet', '0.100'),
    (95, 'Mangroves', '0.100'),
    (100, 'MossLichen', '0.100'),
)

def Create_BaseLine_Manning_n_File_ESA(workspace: Workspace) -> Path:
    """Write the Manning's n table of the ESA WorldCover classes, unless it exists."""
    ManningN = workspace.mannings_n_text_file
    if ManningN.exists():
        LOG.info(f"Manning's n file already exists, skipping creation: {ManningN}")
        return ManningN

    ManningN.parent.mkdir(parents=True, exist_ok=True)
    LOG.info('Creating Manning n file: ' + str(ManningN))
    with open(ManningN,'w') as out_file:
        out_file.write('LC_ID\tDescription\tManning_n')
        out_file.writelines(f'\n{lc_id}\t{description}\t{mannings_n}' for lc_id, description, mannings_n in ESA_MANNINGS_N)

    return ManningN
