"""Automatically retrieve satellite evidence for the selected farm."""

from datetime import datetime, timezone
from functools import lru_cache
import gzip
import json
import math
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from satellite import smap, terrain

CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
HLS = "https://gis.earthdata.nasa.gov/portal/rest/services/C2021957657-LPCLOUD/HLSL30_V02/ImageServer"
SRTM = "https://elevation-tiles-prod.s3.amazonaws.com/skadi"
SMAP_SOURCE = "https://nsidc.org/data/spl4smgp/versions/8"
HLS_SOURCE = HLS
LANDSAT_REFERENCE = "https://www.usgs.gov/landsat-missions/landsat-collection-2-surface-reflectance"
DATA = Path(__file__).resolve().parent / "data"
SRTM_SOURCE = "https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003"


def json_request(url, params):
    with urlopen(f"{url}?{urlencode(params)}", timeout=22) as response:
        return json.load(response)


@lru_cache(maxsize=4)
def smap_granule(hour):
    result = json_request(CMR, {"short_name": "SPL4SMGP", "version": "008",
                                "page_size": 1, "sort_key": "-start_date"})
    entry, = result["feed"]["entry"]
    url = next(link["href"] for link in entry["links"]
               if not link.get("inherited")
               and urlsplit(link["href"]).hostname == "data.nsidc.earthdatacloud.nasa.gov"
               and urlsplit(link["href"]).path.endswith(".h5"))
    name = Path(urlsplit(url).path).name
    stamp = name.removeprefix("SMAP_L4_SM_gph_").split("_")[0]
    observed = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return name, url, observed.isoformat()


@lru_cache(maxsize=32)
def moisture(lat, lon, hour):
    name, url, observed = smap_granule(hour)
    result = {"source": SMAP_SOURCE, "observed": observed, "value": None}
    token = os.getenv("NASA_EARTHDATA_TOKEN")
    if not token:
        return dict(result, status="The server needs NASA Earthdata access to retrieve the root-zone value.")
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        download_granule(url, path, token, 160 * 1024 * 1024)
        rows = smap(path, name, lat, lon)
    return dict(result, value=rows[0]["value"] if rows else None,
                status="Root-zone estimate at roughly 9 km resolution." if rows
                else "NASA reports no valid root-zone estimate at this point.")


@lru_cache(maxsize=4)
def elevation_tile(tile):
    url = f"{SRTM}/{tile[:3]}/{tile}.hgt.gz"
    with urlopen(url, timeout=25) as response, gzip.GzipFile(fileobj=response) as file:
        content = file.read(3601 * 3601 * 2 + 1)
    if len(content) not in {1201 * 1201 * 2, 3601 * 3601 * 2}:
        raise ValueError("The elevation tile did not contain the expected grid.")
    return content


@lru_cache(maxsize=64)
def elevation(lat, lon):
    if not (-56 < lat < 60 and -180 <= lon < 180):
        return {"source": SRTM_SOURCE, "status": "This point is outside SRTM coverage."}
    south, west = math.floor(lat), math.floor(lon)
    tile = f"{'N' if south >= 0 else 'S'}{abs(south):02d}{'E' if west >= 0 else 'W'}{abs(west):03d}"
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{tile}.hgt"
        path.write_bytes(elevation_tile(tile))
        result = terrain(path, lat, lon, radius=1)
    return {"source": SRTM_SOURCE, "elevation": result["elevation"], "slope": result["slope"],
            "status": "Slope describes the area around the pin. Check runoff and soil cover in the field."}


def scene_ndvi(samples, by_id, minimum):
    """Median clear-sky NDVI per scene from getSamples results. Needs `minimum` clear pixels."""
    per_scene = {}
    for sample in samples:
        try:
            bands = [int(value) for value in sample["value"].split()]
            # HLSL30 service order: B01-B07, Fmask, then angles. Fmask bits 0-5 flag
            # cirrus, cloud, cloud edge, shadow, snow or ice, and water.
            red, nir, mask = bands[3], bands[4], bands[7]
            if mask & 63 or red < 0 or nir < 0 or red + nir == 0:
                continue
            value = (nir - red) / (nir + red)
            scene = by_id[sample["rasterId"]]
        except (TypeError, ValueError, KeyError, IndexError, AttributeError):
            continue
        if -1 <= value <= 1:
            per_scene.setdefault(sample["rasterId"], []).append(value)
    return [(by_id[rid]["acquisitiondate"], by_id[rid]["name"], median(values))
            for rid, values in per_scene.items() if len(values) >= minimum]


@lru_cache(maxsize=64)
def vegetation(lat, lon, hour):
    point = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
    query = {"f": "json", "where": "category=1", "geometry": f"{lon},{lat}", "inSR": 4326,
             "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects",
             "outFields": "objectid,acquisitiondate,name", "orderByFields": "acquisitiondate DESC",
             "resultRecordCount": 8, "returnGeometry": "false"}
    scenes = json_request(f"{HLS}/query", query)["features"]
    if not scenes:
        return {"source": HLS_SOURCE, "status": "No Landsat scene was returned for this point."}
    by_id = {scene["attributes"]["objectid"]: scene["attributes"] for scene in scenes}
    mosaic = json.dumps({"mosaicMethod": "esriMosaicLockRaster",
                         "lockRasterIds": [scene["attributes"]["objectid"] for scene in scenes]})
    base = {"f": "json", "mosaicRule": mosaic, "outFields": "acquisitiondate,name",
            "renderingRule": json.dumps({"rasterFunction": "NDVI Raw with Cloud Mask"}),
            "returnFirstValueOnly": "false"}
    valid, pixels = [], 1
    try:
        # A 5 x 5 grid of 30 m pixels around the pin is steadier than a single pixel.
        dlat = 30 / 110574
        dlon = 30 / (111320 * max(0.05, math.cos(math.radians(lat))))
        grid = [[lon + i * dlon, lat + j * dlat] for i in range(-2, 3) for j in range(-2, 3)]
        multi = dict(base, geometry=json.dumps({"points": grid, "spatialReference": {"wkid": 4326}}),
                     geometryType="esriGeometryMultipoint")
        valid = scene_ndvi(json_request(f"{HLS}/getSamples", multi)["samples"], by_id, minimum=5)
        pixels = 25
    except (OSError, KeyError, TypeError, ValueError):
        valid = []
    if not valid:
        pixels = 1
        single = dict(base, geometry=json.dumps(point), geometryType="esriGeometryPoint")
        valid = scene_ndvi(json_request(f"{HLS}/getSamples", single)["samples"], by_id, minimum=1)
    if not valid:
        return {"source": HLS_SOURCE, "status": "Recent Landsat scenes have no clear NDVI sample at the pin."}
    stamp, name, value = max(valid, key=lambda item: item[0])
    raster_id = next(scene["attributes"]["objectid"] for scene in scenes if scene["attributes"]["name"] == name)
    bbox = f"{lon - .006},{lat - .004},{lon + .006},{lat + .004}"
    image_params = {"f": "image", "bbox": bbox, "bboxSR": 4326, "imageSR": 4326,
                    "size": "550,360", "format": "png32",
                    "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicLockRaster",
                                              "lockRasterIds": [raster_id]}),
                    "renderingRule": json.dumps({"rasterFunction": "Natural Color with DRA and Cloud Mask"})}
    return {"source": HLS_SOURCE, "scene": name, "ndvi": round(value, 3), "pixels_used": pixels,
            "observed": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(),
            "image_url": f"{HLS}/exportImage?{urlencode(image_params)}",
            "status": "Cloud-masked vegetation index at about 30 m, median of the clear pixels around the pin. Verify soil cover in the field."}


def archived_vegetation(lat, lon):
    if abs(lat + 15.4) > .003 or abs(lon - 28.3) > .003:
        return None
    bands = [DATA / f"lusaka_{band}.tif" for band in ("red", "nir", "qa")]
    try:
        result = terrain(DATA / "lusaka_elevation.tif", lat, lon, *bands)
    except ValueError:
        return None
    values = [cell["ndvi"] for cell in result["cells"] if cell["ndvi"] is not None]
    if not values:
        return None
    return {"observed": "2025-03-25", "scene": "LC09_L2SP_172071_20250325_20250326_02_T1",
            "ndvi_median": round(median(values), 3), "clear_pixels": len(values),
            "pixels": len(result["cells"]), "source": LANDSAT_REFERENCE}

IMERG_COLLECTION = "C2723754850-GES_DISC"
IMERG_SOURCE = "https://gpm.nasa.gov/data/imerg"


def download_granule(url, path, token, limit):
    # Data centers may redirect to an Earthdata login or storage URL.
    # Do not forward the bearer token across hosts.
    from urllib.request import HTTPRedirectHandler, build_opener

    class EarthdataRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirect and urlsplit(redirect.full_url).hostname != urlsplit(req.full_url).hostname:
                redirect.remove_header("Authorization")
            return redirect

    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with build_opener(EarthdataRedirect()).open(request, timeout=25) as response, path.open("wb") as file:
        remaining = limit
        while chunk := response.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("The Earthdata granule exceeded the server download limit.")
            file.write(chunk)


@lru_cache(maxsize=2)
def imerg_daily_granule(hour):
    result = json_request(CMR, {"collection_concept_id": IMERG_COLLECTION,
                                "page_size": 1, "sort_key": "-start_date"})
    entry, = result["feed"]["entry"]
    url = next(link["href"] for link in entry["links"]
               if not link.get("inherited")
               and urlsplit(link["href"]).hostname == "data.gesdisc.earthdata.nasa.gov"
               and urlsplit(link["href"]).path.endswith(".nc4"))
    return url, entry["time_start"][:10]


@lru_cache(maxsize=1)
def imerg_bytes(url, token):
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "imerg.nc4"
        download_granule(url, path, token, 90 * 1024 * 1024)
        return path.read_bytes()


@lru_cache(maxsize=32)
def imerg_daily_point(lat, lon, hour):
    from satellite import imerg

    url, day = imerg_daily_granule(hour)
    result = {"source": IMERG_SOURCE, "observed": day, "value": None}
    token = os.getenv("NASA_EARTHDATA_TOKEN")
    if not token:
        return dict(result, status="The dated map is public. Numerical daily IMERG needs server-side Earthdata access.")
    rows = imerg(imerg_bytes(url, token), lat, lon)
    row = next((row for row in rows if row["date"] == day), None)
    return dict(result, value=row["value"] if row else None,
                status="Daily Early Run rainfall estimate for the surrounding grid cell." if row
                else "The daily IMERG granule contained no valid observation at this point.")
