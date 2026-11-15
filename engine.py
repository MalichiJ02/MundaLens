"""Crop rotation scoring for MundaLens.

Pure Python (no third-party imports). Combines a daily FAO-56 style water
balance, heat stress, a pH check and agronomic rotation rules into one score
per crop, then ranks every crop in CROP_DB against the previous crop.

Accuracy features
- Reference evapotranspiration uses FAO-56 Penman-Monteith when solar
  radiation, humidity and wind are available, and Hargreaves otherwise.
- Soil water capacity comes from the soil profile at the pin when known.
- Soil moisture at the start of each season is spun up from the weather that
  came before it, unless the grower reported the soil as dry/moist/wet.
- Slope reduces the share of rain that soaks in.
- When past seasons for the same dates are supplied, each crop is scored in
  every season and ranked on the typical (median) and bad-year result.

All crop parameters below are approximate defaults compiled from FAO-56
(Kc, stage lengths), FAO 33 (yield response, ky) and general agronomy
references. They MUST be checked against local varieties before real use.
Scores are a screening aid, not a yield forecast.
"""

import json
import math
from datetime import date
from pathlib import Path

# stages = days of (initial, development, mid-season, late)
# kc = (initial, mid, end); ky = FAO-33 whole-season yield response factor
# root = effective rooting depth in metres; p = fraction of available water
# that can be used before stress starts; heat = max temperature (C) tolerated
# at flowering; ph = (low, high) comfortable soil pH range
CROP_DB = {
    "Maize":        dict(stages=(25, 40, 40, 25), kc=(0.30, 1.20, 0.60), ky=1.25, root=1.0, p=0.55, tbase=8,  heat=35, ph=(5.5, 7.5), family="Poaceae", role="heavy", legume=False),
    "Sorghum":      dict(stages=(20, 35, 40, 30), kc=(0.30, 1.05, 0.55), ky=0.90, root=1.2, p=0.55, tbase=8,  heat=38, ph=(5.5, 8.5), family="Poaceae", role="moderate", legume=False),
    "Soybean":      dict(stages=(20, 30, 40, 20), kc=(0.40, 1.15, 0.50), ky=0.85, root=0.8, p=0.50, tbase=10, heat=35, ph=(6.0, 7.0), family="Fabaceae", role="fixer", legume=True),
    "Groundnut":    dict(stages=(25, 35, 40, 25), kc=(0.40, 1.15, 0.60), ky=0.70, root=0.6, p=0.50, tbase=10, heat=36, ph=(5.5, 7.0), family="Fabaceae", role="fixer", legume=True),
    "Dry bean":     dict(stages=(20, 30, 35, 15), kc=(0.40, 1.15, 0.35), ky=1.15, root=0.5, p=0.45, tbase=8,  heat=32, ph=(6.0, 7.5), family="Fabaceae", role="fixer", legume=True),
    "Cowpea":       dict(stages=(20, 25, 35, 20), kc=(0.40, 1.05, 0.35), ky=0.80, root=0.8, p=0.50, tbase=10, heat=36, ph=(5.5, 7.5), family="Fabaceae", role="fixer", legume=True),
    "Sunflower":    dict(stages=(25, 35, 45, 25), kc=(0.35, 1.10, 0.35), ky=0.95, root=1.5, p=0.45, tbase=6,  heat=35, ph=(6.0, 7.5), family="Asteraceae", role="moderate", legume=False),
    "Sweet potato": dict(stages=(20, 30, 60, 40), kc=(0.50, 1.15, 0.65), ky=0.80, root=1.0, p=0.65, tbase=12, heat=35, ph=(5.5, 6.5), family="Convolvulaceae", role="light", legume=False),
}
COVER_CROPS = {"Cowpea", "Sweet potato", "Groundnut"}
STAGE_WEIGHT = (0.6, 0.9, 1.5, 0.7)  # water stress matters most at flowering and yield formation
SOIL_START = {"dry": 0.25, "moist": 0.6, "wet": 0.9, "unknown": 0.5}
AWC_MM_PER_M = 120.0  # loam default, used only when no soil profile is available
ASSUMED_DAILY_RANGE = 12.0  # used only when daily minimum temperature is missing
SPINUP_DAYS = 45  # weather before the season used to estimate starting soil moisture
MIN_SEASONS_FOR_RANKING = 5  # fewer past seasons than this are not used to rank crops

# Optional local calibration written by the grower's own validation work.
# data/calibration.json: {"ky_scale": {"Maize": 0.95}, "rotation_scale": 1.0}
_CALIBRATION_FILE = Path(__file__).resolve().parent / "data" / "calibration.json"


def _load_calibration():
    try:
        data = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


CALIBRATION = _load_calibration()


def ky_for(crop):
    scale = CALIBRATION.get("ky_scale", {}).get(crop, 1.0)
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    return CROP_DB[crop]["ky"] * (scale if 0.5 <= scale <= 1.5 else 1.0)


# ---------------------------------------------------------------- weather ---

def extraterrestrial_mj(lat, doy):
    """FAO-56 eq. 21, MJ/m2/day."""
    phi = math.radians(lat)
    dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
    delta = 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)
    x = max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta)))
    ws = math.acos(x)
    return (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.sin(ws))


def extraterrestrial_radiation(lat, doy):
    """Extraterrestrial radiation as mm/day of evaporation equivalent."""
    return 0.408 * extraterrestrial_mj(lat, doy)


def et0_hargreaves(lat, day, tmax, tmin):
    doy = day.timetuple().tm_yday
    mean = (tmax + tmin) / 2
    return max(0.0, 0.0023 * (mean + 17.8) * math.sqrt(max(tmax - tmin, 0.1)) * extraterrestrial_radiation(lat, doy))


def _sat_vp(t):
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def et0_penman_monteith(lat, day, tmax, tmin, rh, wind, rs, elevation=0.0):
    """FAO-56 Penman-Monteith (eq. 6) with daily inputs.

    rh is mean relative humidity (%), wind is 2 m wind speed (m/s) and rs is
    incoming solar radiation (MJ/m2/day). Returns mm/day, or None when the
    inputs are not physically plausible.
    """
    if None in (tmax, tmin, rh, wind, rs) or tmax < tmin or rs < 0 or wind < 0 or not 0 <= rh <= 100:
        return None
    z = elevation or 0.0
    doy = day.timetuple().tm_yday
    tmean = (tmax + tmin) / 2
    pressure = 101.3 * ((293 - 0.0065 * z) / 293) ** 5.26
    gamma = 0.000665 * pressure
    delta = 4098 * _sat_vp(tmean) / (tmean + 237.3) ** 2
    es = (_sat_vp(tmax) + _sat_vp(tmin)) / 2
    ea = rh / 100 * es
    ra = extraterrestrial_mj(lat, doy)
    rso = (0.75 + 2e-5 * z) * ra
    if rso <= 0:
        return None
    rns = (1 - 0.23) * rs
    cloud = min(rs / rso, 1.0)
    rnl = 4.903e-9 * (((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2) * (0.34 - 0.14 * math.sqrt(max(ea, 0))) * (1.35 * cloud - 0.35)
    rn = rns - rnl
    et0 = (0.408 * delta * rn + gamma * 900 / (tmean + 273) * wind * (es - ea)) / (delta + gamma * (1 + 0.34 * wind))
    return max(0.0, et0) if math.isfinite(et0) else None


def et0_for(row, lat, day, tmax, tmin, elevation):
    """Penman-Monteith when the row has radiation, humidity and wind, else Hargreaves."""
    pm = et0_penman_monteith(lat, day, tmax, tmin, row.get("rh"), row.get("wind"), row.get("rs"), elevation)
    if pm is not None and pm < 15:
        return pm, "penman-monteith"
    return et0_hargreaves(lat, day, tmax, tmin), "hargreaves"


def runoff_factor(slope):
    """Share of effective rain that soaks in on sloping ground (heuristic)."""
    if slope is None or slope < 2:
        return 1.0
    if slope < 5:
        return 0.95
    if slope < 10:
        return 0.85
    return 0.75


def effective_rain(rain, slope=None):
    """Light showers mostly evaporate and heavy storms run off."""
    base = max(0.0, min(rain, 60.0) - 2.0) * 0.9 if rain > 2.0 else 0.0
    return base * runoff_factor(slope)


def find_onset(days, threshold=1.0):
    """First day with >=20 mm in 3 days and no 10-day dry spell in the following 30 days."""
    rain = [d["rain"] if d["rain"] is not None else 0.0 for d in days]
    for i in range(len(rain) - 3):
        if sum(rain[i:i + 3]) < 20:
            continue
        run = longest = 0
        for value in rain[i + 3:i + 33]:
            run = run + 1 if value < threshold else 0
            longest = max(longest, run)
        if longest < 10:
            return i
    return None


def kc_for(spec, day_in_cycle):
    ini, dev, mid, late = spec["stages"]
    kc_i, kc_m, kc_e = spec["kc"]
    if day_in_cycle < ini:
        return kc_i, 0
    if day_in_cycle < ini + dev:
        return kc_i + (kc_m - kc_i) * (day_in_cycle - ini) / dev, 1
    if day_in_cycle < ini + dev + mid:
        return kc_m, 2
    return kc_m + (kc_e - kc_m) * (day_in_cycle - ini - dev - mid) / late, 3


def stage_at(crop, plant_index, index):
    """Growth stage (0-3) of the crop at a day index, or None before planting or after harvest."""
    spec = CROP_DB[crop]
    age = index - plant_index
    if age < 0 or age >= sum(spec["stages"]):
        return None
    return kc_for(spec, age)[1]


# ------------------------------------------------------------- simulation ---

def simulate_crop(crop, days, lat, plant_index, soil, irrigated, awc=None, slope=None,
                  elevation=0.0, window_start=0, apply_soil_observation=True):
    """Daily water balance for one crop.

    days may start with spin-up days (before window_start). Soil moisture then
    follows the weather unless the grower reported it and apply_soil_observation
    is true, in which case the report applies at window_start.
    """
    spec = CROP_DB[crop]
    cycle = sum(spec["stages"])
    taw = (awc or AWC_MM_PER_M) * spec["root"]
    raw = spec["p"] * taw
    depletion = taw * (1 - SOIL_START["unknown"])
    etc_stage = [0.0] * 4
    eta_stage = [0.0] * 4
    heat_days = gdd = missing = pm_days = total_days = 0
    estimated_tmin = False
    last = min(len(days), plant_index + cycle)
    for i, row in enumerate(days[:last]):
        if i == window_start and apply_soil_observation and soil in SOIL_START and soil != "unknown":
            depletion = taw * (1 - SOIL_START[soil])
        day = date.fromisoformat(row["date"])
        in_window = i >= window_start
        tmax = row["heat"]
        tmin = row.get("tmin")
        if tmax is None:
            missing += in_window
            tmax, tmin = 30.0, 18.0
        elif tmin is None:
            tmin = tmax - ASSUMED_DAILY_RANGE
            estimated_tmin = estimated_tmin or in_window
        rain = row["rain"]
        if rain is None:
            missing += in_window
            rain = 0.0
        et0, method = et0_for(row, lat, day, tmax, tmin, elevation)
        depletion = max(0.0, depletion - effective_rain(rain, slope))
        if i < plant_index:
            depletion = min(taw, depletion + 0.15 * et0)  # bare-soil evaporation
            continue
        age = i - plant_index
        kc, stage = kc_for(spec, age)
        etc = kc * et0
        ks = 1.0 if irrigated or depletion <= raw else max(0.0, (taw - depletion) / (taw - raw))
        eta = etc * ks
        depletion = min(taw, depletion + eta)
        etc_stage[stage] += etc
        eta_stage[stage] += eta
        total_days += 1
        pm_days += method == "penman-monteith"
        gdd += max(0.0, (tmax + tmin) / 2 - spec["tbase"])
        if stage == 2 and row["heat"] is not None and row["heat"] > spec["heat"]:
            heat_days += 1
    rel = 1.0
    ky = ky_for(crop)
    for s in range(4):
        if etc_stage[s] > 0:
            deficit = 1 - eta_stage[s] / etc_stage[s]
            rel *= max(0.0, 1 - ky * STAGE_WEIGHT[s] * deficit)
    observed = last - plant_index
    return dict(
        rel_yield=max(0.0, min(1.0, rel)),
        deficit_mm=round(sum(etc_stage) - sum(eta_stage), 1),
        demand_mm=round(sum(etc_stage), 1),
        flowering_deficit_mm=round(etc_stage[2] - eta_stage[2], 1),
        heat_days=heat_days, gdd=round(gdd), cycle_days=cycle,
        completeness=max(0.0, min(1.0, observed / cycle)),
        missing_days=int(missing), estimated_tmin=estimated_tmin,
        pm_share=(pm_days / total_days) if total_days else 0.0,
        wetness_end=max(0.0, min(1.0, 1 - depletion / taw)),
        reached_end=last >= len(days),
    )


def rotation_points(previous, crop):
    """Returns (points, reasons). Positive points favour the sequence previous -> crop."""
    prev, nxt = CROP_DB[previous], CROP_DB[crop]
    points, reasons = 0.0, []
    if previous == crop:
        points -= 25
        reasons.append(f"{crop} after {crop} builds up its own pests and diseases.")
    elif prev["family"] == nxt["family"]:
        points -= 12
        reasons.append(f"{crop} and {previous} are both {prev['family']}, so they share pests and diseases.")
    else:
        points += 6
        reasons.append(f"Different plant family from {previous}, which helps break pest and disease cycles.")
    if prev["role"] in ("heavy", "moderate") and nxt["legume"]:
        points += 14
        reasons.append(f"{crop} fixes nitrogen after {previous}, which drew nitrogen from the soil.")
    elif prev["legume"] and nxt["role"] in ("heavy", "moderate"):
        points += 10
        reasons.append(f"{crop} can use the nitrogen {previous} leaves behind.")
    elif prev["role"] == "heavy" and nxt["role"] == "heavy":
        points -= 6
        reasons.append(f"Both {previous} and {crop} take a lot of nitrogen.")
    if abs(prev["root"] - nxt["root"]) >= 0.4:
        points += 4
        reasons.append(f"{crop} roots at a different depth from {previous}, so the soil is used more evenly.")
    scale = CALIBRATION.get("rotation_scale", 1.0)
    if isinstance(scale, (int, float)) and 0.5 <= scale <= 1.5:
        points *= scale
    return points, reasons


def ph_multiplier(crop, ph):
    if ph is None:
        return 1.0, None
    low, high = CROP_DB[crop]["ph"]
    gap = max(low - ph, ph - high, 0)
    if gap == 0:
        return 1.0, None
    if gap <= 0.5:
        return 0.9, f"Soil pH {ph} is slightly outside the comfortable {low}-{high} range for {crop}."
    return 0.7, f"Soil pH {ph} is well outside the {low}-{high} range for {crop}. Liming or a different crop may be needed."


def _downgrade(level):
    return {"high": "medium", "medium": "low"}.get(level, "low")


def score_crop(crop, previous, days, lat, plant_index, soil, irrigated, priority, ph,
               awc=None, slope=None, elevation=0.0, window_start=0, apply_soil_observation=True):
    sim = simulate_crop(crop, days, lat, plant_index, soil, irrigated, awc, slope, elevation,
                        window_start, apply_soil_observation)
    points, rot_reasons = rotation_points(previous, crop)
    if priority == "cover" and crop in COVER_CROPS:
        points += 10
        rot_reasons.append(f"{crop} gives good ground cover, which matches your soil-cover priority.")
    rotation_score = max(0.0, min(100.0, 50 + points * 1.5))
    heat_mult = 1 - min(0.3, 0.02 * sim["heat_days"])
    season_mult = 0.5 + 0.5 * sim["completeness"]
    ph_mult, ph_note = ph_multiplier(crop, ph)
    yield_score = 100 * sim["rel_yield"] * heat_mult * season_mult * ph_mult
    w_yield, w_rot = {"water": (0.75, 0.25), "cover": (0.5, 0.5)}.get(priority, (0.65, 0.35))
    score = w_yield * yield_score + w_rot * rotation_score
    warnings = []
    if sim["deficit_mm"] > 60 and not irrigated:
        warnings.append(f"Water shortfall of about {sim['deficit_mm']} mm over the season, {sim['flowering_deficit_mm']} mm of it at flowering.")
    if sim["heat_days"]:
        warnings.append(f"{sim['heat_days']} days above {CROP_DB[crop]['heat']}°C during flowering.")
    if sim["completeness"] < 1:
        warnings.append("The selected dates end before this crop would mature, so its score is reduced. Extend the end date.")
    if sim["missing_days"]:
        warnings.append(f"{sim['missing_days']} days had missing NASA data.")
    if ph_note:
        warnings.append(ph_note)
    confidence = "high"
    if sim["estimated_tmin"] or sim["missing_days"] > 5:
        confidence = "medium"
    if sim["completeness"] < 1 or sim["missing_days"] > 15:
        confidence = "low"
    return dict(
        crop=crop, sequence=[previous, crop], score=round(score), water_score=round(yield_score),
        rotation_score=round(rotation_score), rel_yield_pct=round(100 * sim["rel_yield"]),
        water_demand_mm=sim["demand_mm"], water_deficit_mm=sim["deficit_mm"], heat_days=sim["heat_days"],
        gdd=sim["gdd"], cycle_days=sim["cycle_days"], reasons=rot_reasons, warnings=warnings,
        confidence=confidence, legume=CROP_DB[crop]["legume"],
        pm_share=sim["pm_share"], wetness_end=sim["wetness_end"], reached_end=sim["reached_end"],
        raw_score=score,
    )


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[int(math.floor(fraction * (len(ordered) - 1)))]


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _plant_index(days, window_start):
    """Index of the planting day (first reliable rains) and whether rains were found."""
    onset = find_onset(days[window_start:])
    return window_start + (onset if onset is not None else 0), onset is not None


def rank_crops(history_days, lat, previous, irrigation, soil, priority, ph=None, soil_profile=None,
               slope=None, elevation=None, window_start=0, past_seasons=()):
    """Rank every crop for the farm.

    history_days: the selected season, optionally preceded by spin-up days
    (window_start is the index of the first selected day).
    past_seasons: list of dicts {"days": [...], "window_start": int} for the
    same calendar dates in earlier years.
    """
    irrigated = irrigation == "irrigated"
    elevation = elevation or 0.0
    awc = soil_profile["awc_mm_per_m"] if soil_profile and soil_profile.get("awc_mm_per_m") else None
    if ph is None and soil_profile and soil_profile.get("ph") is not None:
        ph_used, ph_source = soil_profile["ph"], "estimate"
    else:
        ph_used, ph_source = ph, "grower" if ph is not None else None

    if irrigated:
        plant_index, onset_found = window_start, True
    else:
        plant_index, onset_found = _plant_index(history_days, window_start)
    planting = history_days[plant_index]["date"] if history_days else None
    ranked = [score_crop(c, previous, history_days, lat, plant_index, soil, irrigated, priority, ph_used,
                         awc, slope, elevation, window_start, True) for c in CROP_DB]

    past_scores = {c: [] for c in CROP_DB}
    used = 0
    for season in past_seasons:
        days, start = season["days"], season["window_start"]
        if not days or len(days) <= start:
            continue
        index = start if irrigated else _plant_index(days, start)[0]
        for c in CROP_DB:
            past_scores[c].append(score_crop(c, previous, days, lat, index, soil, irrigated, priority,
                                             ph_used, awc, slope, elevation, start, False)["raw_score"])
        used += 1

    use_history = used + 1 >= MIN_SEASONS_FOR_RANKING
    for item in ranked:
        season_score = item["raw_score"]
        item["season_score"] = round(season_score)
        scores = [season_score] + past_scores[item["crop"]]
        item["seasons_used"] = len(scores)
        item["score_median"] = round(_median(scores))
        item["score_bad_year"] = round(_percentile(scores, 0.2))
        item["score_spread"] = round(_percentile(scores, 0.8) - _percentile(scores, 0.2))
        if use_history:
            item["score"] = round(0.7 * _median(scores) + 0.3 * _percentile(scores, 0.2))
            item["ranked_on"] = "history"
        else:
            item["score"] = round(season_score)
            item["ranked_on"] = "season"
            if item["confidence"] == "high":
                item["confidence"] = "medium"
            item["warnings"].append("Only the selected season was available, so this ranking reflects one season's weather.")
        if use_history and item["score_spread"] > 30:
            item["confidence"] = _downgrade(item["confidence"])
            item["warnings"].append(f"Results swing a lot between seasons (spread {item['score_spread']} points), so treat this ranking with caution.")
        del item["raw_score"]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank

    pm_share = sum(i["pm_share"] for i in ranked) / len(ranked) if ranked else 0.0
    notes = [
        "Planting is assumed at the first reliable rains in your dates, or at the start of the dates if none were found."
        if not irrigated else "Irrigated fields are assumed to be planted at the start of the dates and never short of water.",
        (f"Soil water capacity is {awc:.0f} mm per metre from the {soil_profile.get('texture', 'soil')} texture estimated at your pin."
         if awc else f"Soil is assumed to hold {AWC_MM_PER_M:.0f} mm of water per metre because soil texture could not be estimated."),
        ("Ranking uses the median and bad-year result across " + str(used + 1) + " seasons with the same dates."
         if use_history else "Past seasons for these dates were not available, so the ranking uses the selected season only."),
        "Crop parameters are approximate FAO-based defaults. Confirm varieties and local planting calendars before deciding.",
    ]
    return dict(
        ranked=ranked, planting_date=planting, onset_found=onset_found,
        method=dict(
            et0="penman-monteith" if pm_share > 0.8 else "mixed" if pm_share > 0 else "hargreaves",
            soil_source=soil_profile.get("source") if soil_profile else None, awc_mm_per_m=awc or AWC_MM_PER_M,
            texture=soil_profile.get("texture") if soil_profile else None, ph=ph_used, ph_source=ph_source,
            slope=slope, seasons_used=used + 1, ranked_on="history" if use_history else "season",
            calibrated=bool(CALIBRATION),
        ),
        notes=notes,
        _plant_index=plant_index,
    )


# ------------------------------------------------------ evidence checks ---

def evidence_checks(history_days, lat, current_crop, plant_index, awc, soil, irrigated, slope, elevation,
                    window_start, smap=None, hls=None):
    """Compare satellite observations with the model and return plain-language flags.

    These are sanity checks, not corrections: they lower trust in a result when
    the satellite picture and the water balance disagree.
    """
    checks = []
    if not history_days:
        return checks
    sim = simulate_crop(current_crop, history_days, lat, plant_index, soil, irrigated, awc, slope,
                        elevation, window_start, True)
    last_day = date.fromisoformat(history_days[-1]["date"])
    if smap and isinstance(smap.get("value"), (int, float)) and smap.get("observed") and sim["reached_end"] and not irrigated:
        try:
            observed = date.fromisoformat(str(smap["observed"])[:10])
        except ValueError:
            observed = None
        profile_range = smap.get("range")  # (wilting, field capacity) volumetric, supplied by the caller
        if observed and abs((observed - last_day).days) <= 10 and profile_range:
            wilting, capacity = profile_range
            if capacity > wilting:
                satellite = max(0.0, min(1.0, (smap["value"] - wilting) / (capacity - wilting)))
                gap = abs(satellite - sim["wetness_end"])
                if gap > 0.35:
                    checks.append(dict(
                        kind="smap", level="warn",
                        text=f"SMAP suggests the root zone is about {round(satellite * 100)}% of available water but the water balance estimates {round(sim['wetness_end'] * 100)}%. "
                             "Local rain, irrigation or soil type may differ from the model, so treat the water results with caution."))
                else:
                    checks.append(dict(kind="smap", level="ok",
                                       text="SMAP root-zone moisture is consistent with the modelled water balance."))
    if hls and isinstance(hls.get("ndvi"), (int, float)) and hls.get("observed"):
        try:
            seen = date.fromisoformat(str(hls["observed"])[:10])
            first = date.fromisoformat(history_days[window_start]["date"])
        except ValueError:
            seen = first = None
        if seen and first <= seen <= last_day:
            index = window_start + (seen - first).days
            if stage_at(current_crop, plant_index, index) == 2 and hls["ndvi"] < 0.3:
                checks.append(dict(
                    kind="ndvi", level="warn",
                    text=f"Landsat NDVI was only {hls['ndvi']:.2f} on {seen.isoformat()}, when {current_crop} should have had full cover. "
                         "The crop may have struggled, been planted later, or the pin may not be on the field."))
    return checks
