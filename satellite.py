"""Point extraction for NASA product files and a small terrain inspection map."""
from datetime import date
from pathlib import Path
import re

import h5py
import netCDF4
import numpy as np
import rasterio
from rasterio.warp import transform
from rasterio.windows import Window


def imerg(path, lat, lon):
    with (netCDF4.Dataset("imerg.nc4", memory=path) if isinstance(path, bytes) else netCDF4.Dataset(path)) as ds:
        group = ds.groups.get("Grid", ds)
        variable = group["precipitation"]
        if not {"lat", "lon", "time"} <= set(variable.dimensions):
            raise ValueError("Expected IMERG daily precipitation with latitude, longitude and time.")
        unit = variable.units.lower().replace(" ", "")
        if unit not in {"mm", "mm/day", "mmd-1", "mmday-1", "mmday^-1", "mmd^-1", "mm/d"}:
            raise ValueError(f"Expected daily rainfall totals or daily rates in mm, got {unit}.")
        indices = {}
        for name, coordinate in (("lat", lat), ("lon", lon)):
            axis = group[name][:]
            if name == "lon" and axis.min() >= 0 and coordinate < 0:
                coordinate %= 360
            index = int(np.argmin(abs(axis - coordinate)))
            if abs(float(axis[index]) - coordinate) > 0.051:
                raise ValueError("The selected point is outside the IMERG file coverage.")
            indices[name] = index
        indices["time"] = slice(None)
        values = variable[tuple(indices[d] for d in variable.dimensions)]
        time = group["time"]
        dates = netCDF4.num2date(time[:], time.units, getattr(time, "calendar", "standard"))
        return [{"date": d.strftime("%Y-%m-%d"), "value": float(v)} for d, v in zip(dates, values)
                if not np.ma.is_masked(v) and np.isfinite(v) and v >= 0]


def smap(path, filename, lat, lon):
    match = re.fullmatch(r"SMAP_L4_SM_gph_(\d{8}T\d{6})_Vv8\d+_\d+\.h5", filename)
    if not match:
        raise ValueError("Keep the original SPL4SMGP V008 filename so its UTC timestamp can be read.")
    stamp = match[1]
    day = date.fromisoformat(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}")
    with h5py.File(path) as ds:
        grid_lat = ds["cell_lat"]
        grid_lon = ds["cell_lon"]
        lat_axis = grid_lat[:, 0] if grid_lat.ndim == 2 else grid_lat[:]
        lon_axis = grid_lon[0, :] if grid_lon.ndim == 2 else grid_lon[:]
        row = int(np.argmin(abs(lat_axis - lat)))
        col = int(np.argmin(abs(lon_axis - lon)))
        if abs(float(lat_axis[row]) - lat) > .18 or abs(float(lon_axis[col]) - lon) > .06:
            raise ValueError("The selected point is outside the SMAP file coverage.")
        value = float(ds["Geophysical_Data/sm_rootzone"][row, col])
    if not 0 <= value <= 1:
        return []
    return [{"date": day.isoformat(), "sample": stamp, "value": value}]


def terrain(dem, lat, lon, red=None, nir=None, qa=None, radius=12):
    with rasterio.open(dem) as ds:
        if not ds.crs or ds.transform.b or ds.transform.d:
            raise ValueError("Use a georeferenced, north-up SRTM elevation raster.")
        x, y = transform("EPSG:4326", ds.crs, [lon], [lat])
        row, col = ds.index(x[0], y[0])
        if not (radius <= row < ds.height - radius and radius <= col < ds.width - radius):
            raise ValueError("The selected point is too close to an elevation tile edge.")
        win = Window(col-radius, row-radius, 2 * radius + 1, 2 * radius + 1)
        elevation = ds.read(1, window=win, masked=True).astype(float).filled(np.nan)
        transform_grid = ds.window_transform(win)
        scale = ds.crs.linear_units_factor[1] if ds.crs.is_projected else 1
        dx = abs(ds.transform.a) * (111320 * np.cos(np.radians(lat)) if ds.crs.is_geographic else scale)
        dy = abs(ds.transform.e) * (110574 if ds.crs.is_geographic else scale)
        gy, gx = np.gradient(elevation, dy, dx)
        slope = np.degrees(np.arctan(np.hypot(gx, gy)))
        xs, ys = rasterio.transform.xy(transform_grid, *np.indices(elevation.shape).reshape(2,-1))
        lons, lats = transform(ds.crs, "EPSG:4326", xs, ys)
    ndvi = np.full(elevation.shape, np.nan)
    if red and nir and qa:
        arrays = []
        for path in (red, nir, qa):
            with rasterio.open(path) as band:
                bx, by = transform("EPSG:4326", band.crs, lons, lats)
                values = [float(v[0]) if not np.ma.is_masked(v[0]) else np.nan
                          for v in band.sample(zip(bx, by), masked=True)]
                arrays.append(np.array(values).reshape(elevation.shape))
        red_dn, nir_dn, quality = arrays
        invalid = ~np.isfinite(quality) | ((np.nan_to_num(quality).astype(int) & 63) != 0)
        r, n = red_dn * .0000275 - .2, nir_dn * .0000275 - .2
        valid = ~invalid & (r >= 0) & (n >= 0) & (n + r > 0)
        np.divide(n-r, n+r, out=ndvi, where=valid)
    def number(v):
        return round(float(v), 3) if np.isfinite(v) else None
    return {"size": 2 * radius + 1, "elevation": number(elevation[radius, radius]),
            "slope": number(slope[radius, radius]), "ndvi": number(ndvi[radius, radius]), "cells": [
                {"lat": la, "lon": lo, "slope": number(s), "ndvi": number(n)}
                for la, lo, s, n in zip(lats, lons, slope.flat, ndvi.flat)]}
