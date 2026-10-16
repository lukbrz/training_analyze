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
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

import storage
from geocode import USER_AGENT
from gui_common import COLORS, on_ui

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_ZOOM, MAX_ZOOM = 3, 18
COLOR_BUCKETS = 16
# Points closer than this (Manhattan distance, screen pixels) to the previous kept point are
# dropped when building a zoom level's paths.
SIMPLIFY_PX = 1.5
MARKER_SPACING_PX = 24
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
        # Track geometry projected once, in zoom-0 world pixels; per-zoom drawable paths are
        # built from it lazily and cached, so panning only translates and redraws them.
        self._world: list[tuple[float, float]] = []
        self._buckets: list[int | None] = []  # colour bucket per point (None = no value)
        self._paths: dict[int, dict] = {}

    # --- data ---------------------------------------------------------------------

    def set_track(self, data: TrackData, refit: bool) -> None:
        self.data = data
        present = sorted(v for v in data.values if v is not None)
        if present:
            # 5th-95th percentile, so a GPS spike or a stop doesn't wash out the whole colour scale.
            low, high = present[len(present) // 20], present[-1 - len(present) // 20]
            self._value_range = (low, high if high > low else low + 1)
        low, high = self._value_range
        self._world = [world_xy(lat, lon, 0) for lat, lon in data.points]
        self._buckets = [
            None if v is None else min(COLOR_BUCKETS - 1, max(0, int((v - low) / (high - low) * COLOR_BUCKETS)))
            for v in data.values
        ] if present else [None] * len(data.points)
        self._paths = {}
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
                target = QRectF(tx * TILE_SIZE - left, ty * TILE_SIZE - top, TILE_SIZE, TILE_SIZE)
                if pixmap is not None:
                    painter.drawPixmap(target.topLeft(), pixmap)
                else:
                    self._draw_placeholder_tile(painter, key, target)

    def _draw_placeholder_tile(self, painter: QPainter, key: tuple[int, int, int], target: QRectF) -> None:
        """While a tile downloads, stretch the matching part of an already-loaded tile from a
        lower zoom level over its place, so zooming in never flashes blank."""
        z, x, y = key
        for up in range(1, 4):
            parent = self.tiles.get((z - up, x >> up, y >> up))
            if parent is None:
                continue
            size = TILE_SIZE / 2**up
            source = QRectF((x % 2**up) * size, (y % 2**up) * size, size, size)
            painter.drawPixmap(target, parent, source)
            return

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

    @staticmethod
    def _simplified_path(world: list[tuple[float, float]], scale: float) -> QPainterPath:
        """A polyline as a QPainterPath in world pixels at `scale` (2**zoom), dropping points
        closer than SIMPLIFY_PX to the last kept one — invisible at that zoom anyway."""
        path = QPainterPath()
        last = None
        for i, (wx, wy) in enumerate(world):
            x, y = wx * scale, wy * scale
            if last is None:
                path.moveTo(x, y)
            elif abs(x - last[0]) + abs(y - last[1]) >= SIMPLIFY_PX or i == len(world) - 1:
                path.lineTo(x, y)
            else:
                continue
            last = (x, y)
        return path

    def _zoom_paths(self) -> dict:
        """The track's drawable paths at the current zoom (built once per zoom level)."""
        cached = self._paths.get(self.zoom)
        if cached is not None:
            return cached
        scale = 2**self.zoom
        data = self.data
        # Coloured track: one path per colour bucket, so a paint is a dozen drawPath calls
        # instead of thousands of separately-coloured line segments.
        colored: dict[int | None, QPainterPath] = {}
        last, last_bucket = None, object()
        for (wx, wy), bucket in zip(self._world, self._buckets):
            x, y = wx * scale, wy * scale
            if last is not None and abs(x - last[0]) + abs(y - last[1]) < SIMPLIFY_PX:
                continue
            if last is not None:
                path = colored.setdefault(bucket, QPainterPath())
                if bucket != last_bucket or path.currentPosition() != QPointF(*last):
                    path.moveTo(*last)
                path.lineTo(x, y)
            last, last_bucket = (x, y), bucket
        cached = {
            "casing": self._simplified_path(self._world, scale),
            "colored": colored,
            "highlights": [self._simplified_path([world_xy(lat, lon, 0) for lat, lon in h], scale) for h in data.highlights if len(h) > 1],
            "selected": self._simplified_path([world_xy(lat, lon, 0) for lat, lon in data.selected], scale) if len(data.selected) > 1 else None,
        }
        self._paths[self.zoom] = cached
        return cached

    @staticmethod
    def _pen(color: str, width: float, alpha: int = 255) -> QPen:
        qcolor = QColor(color)
        qcolor.setAlpha(alpha)
        return QPen(qcolor, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    def _draw_track(self, painter: QPainter) -> None:
        paths = self._zoom_paths()
        painter.save()
        painter.translate(self.width() / 2 - self.center.x(), self.height() / 2 - self.center.y())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if paths["selected"] is not None:
            painter.setPen(self._pen(COLORS["cyan"], 13, 150))
            painter.drawPath(paths["selected"])
        painter.setPen(self._pen("#ffffff", 10, 150))
        for highlight in paths["highlights"]:
            painter.drawPath(highlight)
        # Dark casing first, so the coloured line reads on any map background.
        painter.setPen(self._pen(COLORS["bg_dark"], 6.5))
        painter.drawPath(paths["casing"])
        if not paths["colored"] or set(paths["colored"]) == {None}:  # nothing to colour by (a route outline)
            painter.setPen(self._pen(COLORS["accent"], 4))
            painter.drawPath(paths["casing"])
        else:
            for bucket, path in paths["colored"].items():
                color = COLORS["comment"] if bucket is None else ramp_color((bucket + 0.5) / COLOR_BUCKETS).name()
                painter.setPen(self._pen(color, 4))
                painter.drawPath(path)
        painter.restore()

    def _draw_markers(self, painter: QPainter) -> None:
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        last = None
        for lat, lon, text in self.data.lap_markers:
            p = self._screen(lat, lon)
            # Skip markers that would overlap the previous one drawn (e.g. 1 km laps seen from
            # far out); zooming in reveals them.
            if last is not None and abs(p.x() - last.x()) + abs(p.y() - last.y()) < MARKER_SPACING_PX:
                continue
            last = p
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
