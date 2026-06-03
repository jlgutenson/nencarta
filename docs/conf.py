from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_FILE = PROJECT_ROOT / "pyproject.toml"

with PYPROJECT_FILE.open("rb") as pyproject_handle:
    pyproject = tomllib.load(pyproject_handle)

project = pyproject["project"]["name"]
author = ", ".join(entry["name"] for entry in pyproject["project"].get("authors", []))
release = pyproject["project"]["version"]
version = release
copyright = f"{datetime.now().year}, {author}"

extensions = [
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.duration",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
autosectionlabel_prefix_document = True

html_theme = "sphinx_rtd_theme"
html_title = "NenCarta Documentation"
html_static_path = ["_static"]
