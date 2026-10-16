"""A slippy map for the Qt GUI: an activity's GPS track drawn over OpenStreetMap tiles,
coloured by a metric, with marked intervals / the selected lap highlighted.

Tiles come from the public OSM tile server, which requires an identifying User-Agent, caching
and attribution (https://operations.osmfoundation.org/policies/tiles/): every tile is fetched
at most once and kept in storage.TILE_CACHE_DIR, requests use the app's User-Agent, and the
attribution is drawn on the map. Without network the track still draws on a plain background.
"""

import math
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

import storage
from geocode import USER_AGENT
from gui_common import COLORS, on_ui

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_ZOOM, MAX_ZOOM = 3, 18
MAX_DRAWN_POINTS = 4000
ATTRIBUTION = "© OpenStreetMap contributors"
# Few parallel downloads, as the OSM tile policy asks.
_tile_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tiles")

# Low -> high colour ramp (blue, cyan, green, yellow, red).
RAMP = [(0.0, "#7aa2f7"), (0.25, "#7dcfff"), (0.5, "#9ece6a"), (0.75, "#e0af68"), (1.0, "#f7768e")]


def ramp_color(t: float) -> QColor:
    t = min(max(t, 0.0), 1.0)
    for (t0, c0), (t1, c1) in zip(RAMP, RAMP[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            a, b = QColor(c0), QColor(c1)
            return QColor(
                round(a.red() + (b.red() - a.red()) * f),
                round(a.green() + (b.green() - a.green()) * f),
                round(a.blue() + (b.blue() - a.blue()) * f),
            )
    return QColor(RAMP[-1][1])


def world_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates at `zoom` (the same projection as the tiles)."""
    scale = TILE_SIZE * 2**zoom
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * scale
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * scale
    return x, y


def _fetch_tile(z: int, x: int, y: int) -> bytes | None:
    path = storage.TILE_CACHE_DIR / str(z) / str(x) / f"{y}.png"
    if path.exists():
        return path.read_bytes()
    request = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
    except Exception:  # offline / server refused: the map just draws without this tile
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


@dataclass
class TrackData:
    points: list[tuple[float, float]]  # (lat, lon)
    values: list[float | None]  # metric per point, for colouring (None = no data there)
    value_label: str = ""
    value_format: Callable[[float], str] = lambda v: f"{v:.0f}"
    highlights: list[list[tuple[float, float]]] = field(default_factory=list)  # marked intervals
    selected: list[tuple[float, float]] = field(default_factory=list)  # the selected lap
    lap_markers: list[tuple[float, float, str]] = field(default_factory=list)  # (lat, lon, label)


class TrackMap(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # so the page's key bindings work while it has focus
        self.data: TrackData | None = None
        self.zoom = 13
        self.center = QPointF(0, 0)  # world pixel coordinates at self.zoom
        self.tiles: dict[tuple[int, int, int], QPixmap | None] = {}
        self._drag_from: QPointF | None = None
        self._needs_fit = True
        self._value_range = (0.0, 1.0)

    # --- data ---------------------------------------------------------------------

    def set_track(self, data: TrackData, refit: bool) -> None:
        self.data = data
        present = sorted(v for v in data.values if v is not None)
        if present:
            # 5th-95th percentile, so a GPS spike or a stop doesn't wash out the whole colour scale.
            low, high = present[len(present) // 20], present[-1 - len(present) // 20]
            self._value_range = (low, high if high > low else low + 1)
        if refit or self._needs_fit:
            self._needs_fit = True
            self.fit()
        self.update()

    def fit(self) -> None:
        if not self.data or not self.data.points or self.width() < 50:
            return
        lats = [p[0] for p in self.data.points]
        lons = [p[1] for p in self.data.points]
        for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
            x0, y0 = world_xy(max(lats), min(lons), zoom)
            x1, y1 = world_xy(min(lats), max(lons), zoom)
            if x1 - x0 <= self.width() * 0.88 and y1 - y0 <= self.height() * 0.85:
                break
        self.zoom = zoom
        self.center = QPointF((x0 + x1) / 2, (y0 + y1) / 2)
        self._needs_fit = False
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._needs_fit:
            self.fit()
        super().resizeEvent(event)

    # --- coordinates ----------------------------------------------------------------

    def _screen(self, lat: float, lon: float) -> QPointF:
        x, y = world_xy(lat, lon, self.zoom)
        return QPointF(x - self.center.x() + self.width() / 2, y - self.center.y() + self.height() / 2)

    # --- painting -------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["bg_dark"]))
        if not self.data or not self.data.points:
            painter.setPen(QColor(COLORS["comment"]))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No GPS track for this activity")
            return
        self._draw_tiles(painter)
        # Dim the (bright) map a little so the coloured track stands out in the dark theme.
        painter.fillRect(self.rect(), QColor(26, 27, 38, 70))
        for path, color, width in ((self.data.selected, COLORS["cyan"], 13), *((h, "#ffffff", 10) for h in self.data.highlights)):
            self._draw_polyline(painter, path, QColor(color), width, alpha=150)
        self._draw_track(painter)
        self._draw_markers(painter)
        self._draw_legend(painter)
        self._draw_attribution(painter)

    def _draw_tiles(self, painter: QPainter) -> None:
        z = self.zoom
        left = self.center.x() - self.width() / 2
        top = self.center.y() - self.height() / 2
        count = 2**z
        for tx in range(int(left // TILE_SIZE), int((left + self.width()) // TILE_SIZE) + 1):
            for ty in range(int(top // TILE_SIZE), int((top + self.height()) // TILE_SIZE) + 1):
                if not 0 <= ty < count:
                    continue
                key = (z, tx % count, ty)
                pixmap = self.tiles.get(key)
                if key not in self.tiles:
                    self._request_tile(key)
                if pixmap is not None:
                    painter.drawPixmap(QPointF(tx * TILE_SIZE - left, ty * TILE_SIZE - top), pixmap)

    def _request_tile(self, key: tuple[int, int, int]) -> None:
        self.tiles[key] = None  # pending (and stays None if the download fails)
        future = _tile_pool.submit(_fetch_tile, *key)
        future.add_done_callback(lambda f: on_ui(self._tile_loaded, key, f.result()))

    def _tile_loaded(self, key: tuple[int, int, int], data: bytes | None) -> None:
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.tiles[key] = pixmap
                self.update()

    def _sampled(self, points):
        step = max(1, len(points) // MAX_DRAWN_POINTS)
        return points[::step] if step > 1 else points

    def _draw_polyline(self, painter: QPainter, points, color: QColor, width: float, alpha: int = 255) -> None:
        if len(points) < 2:
            return
        color = QColor(color)
        color.setAlpha(alpha)
        pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([self._screen(lat, lon) for lat, lon in self._sampled(points)]))

    def _draw_track(self, painter: QPainter) -> None:
        points, values = self.data.points, self.data.values
        step = max(1, len(points) // MAX_DRAWN_POINTS)
        low, high = self._value_range
        # Dark casing first, so the coloured line reads on any map background.
        self._draw_polyline(painter, points, QColor(COLORS["bg_dark"]), 6.5)
        if not any(v is not None for v in values):  # nothing to colour by (e.g. a route outline)
            self._draw_polyline(painter, points, QColor(COLORS["accent"]), 4)
            return
        previous = None
        for i in range(0, len(points), step):
            here = self._screen(*points[i])
            if previous is not None:
                value = values[i]
                color = ramp_color((value - low) / (high - low)) if value is not None else QColor(COLORS["comment"])
                painter.setPen(QPen(color, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(previous, here)
            previous = here

    def _draw_markers(self, painter: QPainter) -> None:
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        markers = self.data.lap_markers if len(self.data.lap_markers) <= 60 else []
        for lat, lon, text in markers:
            p = self._screen(lat, lon)
            rect = QRectF(p.x() - 9, p.y() - 9, 18, 18)
            painter.setPen(QPen(QColor(COLORS["bg_dark"]), 1.5))
            painter.setBrush(QBrush(QColor(COLORS["fg"])))
            painter.drawEllipse(rect)
            painter.setPen(QColor(COLORS["bg_dark"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        for (lat, lon), color in ((self.data.points[0], COLORS["green"]), (self.data.points[-1], COLORS["red"])):
            p = self._screen(lat, lon)
            painter.setPen(QPen(QColor("#ffffff"), 2.5))
            painter.setBrush(QBrush(QColor(color)))
            painter.drawEllipse(p, 7, 7)

    def _draw_legend(self, painter: QPainter) -> None:
        if not any(v is not None for v in self.data.values):
            return
        low, high = self._value_range
        box = QRectF(12, self.height() - 62, 230, 50)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(22, 22, 30, 215)))
        painter.drawRoundedRect(box, 8, 8)
        gradient = QLinearGradient(box.left() + 12, 0, box.right() - 12, 0)
        for t, color in RAMP:
            gradient.setColorAt(t, QColor(color))
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(QRectF(box.left() + 12, box.top() + 24, box.width() - 24, 9), 4, 4)
        painter.setPen(QColor(COLORS["fg"]))
        font = QFont()
        font.setPointSizeF(8)
        painter.setFont(font)
        painter.drawText(QRectF(box.left() + 12, box.top() + 4, box.width() - 24, 16), Qt.AlignmentFlag.AlignLeft, self.data.value_label)
        fmt = self.data.value_format
        painter.drawText(QRectF(box.left() + 12, box.top() + 33, 100, 16), Qt.AlignmentFlag.AlignLeft, fmt(low))
        painter.drawText(QRectF(box.right() - 112, box.top() + 33, 100, 16), Qt.AlignmentFlag.AlignRight, fmt(high))

    def _draw_attribution(self, painter: QPainter) -> None:
        font = QFont()
        font.setPointSizeF(7.5)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(ATTRIBUTION) + 12
        box = QRectF(self.width() - width - 4, self.height() - 20, width, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(22, 22, 30, 200)))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(COLORS["comment"]))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, ATTRIBUTION)

    # --- interaction ----------------------------------------------------------------

    def _zoom_to(self, zoom: int, anchor: QPointF) -> None:
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if zoom == self.zoom:
            return
        # Keep the world point under the cursor fixed while zooming.
        factor = 2 ** (zoom - self.zoom)
        offset = QPointF(anchor.x() - self.width() / 2, anchor.y() - self.height() / 2)
        world_anchor = self.center + offset
        self.center = world_anchor * factor - offset
        self.zoom = zoom
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        steps = 1 if event.angleDelta().y() > 0 else -1
        self._zoom_to(self.zoom + steps, event.position())

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._drag_from is not None:
            self.center -= event.position() - self._drag_from
            self._drag_from = event.position()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._drag_from = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.fit()

    def zoom_in(self) -> None:
        self._zoom_to(self.zoom + 1, QPointF(self.width() / 2, self.height() / 2))

    def zoom_out(self) -> None:
        self._zoom_to(self.zoom - 1, QPointF(self.width() / 2, self.height() / 2))
