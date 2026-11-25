from decimal import Decimal
from io import BytesIO

from PIL import Image

from solya.render.card import CardData, CardRow, assert_raqm, render_card

GREEN = (0x2E, 0x9E, 0x5B)


def _row(name="אורן סופר"):
    return CardRow(name, 316990.0, Decimal("142.65"), None)


def _data(ray=None, rows=None, total=True):
    rows = rows if rows is not None else [_row()]
    return CardData(
        ray=ray,
        rows=rows,
        total=(402029.0, Decimal("180.68")) if total else None,
        date_label="24.11.2026",
    )


def _open(data):
    return Image.open(BytesIO(render_card(data))).convert("RGB")


def _near(px, rgb, tol=10):
    return all(abs(a - b) <= tol for a, b in zip(px, rgb))


def test_raqm_available():
    assert_raqm()


def test_render_png_width():
    im = Image.open(BytesIO(render_card(_data())))
    assert im.format == "PNG"
    assert im.width == 1080


def test_ray_circle_color():
    assert _near(_open(_data(ray=95)).getpixel((540, 190)), GREEN)


def test_no_ray_no_circle():
    im = _open(_data(ray=None))
    assert not _near(im.getpixel((540, 190)), GREEN, tol=60)
    assert im.getpixel((540, 5)) == (255, 255, 255)


def test_comm_loss_row_renders():
    row = CardRow("IL 360", None, None, "אין תקשורת")
    assert _open(_data(rows=[_row(), row])).width == 1080


def test_height_grows_with_rows():
    one = _open(_data(rows=[_row()]))
    three = _open(_data(rows=[_row(), _row("IL 360"), _row("אחר")]))
    assert three.height > one.height
