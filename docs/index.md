# NenCarta

NenCarta makes riverine flood maps from a DEM and a stream network. It takes
flows from GEOGLOWS or the National Water Model, builds rating curves and
estimates channel bathymetry with ARC, and maps flood extent, depth, water
surface elevation and velocity with Curve2Flood. In the coterminous United
States it can also estimate flood damage with Go-Consequences and the National
Structure Inventory.

![NenCarta workflow overview](figures/workflow.png)

## Highlights

* Run it from the GUI, a JSON file, the command line or Python.
* Map GEOGLOWS or National Water Model forecasts, return period flows or your
  own flows.
* Estimate channel bathymetry, or map on the DEM as it is.
* Move a stream network onto the DEM's flow paths, so the streams follow the
  DEM.
* Map neighbouring DEMs whose flood maps meet with fewer seams.
* Optionally estimate flood damage with Go-Consequences.

## Where to start

* [Installation](installation.md) sets NenCarta up.
* [Usage](usage.md) runs it, and [Configuration](configuration.md) describes
  every setting.
* [Python API](api.md) runs it from Python.
* [How it works](methods.md) explains what it does to the streams, how it
  estimates the river bed, and how the flood maps are made.
