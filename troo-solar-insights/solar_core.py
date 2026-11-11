"""
solar_core.py — Google Geocoding + Solar API (buildingInsights) logic for Troo Energy Services.

No Streamlit imports here, so the same module can be reused from Colab, GitHub Actions
or a future HubSpot custom-code port.

Field mapping is identical to `hubspot_solar_enrichment_action.js` (Site object 2-112673583),
so a CSV exported from the app can be imported straight onto Site records.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
SOLAR_URL = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
TIMEOUT = 20

# UK postcode (outward + inward), tolerant of missing space / lower case
UK_POSTCODE_RE = re.compile(r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\s*$", re.IGNORECASE)

# Coverage tiers tried in order. LOW = "anything Google has at standard quality" (HIGH/MEDIUM/LOW);
# then the experimental ML-from-satellite tier, which fills many UK gaps at BASE quality.
COVERAGE_TIERS: list[tuple[str, dict[str, str]]] = [
    ("STANDARD", {"requiredQuality": "LOW"}),
    ("EXPANDED", {"requiredQuality": "BASE", "experiments": "EXPANDED_COVERAGE"}),
]

# HubSpot Site property names — keep in lock-step with the custom-code action.
HUBSPOT_SITE_PROPS = [
    "solar_data_available",
    "solar_status",
    "solar_imagery_quality",
    "solar_geocode_confidence",
    "solar_roof_area_m2",
    "solar_usable_area_m2",
    "solar_max_panel_count",
    "solar_yearly_kwh_potential",
    "solar_roof_segment_count",
    "solar_dominant_pitch_deg",
    "solar_dominant_azimuth_deg",
    "solar_max_sunshine_hours",
    "solar_carbon_offset_kg_mwh",
    "solar_enriched_at",
]


class SolarError(Exception):
    """Carries a HubSpot-ready status code (e.g. GEOCODE_ZERO_RESULTS, NOT_FOUND, SOLAR_403)."""

    def __init__(self, status: str, message: str = ""):
        super().__init__(message or status)
        self.status = status
        self.message = message or status


# ----------------------------------------------------------------------------- helpers
def normalise_postcode(text: str) -> str | None:
    """Return 'SW1A 1AA' style postcode if the whole string is a UK postcode, else None."""
    m = UK_POSTCODE_RE.match(text or "")
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


def compass(azimuth: float | None) -> str:
    if azimuth is None:
        return ""
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int(((azimuth % 360) + 22.5) // 45) % 8]


def _r(x: Any, nd: int = 1):
    return round(float(x), nd) if x is not None else None


def _date(d: dict | None) -> str:
    if not d:
        return ""
    try:
        return dt.date(d["year"], d.get("month", 1), d.get("day", 1)).isoformat()
    except Exception:
        return ""


# ----------------------------------------------------------------------------- API calls
@dataclass
class GeocodeResult:
    query: str
    lat: float
    lng: float
    formatted_address: str
    location_type: str  # ROOFTOP / RANGE_INTERPOLATED / GEOMETRIC_CENTER / APPROXIMATE
    partial_match: bool
    is_postcode_only: bool
    types: list[str] = field(default_factory=list)

    @property
    def precise(self) -> bool:
        return self.location_type == "ROOFTOP" and not self.partial_match and not self.is_postcode_only


def geocode(query: str, api_key: str, session: requests.Session | None = None) -> GeocodeResult:
    query = (query or "").strip()
    if not query:
        raise SolarError("NO_ADDRESS", "Enter an address or postcode.")
    pc = normalise_postcode(query)
    s = session or requests
    r = s.get(
        GEOCODE_URL,
        params={"address": pc or query, "region": "uk", "components": "country:GB", "key": api_key},
        timeout=TIMEOUT,
    )
    data = r.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        msg = data.get("error_message") or f"Geocoding returned {status}."
        raise SolarError(f"GEOCODE_{status}", msg)
    top = data["results"][0]
    loc = top["geometry"]["location"]
    return GeocodeResult(
        query=query,
        lat=loc["lat"],
        lng=loc["lng"],
        formatted_address=top.get("formatted_address", ""),
        location_type=top["geometry"].get("location_type", ""),
        partial_match=bool(top.get("partial_match")),
        is_postcode_only=bool(pc) or "postal_code" in top.get("types", []),
        types=top.get("types", []),
    )


def building_insights(
    lat: float, lng: float, api_key: str, session: requests.Session | None = None
) -> tuple[dict, str]:
    """Return (buildingInsights JSON, coverage tier). Raises SolarError on failure."""
    s = session or requests
    for tier, extra in COVERAGE_TIERS:
        r = s.get(
            SOLAR_URL,
            params={"location.latitude": f"{lat:.7f}", "location.longitude": f"{lng:.7f}", "key": api_key, **extra},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return r.json(), tier
        if r.status_code == 404:
            continue  # no building within ~50 m at this tier -> try next tier
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:
            msg = r.text
        hint = {
            403: " Check the key is allowed to call the Solar API and that billing is on.",
            429: " Quota hit. Wait a minute or raise the quota in Google Cloud.",
            400: " Bad request; usually a malformed coordinate.",
        }.get(r.status_code, "")
        raise SolarError(f"SOLAR_{r.status_code}", f"{msg}{hint}")
    raise SolarError(
        "NOT_FOUND",
        "Google has no building within ~50 m of this point at any coverage tier. "
        "Move the pin onto the roof, or survey it manually (ground-mounted sites are never covered).",
    )


# ----------------------------------------------------------------------------- extraction
def extract(bi: dict, geo: GeocodeResult | None, tier: str, today: dt.date | None = None) -> dict:
    """Flatten buildingInsights into HubSpot props + display extras."""
    sp = bi.get("solarPotential", {}) or {}
    segs = sp.get("roofSegmentStats", []) or []
    configs = sp.get("solarPanelConfigs", []) or []
    dom = max(segs, key=lambda s: (s.get("stats") or {}).get("areaMeters2", 0), default={})
    yearly = max((c.get("yearlyEnergyDcKwh", 0) or 0 for c in configs), default=0)
    today = today or dt.date.today()

    props = {
        "solar_data_available": "true",
        "solar_status": "OK",
        "solar_imagery_quality": bi.get("imageryQuality", ""),
        "solar_geocode_confidence": geo.location_type if geo else "MANUAL_PIN",
        "solar_roof_area_m2": _r((sp.get("wholeRoofStats") or {}).get("areaMeters2")),
        "solar_usable_area_m2": _r(sp.get("maxArrayAreaMeters2")),
        "solar_max_panel_count": sp.get("maxArrayPanelsCount"),
        "solar_yearly_kwh_potential": round(yearly) if yearly else None,
        "solar_roof_segment_count": len(segs),
        "solar_dominant_pitch_deg": _r(dom.get("pitchDegrees")),
        "solar_dominant_azimuth_deg": _r(dom.get("azimuthDegrees")),
        "solar_max_sunshine_hours": round(sp.get("maxSunshineHoursPerYear") or 0),
        "solar_carbon_offset_kg_mwh": _r(sp.get("carbonOffsetFactorKgPerMwh") or 0),
        "solar_enriched_at": today.isoformat(),  # HubSpot import accepts YYYY-MM-DD for date props
    }

    panel_w = sp.get("panelCapacityWatts") or 400
    extras = {
        "formatted_address": geo.formatted_address if geo else "",
        "query_lat": geo.lat if geo else None,
        "query_lng": geo.lng if geo else None,
        "building_lat": (bi.get("center") or {}).get("latitude"),
        "building_lng": (bi.get("center") or {}).get("longitude"),
        "imagery_date": _date(bi.get("imageryDate")),
        "coverage_tier": tier,
        "panel_capacity_w": panel_w,
        "panel_height_m": sp.get("panelHeightMeters"),
        "panel_width_m": sp.get("panelWidthMeters"),
        "max_system_kwp": round((sp.get("maxArrayPanelsCount") or 0) * panel_w / 1000, 2),
        "roof_ground_footprint_m2": _r((sp.get("wholeRoofStats") or {}).get("groundAreaMeters2")),
        "dominant_orientation": compass(dom.get("azimuthDegrees")),
        "bounding_box": bi.get("boundingBox"),
    }
    segments = [
        {
            "segment": i,
            "area_m2": _r((s.get("stats") or {}).get("areaMeters2")),
            "ground_area_m2": _r((s.get("stats") or {}).get("groundAreaMeters2")),
            "pitch_deg": _r(s.get("pitchDegrees")),
            "azimuth_deg": _r(s.get("azimuthDegrees")),
            "faces": compass(s.get("azimuthDegrees")),
            "max_sun_hours": _r(((s.get("stats") or {}).get("sunshineQuantiles") or [None])[-1], 0),
            "lat": (s.get("center") or {}).get("latitude"),
            "lng": (s.get("center") or {}).get("longitude"),
        }
        for i, s in enumerate(segs)
    ]
    panel_configs = [
        {"panels": c.get("panelsCount"), "yearly_kwh_dc": round(c.get("yearlyEnergyDcKwh", 0))} for c in configs
    ]
    return {"props": props, "extras": extras, "segments": segments, "panel_configs": panel_configs}


def failure_props(status: str, geo: GeocodeResult | None = None, today: dt.date | None = None) -> dict:
    """HubSpot props for a failed lookup, so the CSV still imports cleanly."""
    props = {k: None for k in HUBSPOT_SITE_PROPS}
    props.update(
        solar_data_available="false",
        solar_status=status,
        solar_geocode_confidence=geo.location_type if geo else "",
        solar_enriched_at=(today or dt.date.today()).isoformat(),
    )
    return props


def estimate(panel_configs: list[dict], panels: int, native_w: float, chosen_w: float, derate: float) -> dict:
    """Energy for a chosen panel count, rescaled to a different panel wattage and DC->AC derate."""
    cfg = min(panel_configs, key=lambda c: abs((c["panels"] or 0) - panels)) if panel_configs else None
    if not cfg:
        return {"panels": 0, "kwp": 0, "yearly_kwh_dc": 0, "yearly_kwh_ac": 0}
    scale = chosen_w / native_w if native_w else 1
    dc = cfg["yearly_kwh_dc"] * scale
    return {
        "panels": cfg["panels"],
        "kwp": round(cfg["panels"] * chosen_w / 1000, 2),
        "yearly_kwh_dc": round(dc),
        "yearly_kwh_ac": round(dc * derate),
    }


def lookup(query: str, api_key: str, session: requests.Session | None = None) -> dict:
    """Address/postcode -> full result. Never raises; failures come back with props filled in."""
    geo = None
    try:
        geo = geocode(query, api_key, session)
        bi, tier = building_insights(geo.lat, geo.lng, api_key, session)
        out = extract(bi, geo, tier)
        out["geo"] = geo
        out["raw"] = bi
        return out
    except SolarError as e:
        return {"props": failure_props(e.status, geo), "error": e.message, "geo": geo}
