from setuptools import setup, find_packages, Extension
from Cython.Build import cythonize
import numpy

##Takes out the CPP for floodspreader, which can cause hang-ups when doing the pip install

# # Define Cython extension modules
# extensions = [
#     Extension(
#         name="flood_mapping.functions_cpp",  # Compiled name: flood_mapping.functions_cpp
#         sources=["flood_mapping/functions_cpp.pyx"],  # Path to the .pyx file
#         include_dirs=[numpy.get_include()],  # Include numpy headers
#     ),
# ]

setup(
    name='nencarta', 
    version='0.1.1',
    description='A library for estimaing riverine bathymetr, performing flood inundation mapping, and estimating economic consequences.',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    author='Joseph Gutenson',
    author_email='joseph@follumhydro.com',
    url='https://github.com/jlgutenson/nencarta', 
    packages=find_packages(),
    install_requires=[
        'beautifulsoup4',
        'cython',
        'dask',
        'dataretrieval',
        'fiona',
        'gdal',
        'geoglows',
        'geojson',
        'geopandas',
        'netCDF4',
        'noise',
        'numpy',
        'pandas',
        'pillow==9.0.1',
        'progress',
        'pygeos',
        'pyproj',
        'rasterio',
        'requests',
        's3fs',
        'scipy',
        'shapely',
        'tqdm',
        'xarray'
    ],
    # ext_modules=cythonize(extensions, compiler_directives={"language_level": "3"}),  # Compile Cython extensions
    entry_points={
        "console_scripts": [
        "flood-mapping=flood_mapping.main:main",
        ],
    },
    python_requires='>=3.10',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],

)