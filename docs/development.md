# Development

## Tests

With the environment active, from the repository folder:

```bash
python -m pytest
```

GitHub runs the tests on every pull request (`.github/workflows/tests.yml`),
in a new conda environment made from `environment.yaml`, with ARC and
Curve2Flood installed from GitHub. Tests that need data kept only on a
developer's machine, in the git-ignored `ignore/` folder, are skipped where it
is missing.

## Adding a setting

1. Add it to `CONFIG_OPTIONS` in `nencarta/core/defaults.py`, with its
    default, its type and a one-line description. The `cli` command gets an
    option for it from there.
2. Read it in `NencartaConfig.setup` (`nencarta/core/configs.py`).
3. Give it a field in the GUI (`nencarta/gui_app.py`).
4. Document it in `docs/configuration.md`, as an item starting with its
    anchor: `` * <a id="json-<key>"></a>`<key>` (<type>, default <value>): ``.

The tests fail until the GUI and the docs have it.

## Documentation

The documentation is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/). To build
it, or to serve it while you edit it, from the repository folder:

```bash
pip install -r docs/requirements.txt
mkdocs serve
```

`mkdocs build --strict` fails on any warning, such as a broken link, as
GitHub's check of the documentation does. Read the Docs builds it from
`mkdocs.yml` (see `.readthedocs.yaml`).

The [Python API](api.md) page is made by
[mkdocstrings](https://mkdocstrings.github.io/) from the docstrings, which are
in numpy style. It formats the signatures with ruff's Python package, which
`docs/requirements.txt` installs; conda-forge's `ruff` is the program alone,
and without the package the signatures stay on one line.

The diagrams are [Mermaid](https://mermaid.js.org/) flowcharts, written in the
pages as `mermaid` code blocks and drawn by the browser. The overview on the
home page, `docs/figures/workflow.png`, is drawn from `workflow.drawio` beside
it with [draw.io](https://www.drawio.com/). The other figures in
`docs/figures` are made by `docs/figures/make_figures.py`: the synthetic one
on its own, and the others from a run on the FABDEM tile N39W109 (see the
script for its settings):

```bash
python docs/figures/make_figures.py --run <run folder> --dem <N39W109_FABDEM_V1-2.tif>
```
