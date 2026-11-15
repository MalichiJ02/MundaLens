"""Local MundaLens web server. Run with Python 3.10 or later."""

import csv
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from xml.etree.ElementTree import ParseError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from pydantic import BaseModel, Field
from satellite import imerg, smap, terrain
from live import imerg_view
from evidence import (elevation, moisture, vegetation, archived_vegetation, imerg_daily_point,
                      HLS_SOURCE, IMERG_SOURCE, SMAP_SOURCE, SRTM_SOURCE)
from rotations import CROPS, RotationOptions, compare_rotations
from engine import CROP_DB, SPINUP_DAYS
import chirps
import soil as soil_module

ROOT = Path(__file__).resolve().parent
POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
DEMO = (-15.4, 28.3)


class Comparison(BaseModel):
    lat: float
    lon: float
    start: date
    end: date
    planting_a: date
    planting_b: date
    stage_start: int = Field(ge=0, le=365)
    stage_end: int = Field(ge=0, le=365)
    threshold: float = Field(default=1, gt=0, le=100)
    rainfall_source: str = Field(default="power", pattern="^(power|imerg)$")
    imerg_daily: dict[date, float] = Field(default_factory=dict, max_length=731)


def climate(query):
    lat, lon = (float(query[k][0]) for k in ("lat", "lon"))
    start, end = (date.fromisoformat(query[k][0]) for k in ("start", "end"))
    return daily_climate(lat, lon, start, end, True)


POWER_PARAMETERS = "T2M_MAX,T2M_MIN,PRECTOTCORR,RH2M,WS2M,ALLSKY_SFC_SW_DWN"


def number(value):
    """Float from a POWER CSV cell, or None for blanks and the -999 missing marker."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > -990 else None


def daily_climate(lat, lon, start, end, allow_demo=False, max_days=730):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Enter valid latitude and longitude.")
    if not 0 <= (end - start).days <= max_days:
        raise ValueError(f"Choose a date range of at most {max_days + 1} days.")
    params = urlencode(dict(parameters=POWER_PARAMETERS, community="AG",
                            latitude=lat, longitude=lon, start=start.strftime("%Y%m%d"),
                            end=end.strftime("%Y%m%d"), format="CSV", **{"time-standard": "UTC"}))
    url = f"{POWER}?{params}"
    bundled = allow_demo and (lat, lon) == DEMO and date(2023, 11, 1) <= start <= end <= date(2025, 4, 30)
    if bundled:
        source = (ROOT / "data/lusaka.csv").read_text(encoding="utf-8-sig")
    else:
        source = power_csv(url, int(datetime.now(timezone.utc).timestamp()) // 900)
    lines = source.splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith("YEAR,DOY,")), None)
    if header is None:
        raise ValueError("NASA POWER did not return the expected daily data table.")
    records = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[header:]))):
        day = date(int(row["YEAR"]), 1, 1) + timedelta(days=int(row["DOY"]) - 1)
        rain, heat, low = (number(row.get(k)) for k in ("PRECTOTCORR", "T2M_MAX", "T2M_MIN"))
        records[day] = dict(rain=rain if rain is not None and rain >= 0 else None, heat=heat, tmin=low,
                            rh=number(row.get("RH2M")), wind=number(row.get("WS2M")),
                            rs=number(row.get("ALLSKY_SFC_SW_DWN")))
    blank = dict(rain=None, heat=None, tmin=None, rh=None, wind=None, rs=None)
    days = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        days.append(dict(date=day.isoformat(), **records.get(day, blank)))
    return dict(days=days, source="NASA POWER / MERRA-2 corrected precipitation and maximum air temperature",
                url=url, bundled=bundled, lat=lat, lon=lon)


def climate_rows(lat, lon, start, end, chunk=1800):
    """Daily rows for a long range, fetched in parallel chunks. Failed chunks leave blank days."""
    spans, cursor = [], start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=chunk - 1))
        spans.append((cursor, stop))
        cursor = stop + timedelta(days=1)

    def fetch(span):
        try:
            return daily_climate(lat, lon, span[0], span[1], True, chunk)["days"]
        except (OSError, ValueError, KeyError):
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(fetch, spans))
    return {row["date"]: row for part in parts for row in part}


def shift_years(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def past_season_rows(lat, lon, start, end, count):
    """The same calendar dates in earlier years, each preceded by spin-up days."""
    today = datetime.now(timezone.utc).date()
    windows = []
    for k in range(1, count + 1):
        s, e = shift_years(start, k), shift_years(end, k)
        if s - timedelta(days=SPINUP_DAYS) < date(1981, 1, 1) or e >= today - timedelta(days=7):
            continue
        windows.append((s, e))
    if not windows:
        return []
    rows = climate_rows(lat, lon, min(s for s, _ in windows) - timedelta(days=SPINUP_DAYS), max(e for _, e in windows))
    blank = dict(rain=None, heat=None, tmin=None, rh=None, wind=None, rs=None)
    seasons = []
    for s, e in windows:
        total = (e - s).days + 1 + SPINUP_DAYS
        days = [rows.get((s - timedelta(days=SPINUP_DAYS) + timedelta(days=i)).isoformat(),
                         dict(blank, date=(s - timedelta(days=SPINUP_DAYS) + timedelta(days=i)).isoformat()))
                for i in range(total)]
        window = days[SPINUP_DAYS:]
        if sum(row["rain"] is None for row in window) > 0.2 * len(window):
            continue
        seasons.append({"days": days, "window_start": SPINUP_DAYS})
    return seasons


@lru_cache(maxsize=64)
def power_csv(url, quarter_hour):
    with urlopen(url, timeout=22) as response:
        return response.read(3_000_000).decode("utf-8-sig")


app = FastAPI(title="MundaLens API")


@app.get("/api/climate")
def get_climate(lat: float, lon: float, start: date, end: date):
    try:
        return climate({"lat": [lat], "lon": [lon], "start": [start.isoformat()], "end": [end.isoformat()]})
    except (KeyError, ValueError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(502, "Climate data unavailable. Try the bundled Lusaka demo or reconnect.") from error


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/live/conditions")
def recent_conditions(lat: float, lon: float):
    validate_point(lat, lon)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=20)
    climate_data = get_climate(lat, lon, start, end)
    rows = climate_data["days"]
    latest_rain = next((row for row in reversed(rows) if row["rain"] is not None), None)
    latest_heat = next((row for row in reversed(rows) if row["heat"] is not None), None)
    if latest_rain is None and latest_heat is None:
        raise HTTPException(503, "NASA POWER has no published values for this point in the last 21 days.")
    rain_end = next((i for i in range(len(rows) - 1, -1, -1) if rows[i]["rain"] is not None), -1)
    week = rows[max(0, rain_end-6):rain_end+1]
    valid = [row["rain"] for row in week if row["rain"] is not None]
    return {"rain": latest_rain, "heat": latest_heat,
            "week": {"start": week[0]["date"], "end": week[-1]["date"], "days": len(valid),
                     "total": round(sum(valid), 2)} if week else None,
            "source": climate_data["source"], "url": climate_data["url"],
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/live/imerg-map")
def live_rain_map(lat: float, lon: float):
    validate_point(lat, lon)
    if not (-90 < lat < 90 and -180 < lon < 180):
        raise HTTPException(400, "Choose a point inside the map bounds.")
    try:
        return imerg_view(lat, lon)
    except (OSError, ValueError, ParseError) as error:
        raise HTTPException(502, "NASA's IMERG imagery catalog is temporarily unavailable.") from error


def analyse(data, scenario):
    days = data["days"]
    rain = [row["rain"] for row in days if row["rain"] is not None]
    heat = [row["heat"] for row in days if row["heat"] is not None]
    spells = []
    for row in days:
        if row["rain"] is not None and row["rain"] < scenario.threshold:
            if spells and date.fromisoformat(spells[-1]["end"]) + timedelta(days=1) == date.fromisoformat(row["date"]):
                spells[-1]["end"] = row["date"]
                spells[-1]["days"] += 1
            else:
                spells.append(dict(start=row["date"], end=row["date"], days=1))
    windows = []
    for planting in (scenario.planting_a, scenario.planting_b):
        start = planting + timedelta(days=scenario.stage_start)
        end = planting + timedelta(days=scenario.stage_end)
        covered = [row["rain"] for row in days if start.isoformat() <= row["date"] <= end.isoformat() and row["rain"] is not None]
        windows.append(dict(planting=planting, start=start, end=end,
                            dry_days=sum(value < scenario.threshold for value in covered),
                            available_days=len(covered), days=(end - start).days + 1))
    return dict(**data, total=round(sum(rain), 2) if rain else None, valid=len(rain),
                peak=max(heat, default=None), longest=max(spells, key=lambda spell: spell["days"], default=None),
                windows=windows)


@app.post("/api/compare")
def compare(scenario: Comparison):
    if scenario.stage_start > scenario.stage_end:
        raise HTTPException(400, "The growth stage must end on or after its start.")
    data = get_climate(scenario.lat, scenario.lon, scenario.start, scenario.end)
    if scenario.rainfall_source == "imerg":
        if not scenario.imerg_daily or any(not math.isfinite(v) or v < 0 for v in scenario.imerg_daily.values()):
            raise HTTPException(400, "Import usable daily IMERG data for this location.")
        for row in data["days"]:
            row["rain"] = scenario.imerg_daily.get(date.fromisoformat(row["date"]))
        data["source"] = "Imported GPM IMERG daily rainfall / NASA POWER maximum air temperature"
    return analyse(data, scenario)


def validate_point(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Invalid location.")


def copy_upload(upload, path):
    total = 0
    with path.open("wb") as target:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > 250 * 1024 * 1024:
                raise HTTPException(413, "Use a subset smaller than 250 MB per file.")
            target.write(chunk)


@app.post("/api/satellite/{product}")
def extract(product: str, lat: float = Form(), lon: float = Form(), file: UploadFile = File()):
    validate_point(lat, lon)
    if product not in {"imerg", "smap"}:
        raise HTTPException(404, "Choose IMERG or SMAP.")
    try:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample"
            copy_upload(file, path)
            rows = imerg(path, lat, lon) if product == "imerg" else smap(path, file.filename or "", lat, lon)
    except (OSError, RuntimeError, KeyError, ValueError, TypeError, IndexError) as error:
        raise HTTPException(422, f"Could not read the selected NASA product: {error}") from error
    return {"rows": rows, "product": product}


@app.get("/api/terrain/demo")
def terrain_demo():
    data = ROOT / "data"
    landsat = [data / f"lusaka_{band}.tif" for band in ("red", "nir", "qa")]
    result = terrain(data / "lusaka_elevation.tif", *DEMO, *landsat)
    return dict(result, scene="Landsat 9 Collection 2 Level-2 · 25 March 2025", source="SRTM elevation")


@app.post("/api/terrain")
def terrain_upload(lat: float = Form(), lon: float = Form(), dem: UploadFile = File(),
                   red: UploadFile | None = File(None), nir: UploadFile | None = File(None),
                   qa: UploadFile | None = File(None)):
    validate_point(lat, lon)
    if any((red, nir, qa)) and not all((red, nir, qa)):
        raise HTTPException(400, "Provide all three Landsat bands together.")
    try:
        with TemporaryDirectory() as tmp:
            paths = []
            for name, upload in zip(("dem", "red", "nir", "qa"), (dem, red, nir, qa)):
                path = Path(tmp) / f"{name}.tif"
                if upload:
                    copy_upload(upload, path)
                paths.append(path if upload else None)
            return terrain(paths[0], lat, lon, *paths[1:])
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise HTTPException(422, f"Could not process the rasters: {error}") from error


class FarmAnalysis(BaseModel):
    lat: float
    lon: float
    start: date
    end: date
    threshold: float = Field(default=1, gt=0, le=100)
    rotation: RotationOptions
    rain_source: str = Field(default="power", pattern="^(power|chirps)$")
    past_seasons: int = Field(default=15, ge=0, le=25)
    evidence: dict | None = None


@app.get("/api/config")
def config():
    return {"crops": list(CROPS), "google_maps_key": os.getenv("GOOGLE_MAPS_BROWSER_KEY", ""),
            "stages": {crop: list(spec["stages"]) for crop, spec in CROP_DB.items()}}


def clean_evidence(raw):
    """Keep only the few numbers the consistency checks use, with range checks."""
    cleaned = {}
    for name, key, low, high in (("smap", "value", 0, 1), ("hls", "ndvi", -1, 1)):
        item = raw.get(name) if isinstance(raw, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get(key), (int, float)):
            continue
        if low <= item[key] <= high and isinstance(item.get("observed"), str):
            cleaned[name] = {key: float(item[key]), "observed": item["observed"][:10]}
    return cleaned


@app.post("/api/farm/analysis")
def farm_analysis(body: FarmAnalysis):
    validate_point(body.lat, body.lon)
    scenario = Comparison(
        lat=body.lat, lon=body.lon, start=body.start, end=body.end,
        planting_a=body.start, planting_b=body.start, stage_start=0, stage_end=0,
        threshold=body.threshold,
    )
    spin_start = body.start - timedelta(days=SPINUP_DAYS)
    try:
        try:
            full = daily_climate(body.lat, body.lon, spin_start, body.end, False, 730 + SPINUP_DAYS)
        except OSError:
            # Offline: the bundled Lusaka history is the same NASA POWER series, without humidity, wind or radiation.
            full = daily_climate(body.lat, body.lon, spin_start, body.end, True, 730 + SPINUP_DAYS)
            if not full["bundled"]:
                raise
    except (KeyError, ValueError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(502, "NASA POWER did not return the historical weather. Try again shortly.") from error
    spin_days, window = full["days"][:SPINUP_DAYS], full["days"][SPINUP_DAYS:]
    inputs = {"rain_source": "power", "notes": []}
    if body.rain_source == "chirps":
        try:
            observed = chirps.daily(body.lat, body.lon, body.start, body.end)
            for row in window:
                if row["date"] in observed:
                    row["rain"] = observed[row["date"]]
            inputs["rain_source"] = "chirps"
            full["source"] = "CHIRPS daily rainfall / NASA POWER temperature"
        except (OSError, ValueError, RuntimeError, ImportError):
            inputs["notes"].append("CHIRPS rainfall was unavailable here, so NASA POWER rainfall was used.")
    try:
        history = analyse(dict(full, days=window), scenario)
    except (KeyError, ValueError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error
    history.pop("windows")

    context = {"spin_days": spin_days, "evidence": clean_evidence(body.evidence)}
    terrain = optional_evidence(elevation, SRTM_SOURCE, body.lat, body.lon)
    context["slope"], context["elevation"] = terrain.get("slope"), terrain.get("elevation")
    try:
        context["soil_profile"] = soil_module.profile(body.lat, body.lon)
    except (OSError, ValueError, KeyError, TypeError):
        context["soil_profile"] = None
    if context["soil_profile"] is None:
        inputs["notes"].append("Soil texture could not be estimated, so a loam water capacity was assumed.")
    if body.past_seasons:
        try:
            context["past_seasons"] = past_season_rows(body.lat, body.lon, body.start, body.end, body.past_seasons)
        except (OSError, ValueError, OverflowError):
            context["past_seasons"] = []
        if inputs["rain_source"] == "chirps":
            inputs["notes"].append("Past seasons use NASA POWER rainfall because CHIRPS is only fetched for the selected dates.")
    try:
        rotations = compare_rotations(history, body.rotation, body.lat, context)
    except (KeyError, ValueError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error
    inputs.update(soil=context["soil_profile"], slope=context["slope"], elevation=context["elevation"])
    return {"history": history, "rotation": rotations, "inputs": inputs}


@app.get("/api/farm/soil")
def farm_soil(lat: float, lon: float):
    validate_point(lat, lon)
    try:
        result = soil_module.profile(lat, lon)
    except (OSError, ValueError, KeyError, TypeError):
        result = None
    if result is None:
        return {"source": soil_module.SOIL_SOURCE, "status": "SoilGrids did not return soil texture for this point."}
    return result


@app.get("/api/farm/rain-check")
def farm_rain_check(lat: float, lon: float, start: date, end: date):
    """Compare NASA POWER and CHIRPS rainfall totals for the same days."""
    validate_point(lat, lon)
    try:
        power = daily_climate(lat, lon, start, end)["days"]
        observed = chirps.daily(lat, lon, start, end)
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        return {"source": chirps.CHIRPS_SOURCE,
                "status": "CHIRPS rainfall is only available over Africa and may be unavailable for recent days."}
    result = chirps.compare(power, observed)
    if result is None:
        return {"source": chirps.CHIRPS_SOURCE, "status": "Too few days were available to compare the two rainfall sources."}
    gap = abs(result["difference_pct"] or 0)
    result["agreement"] = "close" if gap <= 15 else "moderate" if gap <= 30 else "weak"
    result["status"] = ("NASA POWER and CHIRPS agree closely for these dates." if gap <= 15 else
                        "The two rainfall sources differ noticeably. Local rain-gauge records would settle which is closer.")
    return result


def optional_evidence(operation, source, *args):
    try:
        return operation(*args)
    except (OSError, StopIteration, KeyError, TypeError, ValueError, RuntimeError, IndexError):
        return {"source": source, "status": "This service did not return evidence for the selected point."}


@app.get("/api/farm/satellite/{product}")
def farm_satellite(product: str, lat: float, lon: float):
    validate_point(lat, lon)
    hour = int(datetime.now(timezone.utc).timestamp()) // 3600
    if product == "imerg":
        return optional_evidence(imerg_daily_point, IMERG_SOURCE, lat, lon, hour)
    if product == "smap":
        return optional_evidence(moisture, SMAP_SOURCE, lat, lon, hour)
    if product == "hls":
        result = optional_evidence(vegetation, HLS_SOURCE, lat, lon, hour)
        if "ndvi" not in result:
            archive = optional_evidence(archived_vegetation, HLS_SOURCE, lat, lon)
            if archive and "ndvi_median" in archive:
                result["archive"] = archive
        return result
    if product == "srtm":
        return optional_evidence(elevation, SRTM_SOURCE, lat, lon)
    raise HTTPException(404, "Choose imerg, smap, hls or srtm.")


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
