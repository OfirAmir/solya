"""Fixed-template Hebrew PNG card: numbers poured into a template, never generated."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from solya.rules.ray import ray_band
from solya.text.he import fmt_ils, fmt_kwh

RAY_COLORS = {"green": "#2E9E5B", "orange": "#F28C28", "yellow": "#E6C229", "red": "#D64545"}
BACKGROUND = "#FFFFFF"
GREY = "#8A8A8A"
INK = "#1F1F1F"
RULE = "#E3E3E3"

WIDTH = 1080
MARGIN = 60
FONT_PATH = Path(__file__).parent / "fonts" / "Heebo[wght].ttf"
TOTAL_LABEL = "סה״כ"

RAY_DIAMETER = 260
RAY_CENTER = (540, 190)
TOP_NO_RAY = 60
TOP_WITH_RAY = RAY_CENTER[1] + RAY_DIAMETER // 2 + 50
ROW_HEIGHT = 150
FOOTER_HEIGHT = 90


@dataclass(frozen=True)
class CardRow:
    name: str
    production_wh: float | None
    income_ils: Decimal | None
    note: str | None


@dataclass(frozen=True)
class CardData:
    ray: float | None
    rows: list[CardRow]
    total: tuple[float, Decimal] | None
    date_label: str


def assert_raqm() -> None:
    if not features.check("raqm"):
        raise RuntimeError("Pillow Raqm layout unavailable; install libfribidi0")


def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size, layout_engine=ImageFont.Layout.RAQM)
    font.set_variation_by_axes([weight])
    return font


def _text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, anchor: str) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=anchor, direction="rtl", language="he")


def _draw_row(draw: ImageDraw.ImageDraw, y: int, row: CardRow, fonts, bold: bool = False) -> None:
    small, big, note = fonts
    _text(draw, (WIDTH - MARGIN, y), row.name, small, INK if bold else GREY, "ra")
    base = y + 52
    if row.note is not None:
        _text(draw, (WIDTH - MARGIN, base + 8), row.note, note, INK, "ra")
        return
    if row.production_wh is not None:
        _text(draw, (WIDTH - MARGIN, base), fmt_kwh(row.production_wh), big, INK, "ra")
    if row.income_ils is not None:
        _text(draw, (MARGIN, base), fmt_ils(row.income_ils), big, INK, "la")


def render_card(data: CardData) -> bytes:
    assert_raqm()
    fonts = (_font(34, 400), _font(56, 700), _font(44, 700))
    top = TOP_WITH_RAY if data.ray is not None else TOP_NO_RAY
    n_rows = len(data.rows) + (1 if data.total else 0)
    height = top + n_rows * ROW_HEIGHT + FOOTER_HEIGHT

    img = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(img)

    if data.ray is not None:
        cx, cy = RAY_CENTER
        r = RAY_DIAMETER // 2
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=RAY_COLORS[ray_band(data.ray)])
        draw.text(RAY_CENTER, str(round(data.ray)), font=_font(110, 700), fill="#FFFFFF", anchor="mm")
        draw.text((cx, cy + r + 28), "RAY FACTOR", font=_font(30, 400), fill=GREY, anchor="mm")

    y = top
    for row in data.rows:
        _draw_row(draw, y, row, fonts)
        y += ROW_HEIGHT
        draw.line((MARGIN, y - 20, WIDTH - MARGIN, y - 20), fill=RULE, width=2)
    if data.total:
        wh, ils = data.total
        _draw_row(draw, y, CardRow(TOTAL_LABEL, wh, ils, None), fonts, bold=True)
        y += ROW_HEIGHT
    _text(draw, (WIDTH - MARGIN, y), data.date_label, fonts[0], GREY, "ra")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _sample(ray: float | None) -> CardData:
    return CardData(
        ray=ray,
        rows=[
            CardRow("אורן סופר", 316990.0, Decimal("142.65"), None),
            CardRow("IL 360", 85039.0, Decimal("38.03"), None),
        ],
        total=(402029.0, Decimal("180.68")),
        date_label="24.11.2026",
    )


if __name__ == "__main__":
    out = Path("out")
    out.mkdir(exist_ok=True)
    (out / "card_sample.png").write_bytes(render_card(_sample(None)))
    (out / "card_sample_ray.png").write_bytes(render_card(_sample(95)))
    print("wrote", out / "card_sample.png", out / "card_sample_ray.png")
