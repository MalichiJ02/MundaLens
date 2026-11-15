"""Soil texture, water capacity and pH at the farm pin.

Reads the ISRIC SoilGrids 2.0 REST service (about 250 m) for the top 60 cm.
Texture classes use the USDA triangle and water capacity comes from typical
wilting-point and field-capacity values for each class. These are screening
estimates and should be confirmed with a local soil test.
"""

import json
from functools import lru_cache
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOILGRIDS = "https://rest.isric.org/soilgrids/v2.0/properties/query"
SOIL_SOURCE = "https://soilgrids.org"
DEPTHS = (("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15), ("30-60cm", 30))

# class: (wilting point, field capacity) as volumetric water content
WATER_LIMITS = {
    "sand": (0.05, 0.12), "loamy sand": (0.06, 0.14), "sandy loam": (0.09, 0.21),
    "loam": (0.12, 0.27), "silt loam": (0.12, 0.30), "silt": (0.09, 0.28),
    "sandy clay loam": (0.17, 0.28), "clay loam": (0.20, 0.34), "silty clay loam": (0.22, 0.36),
    "sandy clay": (0.23, 0.32), "silty clay": (0.25, 0.37), "clay": (0.27, 0.39),
}


def texture_class(sand, clay):
    """USDA texture class from sand and clay percentages."""
    silt = 100 - sand - clay
    if silt + 1.5 * clay < 15:
        return "sand"
    if silt + 1.5 * clay >= 15 and silt + 2 * clay < 30:
        return "loamy sand"
    if (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30) or (clay < 7 and silt < 50 and silt + 2 * clay >= 30):
        return "sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "silt loam"
    if silt >= 80 and clay < 12:
        return "silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 40 and silt >= 40:
        return "silty clay"
    if clay >= 40:
        return "clay"
    return "loam"


def water_capacity(texture):
    wilting, capacity = WATER_LIMITS[texture]
    return round((capacity - wilting) * 1000)


def _weighted(layer):
    """Thickness-weighted mean of a SoilGrids layer over the top 60 cm, in mapped units."""
    factor = layer.get("unit_measure", {}).get("d_factor") or 1
    by_label = {d.get("label"): d.get("values", {}).get("mean") for d in layer.get("depths", [])}
    total = weight = 0.0
    for label, thickness in DEPTHS:
        value = by_label.get(label)
        if value is None:
            continue
        total += value / factor * thickness
        weight += thickness
    return total / weight if weight >= 30 else None


def parse_soilgrids(payload):
    layers = {layer["name"]: layer for layer in payload.get("properties", {}).get("layers", [])}
    sand = _weighted(layers["sand"]) if "sand" in layers else None
    clay = _weighted(layers["clay"]) if "clay" in layers else None
    ph = _weighted(layers["phh2o"]) if "phh2o" in layers else None
    if sand is None or clay is None or not (0 <= sand <= 100 and 0 <= clay <= 100 and sand + clay <= 100.5):
        return None
    texture = texture_class(sand, clay)
    wilting, capacity = WATER_LIMITS[texture]
    return {
        "source": SOIL_SOURCE, "texture": texture, "sand_pct": round(sand), "clay_pct": round(clay),
        "silt_pct": round(100 - sand - clay), "awc_mm_per_m": water_capacity(texture),
        "wilting_point": wilting, "field_capacity": capacity,
        "ph": round(ph, 1) if ph is not None and 3 <= ph <= 10 else None,
        "status": "Estimated from SoilGrids at about 250 m for the top 60 cm. Confirm with a local soil test.",
    }


@lru_cache(maxsize=64)
def _fetch(lat, lon):
    params = [("lon", lon), ("lat", lat), ("value", "mean")]
    params += [("property", p) for p in ("sand", "clay", "phh2o")]
    params += [("depth", label) for label, _ in DEPTHS]
    request = Request(f"{SOILGRIDS}?{urlencode(params)}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read(2_000_000))


def profile(lat, lon):
    """Soil profile at the pin, or None when SoilGrids has no value (for example on water)."""
    return parse_soilgrids(_fetch(round(lat, 4), round(lon, 4)))
