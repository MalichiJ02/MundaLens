from datetime import date, timedelta

import pytest

import engine
import soil
import chirps


def make_days(start, count, rain_fn, tmax=29.0, tmin=17.0, extras=False):
    rows = []
    for i in range(count):
        day = start + timedelta(days=i)
        row = dict(date=day.isoformat(), rain=rain_fn(i), heat=tmax, tmin=tmin, rh=None, wind=None, rs=None)
        if extras:
            row.update(rh=65.0, wind=2.0, rs=20.0)
        rows.append(row)
    return rows


def test_penman_monteith_matches_fao56_example():
    # FAO-56 example 18 (Uccle, 6 July): about 3.9 mm/day. Mean RH of 73.5 % replaces max/min RH.
    et0 = engine.et0_penman_monteith(50.80, date(2024, 7, 6), 21.5, 12.3, 73.5, 2.078, 22.07, 100)
    assert 3.5 <= et0 <= 4.3


def test_penman_monteith_rejects_bad_inputs():
    assert engine.et0_penman_monteith(-15, date(2025, 1, 5), 30, 18, None, 2, 20) is None
    assert engine.et0_penman_monteith(-15, date(2025, 1, 5), 30, 18, 140, 2, 20) is None
    assert engine.et0_penman_monteith(-15, date(2025, 1, 5), 18, 30, 60, 2, 20) is None


def test_et0_falls_back_to_hargreaves_without_radiation():
    et0, method = engine.et0_for(dict(rh=None, wind=None, rs=None), -15.4, date(2025, 1, 5), 30, 18, 1100)
    assert method == "hargreaves" and et0 > 0
    et0, method = engine.et0_for(dict(rh=65, wind=2, rs=20), -15.4, date(2025, 1, 5), 30, 18, 1100)
    assert method == "penman-monteith" and 2 < et0 < 8


def test_texture_classes():
    assert soil.texture_class(90, 5) == "sand"
    assert soil.texture_class(40, 20) == "loam"
    assert soil.texture_class(20, 60) == "clay"
    assert soil.texture_class(70, 12) == "sandy loam"
    assert soil.water_capacity("sand") < soil.water_capacity("loam")


SOILGRIDS = {"properties": {"layers": [
    {"name": name, "unit_measure": {"d_factor": 10},
     "depths": [{"label": label, "values": {"mean": value}} for label in ("0-5cm", "5-15cm", "15-30cm", "30-60cm")]}
    for name, value in (("sand", 700), ("clay", 150), ("phh2o", 58))]}}


def test_parse_soilgrids():
    result = soil.parse_soilgrids(SOILGRIDS)
    assert result["texture"] == "sandy loam" and result["ph"] == 5.8
    assert result["sand_pct"] == 70 and result["awc_mm_per_m"] == 120


def test_parse_soilgrids_missing_values():
    payload = {"properties": {"layers": [{"name": "sand", "unit_measure": {"d_factor": 10}, "depths": []}]}}
    assert soil.parse_soilgrids(payload) is None


def test_slope_reduces_infiltration():
    assert engine.effective_rain(20, slope=12) < engine.effective_rain(20, slope=0)
    assert engine.effective_rain(1, slope=12) == 0


def season(start, rain_every, rain_amount, window=170, spin=45):
    days = make_days(start - timedelta(days=spin), window + spin,
                     lambda i: rain_amount if i % rain_every == 0 else 0.0, extras=True)
    return {"days": days, "window_start": spin}


def test_drier_soil_with_less_capacity_gives_bigger_deficit():
    s = season(date(2024, 11, 1), 3, 9)
    sandy = engine.simulate_crop("Maize", s["days"], -15.4, 45, "unknown", False, awc=70, window_start=45)
    clayey = engine.simulate_crop("Maize", s["days"], -15.4, 45, "unknown", False, awc=150, window_start=45)
    assert sandy["deficit_mm"] >= clayey["deficit_mm"]


def test_spinup_changes_starting_moisture():
    wet = make_days(date(2024, 9, 1), 45 + 170, lambda i: 15.0 if i < 45 and i % 2 == 0 else (6.0 if i % 4 == 0 else 0.0), extras=True)
    dry = make_days(date(2024, 9, 1), 45 + 170, lambda i: 0.0 if i < 45 else (6.0 if i % 4 == 0 else 0.0), extras=True)
    a = engine.simulate_crop("Maize", wet, -15.4, 45, "unknown", False, window_start=45)
    b = engine.simulate_crop("Maize", dry, -15.4, 45, "unknown", False, window_start=45)
    assert a["deficit_mm"] <= b["deficit_mm"]


def test_grower_soil_report_overrides_spinup_only_for_selected_season():
    days = make_days(date(2024, 9, 1), 45 + 170, lambda i: 0.0, extras=True)
    dry = engine.simulate_crop("Maize", days, -15.4, 45, "dry", False, window_start=45, apply_soil_observation=True)
    wet = engine.simulate_crop("Maize", days, -15.4, 45, "wet", False, window_start=45, apply_soil_observation=True)
    ignored = engine.simulate_crop("Maize", days, -15.4, 45, "wet", False, window_start=45, apply_soil_observation=False)
    assert dry["deficit_mm"] >= wet["deficit_mm"]
    assert ignored["deficit_mm"] == engine.simulate_crop("Maize", days, -15.4, 45, "unknown", False, window_start=45)["deficit_mm"]


def test_multi_season_ranking_uses_history_and_reports_spread():
    current = season(date(2024, 11, 1), 3, 9)
    past = [season(date(2024 - k, 11, 1), 2 if k % 2 else 8, 10 if k % 2 else 3) for k in range(1, 9)]
    result = engine.rank_crops(current["days"], -15.4, "Maize", "rainfed", "unknown", "water",
                               window_start=45, past_seasons=past)
    assert result["method"]["ranked_on"] == "history" and result["method"]["seasons_used"] == 9
    assert [i["rank"] for i in result["ranked"]] == list(range(1, 9))
    for item in result["ranked"]:
        assert item["score_bad_year"] <= item["score_median"] <= 100
        assert item["ranked_on"] == "history"
    assert result["method"]["et0"] == "penman-monteith"


def test_single_season_falls_back_and_warns():
    current = season(date(2024, 11, 1), 3, 9)
    result = engine.rank_crops(current["days"], -15.4, "Maize", "rainfed", "unknown", "water", window_start=45)
    assert result["method"]["ranked_on"] == "season"
    assert any("one season" in w for w in result["ranked"][0]["warnings"])
    assert all(i["confidence"] != "high" for i in result["ranked"])


def test_soil_profile_supplies_ph_and_capacity():
    current = season(date(2024, 11, 1), 3, 9)
    profile = soil.parse_soilgrids(SOILGRIDS)
    result = engine.rank_crops(current["days"], -15.4, "Maize", "rainfed", "unknown", "water",
                               soil_profile=profile, window_start=45)
    assert result["method"]["ph_source"] == "estimate" and result["method"]["texture"] == "sandy loam"
    grower = engine.rank_crops(current["days"], -15.4, "Maize", "rainfed", "unknown", "water",
                               ph=6.5, soil_profile=profile, window_start=45)
    assert grower["method"]["ph_source"] == "grower" and grower["method"]["ph"] == 6.5


def test_irrigated_never_short_of_water():
    current = season(date(2024, 11, 1), 40, 1)
    result = engine.rank_crops(current["days"], -15.4, "Maize", "irrigated", "unknown", "water", window_start=45)
    assert all(i["water_deficit_mm"] == 0 for i in result["ranked"])


def test_smap_disagreement_is_flagged():
    current = season(date(2024, 11, 1), 2, 12, window=100)  # wet season, window ends at the end of a bean crop
    days = current["days"]
    checks = engine.evidence_checks(days, -15.4, "Dry bean", 45, 120, "unknown", False, None, 1100, 45,
                                    smap={"value": 0.07, "observed": days[-1]["date"], "range": (0.09, 0.21)})
    assert checks and checks[0]["kind"] == "smap" and checks[0]["level"] == "warn"


def test_smap_ignored_when_observation_is_not_near_window_end():
    current = season(date(2024, 11, 1), 2, 12)
    checks = engine.evidence_checks(current["days"], -15.4, "Sorghum", 45, 120, "unknown", False, None, 1100, 45,
                                    smap={"value": 0.07, "observed": "2026-01-01", "range": (0.09, 0.21)})
    assert checks == []
    short = season(date(2024, 11, 1), 2, 12, window=100)
    checks = engine.evidence_checks(short["days"], -15.4, "Dry bean", 45, 120, "unknown", False, None, 1100, 45,
                                    smap={"value": 0.07, "observed": "2026-01-01", "range": (0.09, 0.21)})
    assert checks == []


def test_ndvi_flag_only_at_full_cover():
    current = season(date(2024, 11, 1), 2, 12)
    days = current["days"]
    mid = days[45 + 70]["date"]  # mid-season for maize planted at window start
    early = days[45 + 5]["date"]
    flagged = engine.evidence_checks(days, -15.4, "Maize", 45, 120, "unknown", True, None, 1100, 45,
                                     hls={"ndvi": 0.2, "observed": mid})
    quiet = engine.evidence_checks(days, -15.4, "Maize", 45, 120, "unknown", True, None, 1100, 45,
                                   hls={"ndvi": 0.2, "observed": early})
    assert flagged and flagged[0]["kind"] == "ndvi" and quiet == []


def test_calibration_scale_is_bounded(monkeypatch):
    monkeypatch.setattr(engine, "CALIBRATION", {"ky_scale": {"Maize": 9}})
    assert engine.ky_for("Maize") == engine.CROP_DB["Maize"]["ky"]
    monkeypatch.setattr(engine, "CALIBRATION", {"ky_scale": {"Maize": 0.8}})
    assert engine.ky_for("Maize") == pytest.approx(engine.CROP_DB["Maize"]["ky"] * 0.8)


def test_chirps_compare_and_daily(monkeypatch):
    power = [dict(date=(date(2025, 1, 1) + timedelta(days=i)).isoformat(), rain=5.0) for i in range(20)]
    other = {row["date"]: 4.0 for row in power}
    result = chirps.compare(power, other)
    assert result["difference_pct"] == -20 and result["days"] == 20
    assert chirps.compare(power[:5], other) is None

    chirps.daily.cache_clear()
    monkeypatch.setattr(chirps, "_day", lambda lat, lon, day: None if day.day == 3 else 2.0)
    values = chirps.daily(-15.4, 28.3, date(2025, 1, 1), date(2025, 1, 5))
    assert len(values) == 4 and "2025-01-03" not in values
