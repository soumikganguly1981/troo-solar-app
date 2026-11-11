"""Offline tests for the PDF report (no network: base map is synthetic)."""
import io
import math
import json
import pathlib

from PIL import Image

import report
import solar_core as sc

FIX = json.loads((pathlib.Path(__file__).parent / "fixture_building_insights.json").read_text())


def _result():
    return sc.extract(FIX, None, "STANDARD")


def test_extent_aspect_and_contains_building():
    res = _result()
    xmin, ymin, xmax, ymax = report.map_extent(res, (53.78, -1.53))
    assert abs((xmax - xmin) / (ymax - ymin) - report.MAP_W / report.MAP_H) < 1e-6
    bx, by = report.merc(res["extras"]["building_lat"], res["extras"]["building_lng"])
    assert xmin < bx < xmax and ymin < by < ymax


def test_render_map_without_base_image():
    res = _result()
    ext = report.map_extent(res, None)
    png = report.render_map(None, ext, res, None)
    assert Image.open(io.BytesIO(png)).size == (report.MAP_W, report.MAP_H)


def test_build_pdf_bytes():
    res = _result()
    ext = report.map_extent(res, (53.78, -1.53))
    base = io.BytesIO()
    Image.new("RGB", (report.MAP_W, report.MAP_H), (60, 90, 60)).save(base, "PNG")
    png = report.render_map(base.getvalue(), ext, res, (53.78, -1.53))
    sizing = sc.estimate(res["panel_configs"], 120, 400, 430, 0.85)
    pdf = report.build_pdf(res, sizing, {"panel_w": 430, "derate": 0.85}, png, "Test Works, Leeds", (53.78, -1.53))
    assert pdf[:5] == b"%PDF-" and len(pdf) > 10_000


def test_fetch_base_map_failure_returns_none():
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("offline")
    assert report.fetch_base_map((0, 0, 1, 1), Boom()) is None


def test_tile_coords_match_slippy_formula():
    lat, lng, z = 54.859168, -1.429621, 19
    mx, my = report.merc(lat, lng)
    fx, fy = report.tile_coords(mx, my, z)
    n = 2 ** z
    exp_x = (lng + 180) / 360 * n
    lr = math.radians(lat)
    exp_y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
    assert abs(fx - exp_x) < 1e-6 and abs(fy - exp_y) < 1e-6


def test_fetch_base_map_stitches_tiles():
    calls = []

    class Tiles:
        def get(self, url, headers=None, timeout=None):
            calls.append(url)
            b = io.BytesIO()
            Image.new("RGB", (256, 256), (10, 120, 30)).save(b, "JPEG")

            class R:
                ok = True
                headers = {"content-type": "image/jpeg"}
                content = b.getvalue()
            return R()

    res = _result()
    ext = report.map_extent(res, (53.78, -1.53))
    png = report.fetch_base_map(ext, Tiles())
    img = Image.open(io.BytesIO(png))
    assert img.size == (report.MAP_W, report.MAP_H)
    assert 1 <= len(calls) <= 80 and "/tile/" in calls[0]
    assert img.getpixel((600, 390))[1] > 100   # green tile content, not grey placeholder


def test_fetch_base_map_all_tiles_fail():
    class Dead:
        def get(self, *a, **k):
            class R:
                ok = False
                headers = {}
            return R()
    assert report.fetch_base_map(report.map_extent(_result(), None), Dead()) is None
