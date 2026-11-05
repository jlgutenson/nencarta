Development
===========

Build the documentation locally
-------------------------------

Install the documentation dependencies:

.. code-block:: bash

   pip install -r docs/requirements.txt

Then build the HTML output from the repository root:

.. code-block:: bash

   sphinx-build -b html docs docs/_build/html

Read the Docs configuration
---------------------------

This repository is configured for Read the Docs with:

* ``.readthedocs.yaml`` at the repository root
* ``docs/conf.py`` for the Sphinx project configuration
* ``docs/requirements.txt`` for documentation-only Python dependencies

When the repository is connected to a Read the Docs project, the platform will build the documentation from the ``docs/`` directory automatically.
