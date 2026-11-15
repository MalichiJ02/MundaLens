"""Find the latest published NASA GIBS rainfall image for a point."""

from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET

GIBS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
LAYER = "IMERG_Precipitation_Rate_30min"
LEGEND = "https://gibs.earthdata.nasa.gov/legends/GPM_Precipitation_Rate_H.png"


@lru_cache(maxsize=2)
def imerg_timestamp(quarter_hour):
    query = urlencode({"SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": "1.3.0"})
    with urlopen(f"{GIBS}?{query}", timeout=16) as response:
        root = ET.fromstring(response.read(12_000_000))
    for layer in root.findall(".//{*}Layer"):
        if layer.findtext("{*}Name") != LAYER:
            continue
        time = layer.find("{*}Dimension[@name='time']")
        if time is None or "default" not in time.attrib:
            break
        stamp = datetime.fromisoformat(time.attrib["default"].replace("Z", "+00:00"))
        if stamp.tzinfo and stamp <= datetime.now(timezone.utc):
            return stamp
        break
    raise ValueError("NASA GIBS did not publish a usable default IMERG time.")


def imerg_view(lat, lon):
    stamp = imerg_timestamp(int(datetime.now(timezone.utc).timestamp()) // 900)
    # Keep the selected point centered, even near the antimeridian or poles.
    x = min(2.8, max(0.00001, 180 - abs(lon)))
    y = min(1.65, max(0.00001, 90 - abs(lat)))
    params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1", "SRS": "EPSG:4326",
              "LAYERS": "BlueMarble_ShadedRelief," + LAYER, "STYLES": ",",
              "BBOX": f"{lon-x},{lat-y},{lon+x},{lat+y}",
              "WIDTH": 660, "HEIGHT": 400, "FORMAT": "image/png",
              "TIME": stamp.isoformat().replace("+00:00", "Z")}
    return {"published_time": stamp.isoformat(), "checked_at": datetime.now(timezone.utc).isoformat(),
            "image_url": f"{GIBS}?{urlencode(params)}", "legend_url": LEGEND,
            "source": "NASA GIBS / IMERG precipitation rate"}
