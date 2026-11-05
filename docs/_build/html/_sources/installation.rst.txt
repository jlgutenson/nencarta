Installation
============

Prerequisites
-------------

NenCarta relies on a Python environment with geospatial libraries, plus external toolkits for the flood-mapping workflow:

* Miniconda or Anaconda
* ARC
* Curve2Flood
* Docker, if you want to run Go-Consequences

Create the environment
----------------------

From the repository root:

.. code-block:: bash

   conda env create -f environment.yaml
   conda activate nencarta_py312

Install the external toolkits
-----------------------------

Install ARC and Curve2Flood into the same active conda environment. Follow the setup steps from their upstream repositories:

* ARC: https://github.com/MikeFHS/automated-rating-curve
* Curve2Flood: https://github.com/MikeFHS/curve2flood

Install NenCarta
----------------

From the repository root:

.. code-block:: bash

   pip install .

After installation, verify the CLI entry point:

.. code-block:: bash

   flood-mapping -h

Optional: build the Go-Consequences container
---------------------------------------------

If you want NenCarta to estimate direct flood consequences, build the Docker image included in this repository:

.. code-block:: bash

   docker build --progress=plain --file Dockerfile.prod -t go-consequences:latest .
