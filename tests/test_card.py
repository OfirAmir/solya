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
    assert _near(_open(_data(ray=95)).getpixel((540, 85)), GREEN)


def test_ray_score_drawn():
    im = _open(_data(ray=95))
    assert any(
        im.getpixel((x, y)) == (255, 255, 255)
        for x in range(500, 581)
        for y in range(150, 231)
    )


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


def test_card_renders_status_labels_header_footnote():
    rows = [
        CardRow("אורן סופר", 316990.0, Decimal("142.65"), None,
                income_label="הכנסה", status=("ok", "אין התראות פעילות")),
        CardRow("IL 360", 85039.0, Decimal("38.03"), None,
                income_label="הכנסה מהזרמה", status=("fault", "התראה פתוחה: FAN_ALERT_1")),
    ]
    plain = CardData(ray=None, rows=rows, total=None)
    full = CardData(
        ray=None, rows=rows, total=None, header="רביעי, 25 בנובמבר · נכון ל־17:01",
        site_count=2, footnote="הכנסה משוערת לפני מע״מ",
    )
    png = render_card(full)
    assert png.startswith(b"\x89PNG")
    assert _open(full).height > _open(plain).height
