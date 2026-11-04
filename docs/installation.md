# Installation

You need [conda](https://github.com/conda-forge/miniforge) (Miniforge is
recommended) and git. Docker is only needed to estimate flood damage.

1. Create the conda environment, from the repository folder:

    ```bash
    conda env create -f environment.yaml
    conda activate nencarta_py312
    ```

2. Install ARC and Curve2Flood, the models NenCarta runs:

    ```bash
    pip install --no-deps "git+https://github.com/MikeFHS/automated-rating-curve.git"
    pip install --no-deps "git+https://github.com/MikeFHS/curve2flood.git"
    ```

3. Install NenCarta:

    ```bash
    pip install --no-deps .
    ```

4. Check it works:

    ```bash
    flood-mapping -h
    ```

NenCarta downloads land cover, flows and forecasts as it needs them, so it needs
an internet connection.

## Flood damage (optional)

To estimate flood damage (`estimate_consequences`), install
[Docker](https://www.docker.com/products/docker-desktop/) and build the
Go-Consequences image from the repository folder:

```bash
docker build --progress=plain --file Dockerfile.prod -t go-consequences:latest .
```
