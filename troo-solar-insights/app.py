"""
Troo Solar Insights — Streamlit front end for the Google Solar API.

Single lookup: address or UK postcode -> geocode -> confirm pin on satellite map -> roof metrics.
Batch: CSV of addresses -> HubSpot-ready CSV (Site object property names).

Run locally:  streamlit run app.py
Secrets:      .streamlit/secrets.toml  (see secrets.toml.example)
"""
from __future__ import annotations

import hmac
import io
import json
import os

import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

import solar_core as sc

st.set_page_config(page_title="Troo Solar Insights", page_icon="☀️", layout="wide")


# ----------------------------------------------------------------------------- config & auth
def secret(name: str, default=None):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:  # no secrets.toml present
        return os.getenv(name, default)


API_KEY = secret("GOOGLE_MAPS_API_KEY")
APP_PASSWORD = secret("APP_PASSWORD")
MAX_BATCH = int(secret("MAX_BATCH_ROWS", 300))


def gate() -> bool:
    if not APP_PASSWORD:
        return True
    if st.session_state.get("authed"):
        return True
    st.title("☀️ Troo Solar Insights")
    pw = st.text_input("Team password", type="password")
    if pw and hmac.compare_digest(pw, str(APP_PASSWORD)):
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    return False


if not gate():
    st.stop()

if not API_KEY:
    st.error("No `GOOGLE_MAPS_API_KEY` configured. Add it to `.streamlit/secrets.toml` or the app's Secrets.")
    st.stop()


# ----------------------------------------------------------------------------- cached API wrappers
# Caching keeps repeat lookups free: same address / same pin within 7 days isn't re-billed.
@st.cache_resource
def http() -> requests.Session:
    return requests.Session()


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def cached_geocode(query: str) -> sc.GeocodeResult:
    return sc.geocode(query, API_KEY, http())


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def cached_insights(lat: float, lng: float) -> tuple[dict, str]:
    return sc.building_insights(lat, lng, API_KEY, http())


def cached_lookup(query: str) -> dict:
    geo = None
    try:
        geo = cached_geocode(query)
        bi, tier = cached_insights(round(geo.lat, 6), round(geo.lng, 6))
        out = sc.extract(bi, geo, tier)
        out["geo"] = geo
        return out
    except sc.SolarError as e:
        return {"props": sc.failure_props(e.status, geo), "error": e.message, "geo": geo}


# ----------------------------------------------------------------------------- map
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def build_map(lat, lng, result: dict | None):
    m = folium.Map(location=[lat, lng], zoom_start=19, max_zoom=21, tiles=None, control_scale=True)
    folium.TileLayer(ESRI, attr="Esri, Maxar, Earthstar Geographics", name="Satellite", max_zoom=21,
                     max_native_zoom=19).add_to(m)
    folium.Marker([lat, lng], tooltip="Lookup point", icon=folium.Icon(color="red", icon="map-pin", prefix="fa")).add_to(m)
    if result and "extras" in result:
        bb = result["extras"].get("bounding_box") or {}
        if bb.get("sw") and bb.get("ne"):
            folium.Rectangle(
                [[bb["sw"]["latitude"], bb["sw"]["longitude"]], [bb["ne"]["latitude"], bb["ne"]["longitude"]]],
                color="#FFC107", weight=2, fill=False, tooltip="Building Google matched",
            ).add_to(m)
        for s in result["segments"]:
            if s["lat"] is None:
                continue
            good = s["faces"] in ("S", "SE", "SW")
            folium.CircleMarker(
                [s["lat"], s["lng"]], radius=6, weight=1, fill=True, fill_opacity=0.9,
                color="#1b5e20" if good else "#555", fill_color="#4caf50" if good else "#bbb",
                tooltip=f"Segment {s['segment']}: {s['area_m2']} m², pitch {s['pitch_deg']}°, faces {s['faces']}",
            ).add_to(m)
    return m


# ----------------------------------------------------------------------------- result view
def show_result(res: dict, key_prefix: str):
    if "error" in res:
        st.error(f"**{res['props']['solar_status']}**: {res['error']}")
        return
    p, x = res["props"], res["extras"]

    if x["coverage_tier"] == "EXPANDED":
        st.warning("Only the experimental expanded-coverage tier (BASE quality, from satellite imagery) had this building. "
                   "Treat the figures as indicative and check them on site.")

    c = st.columns(5)
    c[0].metric("Roof area", f"{p['solar_roof_area_m2']:,} m²")
    c[1].metric("Usable for panels", f"{p['solar_usable_area_m2']:,} m²")
    c[2].metric("Max panels", f"{p['solar_max_panel_count']:,}")
    c[3].metric("Max system", f"{x['max_system_kwp']:,} kWp")
    c[4].metric("Yearly potential (DC)", f"{(p['solar_yearly_kwh_potential'] or 0):,} kWh")

    c = st.columns(5)
    c[0].metric("Roof segments", p["solar_roof_segment_count"])
    c[1].metric("Main pitch", f"{p['solar_dominant_pitch_deg']}°")
    c[2].metric("Main faces", f"{x['dominant_orientation']} ({p['solar_dominant_azimuth_deg']}°)")
    c[3].metric("Max sun hours / yr", f"{p['solar_max_sunshine_hours']:,}")
    c[4].metric("Imagery quality", p["solar_imagery_quality"], delta=f"captured {x['imagery_date']}", delta_color="off")

    # --- sizing estimator
    if res["panel_configs"]:
        with st.expander("System sizing estimator", expanded=True):
            cfgs = res["panel_configs"]
            lo, hi = cfgs[0]["panels"], cfgs[-1]["panels"]
            a, b, d = st.columns(3)
            n = a.slider("Panels", lo, hi, hi, key=f"{key_prefix}_n") if hi > lo else lo
            w = b.number_input("Panel wattage (W)", 250, 700, 430, 5, key=f"{key_prefix}_w",
                               help=f"Google models {x['panel_capacity_w']} W panels; output is rescaled.")
            der = d.slider("DC→AC derate", 0.70, 0.95, 0.85, 0.01, key=f"{key_prefix}_d",
                           help="Inverter + wiring losses. 0.85 is Google's default assumption.")
            e = sc.estimate(cfgs, n, x["panel_capacity_w"], w, der)
            k = st.columns(3)
            k[0].metric("System size", f"{e['kwp']} kWp")
            k[1].metric("Yearly generation (AC est.)", f"{e['yearly_kwh_ac']:,} kWh")
            k[2].metric("CO₂ avoided / yr", f"{e['yearly_kwh_ac'] * p['solar_carbon_offset_kg_mwh'] / 1e6:,.1f} t")
            st.line_chart(pd.DataFrame(cfgs).set_index("panels")["yearly_kwh_dc"], height=200)

    with st.expander("Roof segments"):
        st.dataframe(pd.DataFrame(res["segments"]).drop(columns=["lat", "lng"]), hide_index=True, width="stretch")

    with st.expander("HubSpot Site properties (copy or download)"):
        st.json(p)
        st.download_button("Download as CSV", pd.DataFrame([p]).to_csv(index=False), "solar_site.csv",
                           key=f"{key_prefix}_csv")
    if "raw" in res:
        st.download_button("Raw API JSON", json.dumps(res["raw"], indent=2), "building_insights.json",
                           key=f"{key_prefix}_raw")


# ----------------------------------------------------------------------------- UI
st.title("☀️ Troo Solar Insights")
st.caption("Roof measurements from the Google Solar API. Rooftops only: ground-mounted sites still need a manual survey.")

tab_single, tab_batch, tab_help = st.tabs(["Single lookup", "Batch (CSV)", "How it works"])

with tab_single:
    with st.form("lookup"):
        q = st.text_input("Site address or UK postcode",
                          placeholder="e.g. Unit 4, Riverside Park, Leeds LS10 1AB  — or just LS10 1AB")
        go = st.form_submit_button("Look up", type="primary")
    if go and q:
        st.session_state.pop("pin", None)
        st.session_state.pop("result", None)
        with st.spinner("Geocoding…"):
            try:
                st.session_state.geo = cached_geocode(q)
            except sc.SolarError as e:
                st.session_state.geo = None
                st.error(f"**{e.status}**: {e.message}")

    geo: sc.GeocodeResult | None = st.session_state.get("geo")
    if geo:
        lat, lng = st.session_state.get("pin", (geo.lat, geo.lng))
        st.write(f"**{geo.formatted_address}** · geocode `{geo.location_type}`"
                 + (" · *manual pin*" if "pin" in st.session_state else ""))
        if not geo.precise and "pin" not in st.session_state:
            st.info("This point is **approximate** (postcode or partial match), so it may not be on the right building. "
                    "Click the actual roof on the map, then press **Measure this roof**.")

        left, right = st.columns([3, 2])
        with left:
            out = st_folium(build_map(lat, lng, st.session_state.get("result")), height=480,
                            use_container_width=True, returned_objects=["last_clicked"], key="map")
            clicked = (out or {}).get("last_clicked")
        with right:
            if clicked:
                st.write(f"Clicked: `{clicked['lat']:.6f}, {clicked['lng']:.6f}`")
                if st.button("📍 Use clicked point"):
                    st.session_state.pin = (clicked["lat"], clicked["lng"])
                    st.session_state.pop("result", None)
                    st.rerun()
            if st.button("Measure this roof", type="primary"):
                with st.spinner("Calling Solar API…"):
                    try:
                        bi, tier = cached_insights(round(lat, 6), round(lng, 6))
                        r = sc.extract(bi, geo, tier)
                        if "pin" in st.session_state:
                            r["props"]["solar_geocode_confidence"] = "MANUAL_PIN"
                        r["raw"] = bi
                        st.session_state.result = r
                    except sc.SolarError as e:
                        st.session_state.result = {"props": sc.failure_props(e.status, geo), "error": e.message}
                st.rerun()
            st.caption("Yellow box = the building Google matched. Green dots = roof faces pointing S/SE/SW.")

        if st.session_state.get("result"):
            st.divider()
            show_result(st.session_state.result, "single")

with tab_batch:
    st.write(f"Upload a CSV with an **address** column (or **postcode**). Add a **record_id** column holding the HubSpot "
             f"Site record ID and the output imports straight onto existing Sites. Limit: {MAX_BATCH} rows per run.")
    st.download_button("Template CSV", "record_id,address\n,\"10 Downing Street, London SW1A 2AA\"\n",
                       "solar_batch_template.csv")
    f = st.file_uploader("CSV", type="csv")
    if f:
        df = pd.read_csv(f, dtype=str).fillna("")
        cols = {c.lower().strip(): c for c in df.columns}
        addr_col = cols.get("address") or cols.get("postcode") or cols.get("postal_code")
        if not addr_col:
            st.error("Need a column called `address` or `postcode`.")
        else:
            df = df.head(MAX_BATCH)
            postcode_only = df[addr_col].map(lambda s: sc.normalise_postcode(s) is not None).sum()
            st.write(f"{len(df)} rows · {postcode_only} are postcode-only (lower accuracy).")
            if st.button(f"Run {len(df)} lookups", type="primary"):
                rows, bar = [], st.progress(0.0)
                for i, q in enumerate(df[addr_col]):
                    r = cached_lookup(q)
                    x = r.get("extras", {})
                    rows.append({**r["props"],
                                 "matched_address": r["geo"].formatted_address if r.get("geo") else "",
                                 "coverage_tier": x.get("coverage_tier", ""),
                                 "max_system_kwp": x.get("max_system_kwp"),
                                 "imagery_date": x.get("imagery_date", ""),
                                 "postcode_only": bool(r.get("geo") and r["geo"].is_postcode_only)})
                    bar.progress((i + 1) / len(df), text=f"{i + 1}/{len(df)}")
                out = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
                ok = (out["solar_status"] == "OK").sum()
                st.success(f"{ok}/{len(out)} found. Postcode-only rows may point at the wrong building, so check those first.")
                st.dataframe(out, hide_index=True, width="stretch")
                buf = io.StringIO()
                out.to_csv(buf, index=False)
                st.download_button("Download HubSpot import CSV", buf.getvalue(), "solar_batch_results.csv", type="primary")

with tab_help:
    st.markdown(f"""
**Flow**: address or postcode → Google Geocoding (UK only) → `buildingInsights:findClosest` → metrics.

**Coverage fallback**: standard imagery (HIGH/MEDIUM/LOW) first. If nothing is found, it retries with the experimental
`EXPANDED_COVERAGE` tier at BASE quality, the same order as the HubSpot workflow action.
`NOT_FOUND` means Google has no building within ~50 m of the point.

**Postcode-only lookups** land on the postcode centroid, which can be the wrong building on an industrial estate.
In single mode, click the right roof. In batch mode, check rows flagged `postcode_only`.

**Numbers**: *Yearly potential* is Google's DC estimate for the maximum panel layout using its
standard panel model. The sizing estimator rescales it to your panel wattage and applies a DC→AC derate.
These are **pre-survey estimates**, not a design.

**Output columns** match the HubSpot Site properties (`solar_*`) used by the enrichment workflow action.

**Cost**: Building Insights has a free monthly allowance, and repeat lookups are cached here for 7 days.
""")
