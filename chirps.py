"""CHIRPS daily rainfall (about 5 km) for Africa, used to cross-check NASA POWER.

Reads one pixel per day directly from the CHC archive with range requests, so
nothing large is downloaded. Coverage is Africa only. Recent days may be
missing because CHIRPS is published with a delay.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache

BASE = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05"
CHIRPS_SOURCE = "https://www.chc.ucsb.edu/data/chirps"
MAX_DAYS = 366


def _day(lat, lon, day):
    """CHIRPS rainfall in mm for one day at a point, or None if unavailable."""
    import rasterio
    from rasterio.windows import Window

    url = f"/vsicurl/{BASE}/{day:%Y}/chirps-v2.0.{day:%Y.%m.%d}.tif"
    with rasterio.Env(GDAL_HTTP_TIMEOUT="12", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                      CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        with rasterio.open(url) as dataset:
            row, col = dataset.index(lon, lat)
            if not (0 <= row < dataset.height and 0 <= col < dataset.width):
                raise ValueError("This point is outside CHIRPS Africa coverage.")
            value = float(dataset.read(1, window=Window(col, row, 1, 1))[0, 0])
    return value if value >= 0 else None


@lru_cache(maxsize=16)
def daily(lat, lon, start, end):
    """Map of ISO date to rainfall (mm) for start..end, or raises when nothing is readable."""
    if not (-40 <= lat <= 40 and -20 <= lon <= 55):
        raise ValueError("CHIRPS Africa covers latitudes 40 S to 40 N and longitudes 20 W to 55 E.")
    count = (end - start).days + 1
    if not 1 <= count <= MAX_DAYS:
        raise ValueError(f"Choose at most {MAX_DAYS} days for the CHIRPS cross-check.")
    days = [start + timedelta(days=i) for i in range(count)]

    def one(day):
        try:
            return day, _day(lat, lon, day)
        except ValueError:
            raise
        except Exception:  # missing day file, network error
            return day, None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, days))
    values = {day.isoformat(): value for day, value in results if value is not None}
    if not values:
        raise ValueError("CHIRPS returned no rainfall for these dates.")
    return values


def compare(power_days, chirps_days):
    """Agreement between POWER rows and CHIRPS over the days both report."""
    pairs = [(row["rain"], chirps_days[row["date"]]) for row in power_days
             if row["rain"] is not None and row["date"] in chirps_days]
    if len(pairs) < 10:
        return None
    power_total = sum(p for p, _ in pairs)
    chirps_total = sum(c for _, c in pairs)
    ratio = chirps_total / power_total if power_total > 0 else None
    return {
        "days": len(pairs), "power_mm": round(power_total, 1), "chirps_mm": round(chirps_total, 1),
        "difference_pct": round((chirps_total - power_total) / power_total * 100) if power_total > 0 else None,
        "ratio": round(ratio, 2) if ratio else None, "source": CHIRPS_SOURCE,
    }
