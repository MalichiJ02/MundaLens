import math
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import app as server
import chirps
import soil
from test_engine import SOILGRIDS


def fake_power_csv(url, quarter_hour):
    query = parse_qs(urlsplit(url).query)
    start = date(int(query["start"][0][:4]), int(query["start"][0][4:6]), int(query["start"][0][6:]))
    end = date(int(query["end"][0][:4]), int(query["end"][0][4:6]), int(query["end"][0][6:]))
    lines = ["-BEGIN HEADER-", "x", "-END HEADER-", "YEAR,DOY,T2M_MAX,T2M_MIN,PRECTOTCORR,RH2M,WS2M,ALLSKY_SFC_SW_DWN"]
    day = start
    while day <= end:
        wet = 11 <= day.month or day.month <= 4
        rain = 9.0 if wet and day.toordinal() % (2 + day.year % 3) == 0 else 0.0
        lines.append(f"{day.year},{day.timetuple().tm_yday},29.5,17.5,{rain},65,2.0,20.0")
        day += timedelta(days=1)
    return "\n".join(lines)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "power_csv", fake_power_csv)
    monkeypatch.setattr(server, "elevation", lambda lat, lon: {"source": "x", "elevation": 1100.0, "slope": 1.5})
    monkeypatch.setattr(soil, "_fetch", lambda lat, lon: SOILGRIDS)
    chirps.daily.cache_clear()
    monkeypatch.setattr(chirps, "_day", lambda lat, lon, day: 7.0 if day.toordinal() % 2 == 0 else 0.0)
    return TestClient(server.app)


BODY = {"lat": -15.4, "lon": 28.3, "start": "2024-11-01", "end": "2025-04-30",
        "rotation": {"previous": "Maize", "alternatives": ["Groundnut", "Sorghum"], "irrigation": "rainfed",
                     "priority": "water", "soil": "unknown", "local_notes": "", "soil_ph": None}}


def test_analysis_uses_history_soil_and_terrain(client):
    response = client.post("/api/farm/analysis", json=BODY)
    assert response.status_code == 200, response.text
    data = response.json()
    method = data["rotation"]["ranking"]["method"]
    assert method["ranked_on"] == "history" and method["seasons_used"] >= 10
    assert method["texture"] == "sandy loam" and method["et0"] == "penman-monteith" and method["slope"] == 1.5
    assert len(data["rotation"]["ranking"]["recommended"]) == 2
    assert data["history"]["valid"] == len(data["history"]["days"]) == 181
    assert data["inputs"]["soil"]["texture"] == "sandy loam"


def test_analysis_degrades_when_soil_and_history_fail(client, monkeypatch):
    def broken(lat, lon):
        raise OSError("offline")
    monkeypatch.setattr(soil, "_fetch", broken)
    body = dict(BODY, past_seasons=0)
    data = client.post("/api/farm/analysis", json=body).json()
    ranking = data["rotation"]["ranking"]
    assert ranking["method"]["ranked_on"] == "season" and ranking["method"]["texture"] is None
    assert any("loam water capacity" in note for note in data["inputs"]["notes"])


def test_chirps_rain_source_replaces_selected_dates(client):
    data = client.post("/api/farm/analysis", json=dict(BODY, rain_source="chirps", past_seasons=0)).json()
    assert data["inputs"]["rain_source"] == "chirps"
    assert data["history"]["total"] == pytest.approx(7.0 * 91, abs=8)


def test_chirps_failure_falls_back_to_power(client, monkeypatch):
    chirps.daily.cache_clear()
    monkeypatch.setattr(chirps, "_day", lambda lat, lon, day: (_ for _ in ()).throw(ValueError("outside")))
    data = client.post("/api/farm/analysis", json=dict(BODY, rain_source="chirps", past_seasons=0)).json()
    assert data["inputs"]["rain_source"] == "power"
    assert any("CHIRPS" in note for note in data["inputs"]["notes"])


def test_rain_check_and_soil_endpoints(client):
    check = client.get("/api/farm/rain-check", params={"lat": -15.4, "lon": 28.3, "start": "2025-01-01", "end": "2025-02-15"}).json()
    assert check["days"] == 46 and check["agreement"] in {"close", "moderate", "weak"}
    assert client.get("/api/farm/soil", params={"lat": -15.4, "lon": 28.3}).json()["texture"] == "sandy loam"


def test_evidence_is_range_checked(client):
    evidence = {"smap": {"value": 5, "observed": "2025-04-29"}, "hls": {"ndvi": "bad", "observed": "x"}}
    response = client.post("/api/farm/analysis", json=dict(BODY, evidence=evidence, past_seasons=0))
    assert response.status_code == 200 and response.json()["rotation"]["ranking"]["checks"] == []


def test_invalid_inputs_return_400(client):
    bad = dict(BODY, rotation=dict(BODY["rotation"], alternatives=["Maize", "Sorghum"]))
    assert client.post("/api/farm/analysis", json=bad).status_code == 422
