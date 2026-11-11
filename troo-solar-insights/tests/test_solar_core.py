"""Offline tests: the Google APIs are mocked, so no key or billing is needed.
Run: python -m pytest -q
"""
import datetime as dt
import json
import pathlib

import pytest

import solar_core as sc

FIX = json.loads((pathlib.Path(__file__).parent / "fixture_building_insights.json").read_text())


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


class FakeSession:
    """Replays queued responses per URL and records calls."""

    def __init__(self, geocode_body, solar_responses):
        self.geocode_body, self.solar = geocode_body, list(solar_responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url == sc.GEOCODE_URL:
            return Resp(200, self.geocode_body)
        return self.solar.pop(0)


GEO_OK = {
    "status": "OK",
    "results": [{
        "formatted_address": "Test Works, Leeds LS10 1AB, UK",
        "geometry": {"location": {"lat": 53.78, "lng": -1.53}, "location_type": "ROOFTOP"},
        "types": ["premise"],
    }],
}
GEO_PC = {
    "status": "OK",
    "results": [{
        "formatted_address": "Leeds LS10 1AB, UK",
        "geometry": {"location": {"lat": 53.78, "lng": -1.53}, "location_type": "APPROXIMATE"},
        "types": ["postal_code"],
    }],
}


def test_postcode_normalise():
    assert sc.normalise_postcode("ls101ab") == "LS10 1AB"
    assert sc.normalise_postcode(" sw1a 2aa ") == "SW1A 2AA"
    assert sc.normalise_postcode("10 Downing St, SW1A 2AA") is None


def test_compass():
    assert sc.compass(180) == "S" and sc.compass(350) == "N" and sc.compass(225) == "SW"


def test_lookup_ok_standard_tier():
    s = FakeSession(GEO_OK, [Resp(200, FIX)])
    r = sc.lookup("Test Works, Leeds", "k", s)
    p = r["props"]
    assert p["solar_status"] == "OK" and p["solar_data_available"] == "true"
    assert p["solar_max_panel_count"] == 120
    assert p["solar_roof_area_m2"] == 410.5
    assert p["solar_usable_area_m2"] == 235.4
    assert p["solar_yearly_kwh_potential"] == 41250       # max of configs
    assert p["solar_roof_segment_count"] == 2
    assert p["solar_dominant_azimuth_deg"] == 182.4      # largest segment
    assert p["solar_geocode_confidence"] == "ROOFTOP"
    assert set(sc.HUBSPOT_SITE_PROPS) == set(p)          # exactly the HubSpot props, nothing more
    assert r["extras"]["max_system_kwp"] == 48.0         # 120 x 400 W
    assert r["extras"]["coverage_tier"] == "STANDARD"
    assert r["geo"].precise


def test_fallback_to_expanded_on_404():
    s = FakeSession(GEO_PC, [Resp(404, {"error": {"message": "not found"}}), Resp(200, FIX)])
    r = sc.lookup("LS10 1AB", "k", s)
    assert r["extras"]["coverage_tier"] == "EXPANDED"
    solar_calls = [p for u, p in s.calls if u == sc.SOLAR_URL]
    assert solar_calls[1]["experiments"] == "EXPANDED_COVERAGE" and solar_calls[1]["requiredQuality"] == "BASE"
    assert r["geo"].is_postcode_only and not r["geo"].precise


def test_not_found_both_tiers():
    s = FakeSession(GEO_OK, [Resp(404, {}), Resp(404, {})])
    r = sc.lookup("x", "k", s)
    assert r["props"]["solar_status"] == "NOT_FOUND"
    assert r["props"]["solar_data_available"] == "false"


def test_forbidden_does_not_retry():
    s = FakeSession(GEO_OK, [Resp(403, {"error": {"message": "API not enabled"}})])
    r = sc.lookup("x", "k", s)
    assert r["props"]["solar_status"] == "SOLAR_403"
    assert len([1 for u, _ in s.calls if u == sc.SOLAR_URL]) == 1


def test_geocode_zero_results():
    s = FakeSession({"status": "ZERO_RESULTS", "results": []}, [])
    r = sc.lookup("nowhere", "k", s)
    assert r["props"]["solar_status"] == "GEOCODE_ZERO_RESULTS"


def test_estimate_rescales():
    cfgs = sc.extract(FIX, None, "STANDARD")["panel_configs"]
    e = sc.estimate(cfgs, 120, 400, 440, 0.85)
    assert e["kwp"] == 52.8
    assert e["yearly_kwh_dc"] == round(41250 * 1.1)
    assert e["yearly_kwh_ac"] == round(41250 * 1.1 * 0.85)


def test_enriched_date_iso():
    p = sc.extract(FIX, None, "STANDARD", today=dt.date(2026, 11, 4))["props"]
    assert p["solar_enriched_at"] == "2026-11-04"
    assert p["solar_geocode_confidence"] == "MANUAL_PIN"
