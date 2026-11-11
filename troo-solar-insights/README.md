# Troo Solar Insights ☀️

A small internal web app for Energy Services. Enter a site **address or UK postcode** to get roof measurements from the
**Google Solar API**: roof area, usable area, max panels, kWp, yearly kWh, pitch, orientation and sun hours. There's
also a **batch mode** that takes a CSV and returns a file ready to import into HubSpot.

Replaces the "measure the roof in Google Maps and screenshot it" step.

## Features
- **Single lookup**: address or postcode → satellite map with the matched building outlined. If the geocode is
  approximate (postcode-only), click the correct roof and measure again.
- **Sizing estimator**: choose a panel count, panel wattage and DC→AC derate to get kWp, yearly kWh and CO₂ avoided.
- **Batch (CSV)**: columns `address` (or `postcode`) plus optional `record_id`. Output columns use the HubSpot Site
  property names (`solar_*`), so the file imports straight onto Site records (object `2-112673583`).
- **Coverage fallback**: standard imagery first, then `EXPANDED_COVERAGE` at BASE quality. This matches
  `hubspot_solar_enrichment_action.js`.
- **7-day cache**: repeat lookups don't call Google again. Optional shared-password gate.

> Rooftops only. The Solar API does not model ground-mounted arrays, so those still need a site survey.

## Repo layout
```
app.py              Streamlit UI
solar_core.py       Geocoding + Solar API + field mapping (no UI; reusable in Colab / Actions)
tests/              Offline tests with mocked Google APIs (no key needed)
.streamlit/secrets.toml.example
```

## 1. Google Cloud key (one-off)
1. In Google Cloud, pick the project and enable the **Solar API** and the **Geocoding API**. Billing must be on.
2. Go to Credentials → Create API key → **API restrictions**: Solar API and Geocoding API only.
3. Go to Quotas → set a **daily cap** on Building Insights (e.g. 500/day) as a guard against runaway costs.

The key stays on the server. It is never sent to the browser.

## 2. Run locally
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # paste your key + a password
streamlit run app.py
```

## 3. Deploy (free) from GitHub on Streamlit Community Cloud
1. Push this folder to a **private** GitHub repo, e.g. `troo-solar-insights`.
2. Go to share.streamlit.io → **Create app** → choose the repo, branch `main`, file `app.py`.
3. Open Advanced settings → **Secrets** and paste the contents of `secrets.toml` (key, `APP_PASSWORD`).
4. Deploy. Share the URL and password with the ES team (Dean, Kieran, Daniel).

On the free tier the app sleeps after a few days without use. The first visit afterwards takes ~30 s to wake it.

## Tests
```bash
python -m pytest -q
```
GitHub Actions runs these on every push (`.github/workflows/tests.yml`).

## Field reference (HubSpot Site properties)
| Property | Source |
|---|---|
| `solar_roof_area_m2` | `wholeRoofStats.areaMeters2` |
| `solar_usable_area_m2` | `maxArrayAreaMeters2` |
| `solar_max_panel_count` | `maxArrayPanelsCount` |
| `solar_yearly_kwh_potential` | max `solarPanelConfigs[].yearlyEnergyDcKwh` (DC) |
| `solar_roof_segment_count` | `roofSegmentStats.length` |
| `solar_dominant_pitch_deg` / `_azimuth_deg` | largest roof segment |
| `solar_max_sunshine_hours` | `maxSunshineHoursPerYear` |
| `solar_carbon_offset_kg_mwh` | `carbonOffsetFactorKgPerMwh` |
| `solar_imagery_quality` | HIGH / MEDIUM / LOW / BASE |
| `solar_geocode_confidence` | Geocoding `location_type`, or `MANUAL_PIN` |
| `solar_data_available` / `solar_status` | `true`/`false`; OK, NOT_FOUND, GEOCODE_*, SOLAR_* |
| `solar_enriched_at` | date of lookup (YYYY-MM-DD) |

Figures are pre-survey estimates, not a system design.
