"""Render the app icon (icon.svg; icon_small.svg, simplified, for 48 px and below) to PNGs
and a multi-size Windows icon.ico. Run after editing either SVG:

    QT_QPA_PLATFORM=offscreen ../../.venv/bin/python build_icons.py
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).parent
SIZES = (16, 32, 48, 64, 128, 256, 512)
SMALL_MAX = 48
ICO_SIZES = (16, 32, 48, 256)


def render(svg: Path, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(svg)).render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def ico(images: dict[int, bytes]) -> bytes:
    """An .ico holding PNG-compressed images (supported since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in images.items():
        dim = 0 if size >= 256 else size  # 0 means 256 in the directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def main() -> None:
    QGuiApplication(sys.argv)
    pngs = {}
    for size in SIZES:
        svg = HERE / ("icon_small.svg" if size <= SMALL_MAX else "icon.svg")
        pngs[size] = render(svg, size)
        (HERE / f"icon-{size}.png").write_bytes(pngs[size])
    (HERE / "icon.ico").write_bytes(ico({s: pngs[s] for s in ICO_SIZES}))
    print(f"wrote {len(SIZES)} PNGs and icon.ico to {HERE}")


if __name__ == "__main__":
    main()
