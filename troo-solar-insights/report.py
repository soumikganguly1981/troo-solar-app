"""
report.py — one-page-plus PDF "Solar roof assessment" for a Troo Solar Insights result.

- fetch_base_map(): Esri World Imagery export (no key) for a Web-Mercator extent
- render_map():     draws building outline, roof-segment markers, lookup pin and a scale bar
- build_pdf():      ReportLab document -> bytes
"""
from __future__ import annotations

import datetime as dt
import io
import math

import requests
from PIL import Image, ImageDraw, ImageFont
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

R = 6378137.0
MAP_W, MAP_H = 1200, 780
BRAND = colors.HexColor("#0B3D5C")
ACCENT = colors.HexColor("#F2A900")


# ----------------------------------------------------------------------------- map geometry
def merc(lat: float, lng: float) -> tuple[float, float]:
    return R * math.radians(lng), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def map_extent(result: dict, pin: tuple[float, float] | None) -> tuple[float, float, float, float]:
    """Mercator extent (xmin, ymin, xmax, ymax) around the building, padded, aspect = MAP_W:MAP_H."""
    x = result["extras"]
    pts = []
    bb = x.get("bounding_box") or {}
    if bb.get("sw") and bb.get("ne"):
        pts += [merc(bb["sw"]["latitude"], bb["sw"]["longitude"]), merc(bb["ne"]["latitude"], bb["ne"]["longitude"])]
    if x.get("building_lat") is not None:
        pts.append(merc(x["building_lat"], x["building_lng"]))
    if pin:
        pts.append(merc(*pin))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    lat = x.get("building_lat") or (pin[0] if pin else 52)
    k = 1 / math.cos(math.radians(lat))                       # ground metres -> mercator metres
    half_w = max((max(xs) - min(xs)) / 2 * 1.6, 35 * k)        # pad 60%, at least ~70 m across
    half_h = max((max(ys) - min(ys)) / 2 * 1.6, half_w * MAP_H / MAP_W)
    half_w = max(half_w, half_h * MAP_W / MAP_H)
    half_h = half_w * MAP_H / MAP_W
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h


TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE = 256
MAX_TILE_ZOOM = 19
HEADERS = {"User-Agent": "TrooSolarInsights/1.0 (internal ES tool)"}
WORLD = 2 * math.pi * R


def tile_coords(mx: float, my: float, z: int) -> tuple[float, float]:
    """Mercator metres -> fractional slippy-map tile coordinates at zoom z."""
    n = 2 ** z
    return (mx + WORLD / 2) / WORLD * n, (WORLD / 2 - my) / WORLD * n


def pick_zoom(extent) -> int:
    width_m = extent[2] - extent[0]
    z = math.ceil(math.log2(WORLD * MAP_W / (TILE * width_m)))
    return max(1, min(MAX_TILE_ZOOM, z))


def _get_tile(s, z, x, y):
    try:
        r = s.get(TILE_URL.format(z=z, x=x, y=y), headers=HEADERS, timeout=10)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    return None


def fetch_base_map(extent, session: requests.Session | None = None) -> bytes | None:
    """Stitch Esri World Imagery tiles covering the extent, crop and resize to MAP_W x MAP_H.

    Uses the same tile service as the on-screen map. Returns None if imagery can't be fetched.
    """
    from concurrent.futures import ThreadPoolExecutor

    s = session or requests
    xmin, ymin, xmax, ymax = extent
    for z in (pick_zoom(extent), pick_zoom(extent) - 1):
        fx0, fy0 = tile_coords(xmin, ymax, z)  # top-left
        fx1, fy1 = tile_coords(xmax, ymin, z)  # bottom-right
        tx0, ty0, tx1, ty1 = int(fx0), int(fy0), int(fx1), int(fy1)
        tiles = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
        if len(tiles) > 80:
            continue
        with ThreadPoolExecutor(max_workers=8) as pool:
            imgs = list(pool.map(lambda t: _get_tile(s, z, *t), tiles))
        if sum(i is None for i in imgs) > len(imgs) // 4:   # too many gaps -> try one zoom out
            continue
        canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (225, 228, 232))
        for (x, y), im in zip(tiles, imgs):
            if im is not None:
                canvas.paste(im.resize((TILE, TILE)), ((x - tx0) * TILE, (y - ty0) * TILE))
        box = ((fx0 - tx0) * TILE, (fy0 - ty0) * TILE, (fx1 - tx0) * TILE, (fy1 - ty0) * TILE)
        out = io.BytesIO()
        canvas.crop(tuple(round(v) for v in box)).resize((MAP_W, MAP_H), Image.LANCZOS).save(out, "PNG")
        return out.getvalue()
    return None


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_map(base_png: bytes | None, extent, result: dict, pin: tuple[float, float] | None) -> bytes:
    """Overlay building box, segments, pin, scale bar and attribution on the base image."""
    xmin, ymin, xmax, ymax = extent
    if base_png:
        img = Image.open(io.BytesIO(base_png)).convert("RGB").resize((MAP_W, MAP_H))
    else:
        img = Image.new("RGB", (MAP_W, MAP_H), (225, 228, 232))
    d = ImageDraw.Draw(img, "RGBA")

    def px(lat, lng):
        mx, my = merc(lat, lng)
        return (mx - xmin) / (xmax - xmin) * MAP_W, (ymax - my) / (ymax - ymin) * MAP_H

    bb = result["extras"].get("bounding_box") or {}
    if bb.get("sw") and bb.get("ne"):
        x1, y1 = px(bb["ne"]["latitude"], bb["sw"]["longitude"])
        x2, y2 = px(bb["sw"]["latitude"], bb["ne"]["longitude"])
        d.rectangle([x1, y1, x2, y2], outline=(242, 169, 0, 255), width=5)

    f_small = _font(22)
    for s in result["segments"]:
        if s.get("lat") is None:
            continue
        x, y = px(s["lat"], s["lng"])
        good = s["faces"] in ("S", "SE", "SW")
        fill = (76, 175, 80, 235) if good else (189, 189, 189, 235)
        d.ellipse([x - 16, y - 16, x + 16, y + 16], fill=fill, outline=(255, 255, 255, 255), width=3)
        label = str(s["segment"])
        tw = d.textlength(label, font=f_small)
        d.text((x - tw / 2, y - 12), label, fill=(0, 0, 0, 255), font=f_small)

    if pin:
        x, y = px(*pin)
        d.polygon([(x, y), (x - 13, y - 30), (x + 13, y - 30)], fill=(211, 47, 47, 255), outline=(255, 255, 255, 255))
        d.ellipse([x - 15, y - 46, x + 15, y - 16], fill=(211, 47, 47, 255), outline=(255, 255, 255, 255), width=3)

    # scale bar (ground metres)
    lat = result["extras"].get("building_lat") or (pin[0] if pin else 52)
    m_per_px = (xmax - xmin) / MAP_W * math.cos(math.radians(lat))
    bar_m = next(v for v in (5, 10, 20, 25, 50, 100, 200) if v / m_per_px >= 110)
    bar_px = bar_m / m_per_px
    f = _font(24)
    d.rectangle([20, MAP_H - 62, 40 + bar_px, MAP_H - 18], fill=(255, 255, 255, 210))
    d.rectangle([30, MAP_H - 30, 30 + bar_px, MAP_H - 24], fill=(0, 0, 0, 255))
    d.text((30, MAP_H - 58), f"{bar_m} m", fill=(0, 0, 0, 255), font=f)

    # north arrow
    d.polygon([(MAP_W - 45, 25), (MAP_W - 60, 65), (MAP_W - 30, 65)], fill=(255, 255, 255, 230), outline=(0, 0, 0, 255))
    d.text((MAP_W - 52, 68), "N", fill=(255, 255, 255, 255), font=f, stroke_width=2, stroke_fill=(0, 0, 0, 255))

    attr = "Imagery: Esri, Maxar, Earthstar Geographics" if base_png else "Satellite imagery unavailable"
    fa = _font(18)
    tw = d.textlength(attr, font=fa)
    d.rectangle([MAP_W - tw - 16, MAP_H - 30, MAP_W, MAP_H], fill=(255, 255, 255, 200))
    d.text((MAP_W - tw - 8, MAP_H - 27), attr, fill=(40, 40, 40, 255), font=fa)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


# ----------------------------------------------------------------------------- PDF
def _fmt(v, unit="", nd=None):
    if v is None or v == "":
        return "—"
    if isinstance(v, (int, float)):
        v = f"{v:,.{nd}f}" if nd is not None else f"{v:,}"
    return f"{v}{unit}"


def _chart(panel_configs: list[dict], chosen: int | None) -> Drawing:
    w, h = 80 * mm, 46 * mm
    d = Drawing(w, h)
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 14 * mm, 8 * mm, w - 18 * mm, h - 16 * mm
    lp.data = [[(c["panels"], c["yearly_kwh_dc"]) for c in panel_configs]]
    lp.lines[0].strokeColor = BRAND
    lp.lines[0].strokeWidth = 1.6
    lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 7
    lp.yValueAxis.labelTextFormat = lambda v: f"{v:,.0f}"
    lp.yValueAxis.valueMin = 0
    if chosen:
        pt = min(panel_configs, key=lambda c: abs(c["panels"] - chosen))
        lp.data.append([(pt["panels"], pt["yearly_kwh_dc"])])
        lp.lines[1].strokeColor = ACCENT
        from reportlab.graphics.widgets.markers import makeMarker
        lp.lines[1].symbol = makeMarker("FilledCircle", size=6, fillColor=ACCENT)
    d.add(lp)
    d.add(String(14 * mm, h - 5 * mm, "Yearly kWh (DC) by number of panels", fontSize=7.5, fillColor=colors.grey))
    return d


def build_pdf(result: dict, sizing: dict | None, settings: dict | None, map_png: bytes | None,
              address: str = "", pin: tuple[float, float] | None = None) -> bytes:
    p, x = result["props"], result["extras"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm,
                            title="Solar roof assessment", author="Troo Energy Services")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, textColor=BRAND, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], textColor=BRAND, spaceBefore=8, spaceAfter=4)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#555555"), leading=10)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9, leading=12)

    def kv_table(rows, cols=2):
        cells, row = [], []
        for label, val in rows:
            row += [Paragraph(f"<font color='#666666' size=8>{label}</font>", body),
                    Paragraph(f"<b>{val}</b>", body)]
            if len(row) == cols * 2:
                cells.append(row)
                row = []
        if row:
            cells.append(row + [""] * (cols * 2 - len(row)))
        widths = [32 * mm, 58 * mm] * cols if cols == 2 else [36 * mm, 48 * mm]
        t = Table(cells, colWidths=widths)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        return t

    el = []
    el.append(Paragraph("Solar roof assessment", h1))
    el.append(Paragraph(f"Troo Energy Services · generated {dt.date.today():%d %b %Y} · Google Solar API", small))
    el.append(Spacer(1, 4 * mm))

    loc = pin or (x.get("query_lat"), x.get("query_lng"))
    el.append(kv_table([
        ("Site", address or x.get("formatted_address") or "—"),
        ("Lookup point", f"{loc[0]:.6f}, {loc[1]:.6f}" if loc and loc[0] is not None else "—"),
        ("Location accuracy", p.get("solar_geocode_confidence") or "—"),
        ("Imagery", f"{p.get('solar_imagery_quality') or '—'} · captured {x.get('imagery_date') or '—'}"),
    ]))

    if map_png:
        el.append(Spacer(1, 3 * mm))
        el.append(RLImage(io.BytesIO(map_png), width=158 * mm, height=158 * mm * MAP_H / MAP_W))
        el.append(Paragraph("Yellow box = building matched by Google · numbered dots = roof segments "
                            "(green faces S/SE/SW) · red pin = lookup point", small))

    if x.get("coverage_tier") == "EXPANDED":
        el.append(Paragraph("<font color='#B26A00'><b>Indicative only:</b> building found only in Google's "
                            "experimental expanded-coverage tier (BASE quality). Verify on site.</font>", body))

    potential = [Paragraph("Roof potential", h2), kv_table([
        ("Roof area", _fmt(p["solar_roof_area_m2"], " m<super>2</super>")),
        ("Usable for panels", _fmt(p["solar_usable_area_m2"], " m<super>2</super>")),
        ("Max panels", _fmt(p["solar_max_panel_count"])),
        ("Max system", _fmt(x.get("max_system_kwp"), " kWp")),
        ("Yearly potential (DC)", _fmt(p["solar_yearly_kwh_potential"], " kWh")),
        ("Max sun hours / yr", _fmt(p["solar_max_sunshine_hours"])),
        ("Roof segments", _fmt(p["solar_roof_segment_count"])),
        ("Main roof", f"{_fmt(p['solar_dominant_pitch_deg'], '°')} pitch, faces "
                      f"{x.get('dominant_orientation') or '—'} ({_fmt(p['solar_dominant_azimuth_deg'], '°')})"),
    ], cols=1)]

    sizing_block = ""
    if sizing and sizing.get("panels"):
        st_ = settings or {}
        co2 = sizing["yearly_kwh_ac"] * (p.get("solar_carbon_offset_kg_mwh") or 0) / 1e6
        sizing_block = [Paragraph("Sizing estimate", h2), kv_table([
            ("Panels", _fmt(sizing["panels"])),
            ("Panel wattage", _fmt(st_.get("panel_w"), " W")),
            ("DC-to-AC derate", _fmt(st_.get("derate"), "", 2)),
            ("System size", _fmt(sizing["kwp"], " kWp")),
            ("Yearly generation (AC est.)", _fmt(sizing["yearly_kwh_ac"], " kWh")),
            ("CO<sub>2</sub> avoided / yr", f"{co2:,.1f} t"),
        ], cols=1)]

    duo = Table([[potential, sizing_block]], colWidths=[92 * mm, 88 * mm])
    duo.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    el.append(duo)

    seg_tbl = None
    if result.get("segments"):
        rows = [["#", "Area m2", "Pitch", "Azimuth", "Faces", "Sun h/yr"]]
        for s_ in result["segments"]:
            rows.append([s_["segment"], _fmt(s_["area_m2"]), _fmt(s_["pitch_deg"], "°"), _fmt(s_["azimuth_deg"], "°"),
                         s_["faces"], _fmt(s_["max_sun_hours"], "", 0)])
        seg_tbl = Table(rows, colWidths=[8 * mm, 18 * mm, 15 * mm, 18 * mm, 13 * mm, 18 * mm], repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), BRAND), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("FONTSIZE", (0, 0), (-1, -1), 8), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F8")]),
                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        for i, s_ in enumerate(result["segments"], start=1):
            if s_["faces"] in ("S", "SE", "SW"):
                style.append(("TEXTCOLOR", (4, i), (4, i), colors.HexColor("#2E7D32")))
        seg_tbl.setStyle(TableStyle(style))

    chart = None
    if result.get("panel_configs") and len(result["panel_configs"]) > 1:
        chart = _chart(result["panel_configs"], sizing.get("panels") if sizing else None)

    if seg_tbl or chart:
        pair = Table([[Paragraph("Roof segments", h2) if seg_tbl else "", Paragraph("Output by system size", h2) if chart else ""],
                      [seg_tbl or "", chart or ""]], colWidths=[96 * mm, 84 * mm])
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        el.append(KeepTogether([pair]))

    el.append(Spacer(1, 2 * mm))
    el.append(Paragraph(
        "<b>Notes.</b> Pre-survey estimate from the Google Solar API, not a system design. Covers rooftops only; "
        "ground-mounted arrays need a site survey. Yearly potential is Google's DC figure for its maximum panel "
        f"layout using {x.get('panel_capacity_w', 400)} W panels; the sizing estimate rescales to the chosen "
        "wattage and applies the DC-to-AC derate. Shading, structure, roof condition and grid constraints are not "
        "assessed.", small))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, "Troo Energy Services · Solar roof assessment")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(el, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
