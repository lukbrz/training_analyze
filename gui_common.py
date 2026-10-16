"""Building blocks of the Qt GUI (gui_app.py): theme, running work off the UI thread, the Page
base class that mirrors a Textual Screen (title, key bindings shown in a footer), and table
and chart helpers. The GUI deliberately mirrors the TUI's screens and keys one to one; all
behaviour lives in the shared modules (services, laps_session, workout_library, display)."""

import threading
import traceback
from collections.abc import Callable
from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDate, QDateTime, QMargins, QObject, QPointF, Qt, QTime, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Tokyo Night — the same palette the browser GUI's Textual theme uses.
COLORS = {
    "bg": "#1a1b26", "bg_dark": "#16161e", "surface": "#1f2335", "surface2": "#24283b",
    "border": "#292e42", "border2": "#3b4261", "fg": "#c0caf5", "muted": "#565f89",
    "comment": "#737aa2", "accent": "#7aa2f7", "green": "#9ece6a", "red": "#f7768e",
    "yellow": "#e0af68", "cyan": "#7dcfff", "magenta": "#bb9af7", "orange": "#ff9e64",
    "selection": "#2e3c64",
}
SERIES_COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#ff9e64", "#f7768e", "#73daca"]
SPORT_COLORS = {
    "running": COLORS["green"], "cycling": COLORS["accent"], "swimming": COLORS["cyan"],
    "training": COLORS["magenta"], "strength_training": COLORS["magenta"],
    "multisport": COLORS["orange"], "hiking": COLORS["yellow"],
}
MONO_FONT = "JetBrains Mono, DejaVu Sans Mono, Consolas, monospace"

STYLESHEET = f"""
QWidget {{
    background: {COLORS['bg']}; color: {COLORS['fg']};
    font-family: Inter, "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 10pt;
}}
QLabel#appHeader {{
    background: {COLORS['bg_dark']}; color: {COLORS['accent']};
    font-size: 13pt; font-weight: 600; padding: 10px 16px; border-bottom: 1px solid {COLORS['border']};
}}
QLabel#footer {{
    background: {COLORS['bg_dark']}; color: {COLORS['comment']};
    padding: 6px 12px; border-top: 1px solid {COLORS['border']};
}}
QLabel#pageTitle {{ font-size: 12pt; font-weight: 600; color: {COLORS['fg']}; }}
QLabel#status {{ color: {COLORS['comment']}; }}
QLabel#notes {{ color: {COLORS['yellow']}; }}
QLabel#card, QPlainTextEdit#card {{
    background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 10px;
    padding: 10px 14px; font-family: {MONO_FONT}; font-size: 9.5pt;
}}
QTableWidget {{
    background: {COLORS['bg_dark']}; alternate-background-color: {COLORS['surface']};
    gridline-color: {COLORS['border']}; border: 1px solid {COLORS['border']}; border-radius: 10px;
    selection-background-color: {COLORS['selection']}; selection-color: {COLORS['fg']};
}}
QHeaderView::section {{
    background: {COLORS['surface2']}; color: {COLORS['accent']}; padding: 6px 8px;
    border: none; border-right: 1px solid {COLORS['border']}; font-weight: 600;
}}
QPushButton {{
    background: {COLORS['surface2']}; border: 1px solid {COLORS['border2']}; border-radius: 8px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {COLORS['accent']}; background: #2f3549; }}
QPushButton:pressed {{ background: {COLORS['selection']}; }}
QPushButton:checked, QPushButton[primary="true"] {{
    background: {COLORS['accent']}; color: {COLORS['bg']}; border-color: {COLORS['accent']}; font-weight: 600;
}}
QPushButton:disabled {{ color: {COLORS['muted']}; border-color: {COLORS['border']}; }}
QLineEdit, QComboBox {{
    background: {COLORS['bg_dark']}; border: 1px solid {COLORS['border2']}; border-radius: 8px; padding: 6px 8px;
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {COLORS['accent']}; }}
QComboBox QAbstractItemView {{ background: {COLORS['surface']}; selection-background-color: {COLORS['selection']}; }}
QStatusBar {{ background: {COLORS['bg_dark']}; color: {COLORS['fg']}; }}
QScrollBar:vertical {{ background: {COLORS['bg_dark']}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {COLORS['border2']}; border-radius: 5px; min-height: 24px; }}
QMessageBox QLabel {{ font-family: {MONO_FONT}; }}
"""


class _Bridge(QObject):
    """Delivers callables from worker threads to the Qt main thread (queued signal)."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(self._run)

    @Slot(object)
    def _run(self, fn) -> None:
        fn()


_bridge: _Bridge | None = None


def init_bridge() -> None:
    """Must be called once from the main thread after the QApplication exists."""
    global _bridge
    _bridge = _Bridge()


def on_ui(fn: Callable, *args, **kwargs) -> None:
    """Run fn(*args) on the UI thread (safe to call from any thread)."""
    _bridge.invoke.emit(lambda: fn(*args, **kwargs))


def run_background(work: Callable[[], object], done: Callable | None = None, failed: Callable | None = None) -> None:
    """Run `work` on a worker thread; `done(result)` / `failed(exception)` run on the UI thread."""

    def target() -> None:
        try:
            result = work()
        except Exception as e:  # surfaced to the user via `failed` (or the window's notifier)
            if failed:
                on_ui(failed, e)
            else:
                traceback.print_exception(e)
            return
        if done:
            on_ui(done, result)

    threading.Thread(target=target, daemon=True).start()


class Page(QWidget):
    """A GUI counterpart of a Textual Screen: a title, single-key bindings (shown in the
    window footer like Textual's Footer), and hooks for being shown."""

    title = ""

    def __init__(self, window) -> None:
        super().__init__()
        self.window_ = window
        self.bindings: list[tuple[str, str]] = []
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(14, 12, 14, 10)
        self.layout_.setSpacing(10)

    def bind(self, key: str, label: str, fn: Callable, display_key: str | None = None) -> None:
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(fn)
        self.bindings.append((display_key or key, label))

    def bind_back(self) -> None:
        self.bind("Escape", "Back", self.window_.pop, display_key="esc")

    def notify(self, message: str, severity: str = "information") -> None:
        self.window_.notify(message, severity)

    def on_shown(self) -> None:
        """Called each time the page becomes the visible one (first push and after a pop)."""

    def focus_widget(self) -> QWidget | None:
        return None


def label(text: str = "", name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def make_table(columns: tuple[str, ...], stretch_column: int | None = None) -> QTableWidget:
    """A read-only, row-selecting table. `stretch_column` (e.g. the name column) takes up the
    spare width; without it columns just fit their contents."""
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(30)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    table.setProperty("stretch_column", stretch_column)
    header.setHighlightSections(False)
    table.setWordWrap(False)
    return table


def fill_table(
    table: QTableWidget,
    rows: list[tuple[str, ...]],
    styles: list[dict] | None = None,
    right_aligned: set[int] = frozenset(),
    keep_row: bool = True,
) -> None:
    """Replace a table's rows. Each style dict may hold bold/italic/dim/color/color_columns."""
    current = table.currentRow()
    table.setUpdatesEnabled(False)
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        style = styles[r] if styles else {}
        for c, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if c in right_aligned:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if style.get("bold") or style.get("italic"):
                font = QFont(item.font())
                font.setBold(bool(style.get("bold")))
                font.setItalic(bool(style.get("italic")))
                item.setFont(font)
            color = style.get("color_columns", {}).get(c) or style.get("color") or (COLORS["muted"] if style.get("dim") else None)
            if color:
                item.setForeground(QBrush(QColor(color)))
            table.setItem(r, c, item)
    table.setUpdatesEnabled(True)
    table.resizeColumnsToContents()
    stretch = table.property("stretch_column")
    if stretch is not None and stretch < table.columnCount():
        table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    if rows:
        table.selectRow(min(max(current, 0), len(rows) - 1) if keep_row else 0)


def table_as_tsv(table: QTableWidget) -> str:
    header = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    lines = ["\t".join(header)]
    for r in range(table.rowCount()):
        lines.append("\t".join((table.item(r, c).text() if table.item(r, c) else "").strip() for c in range(table.columnCount())))
    return "\n".join(lines)


# --- Charts ---------------------------------------------------------------------------


def make_chart_view(title: str = "") -> tuple[QChartView, QChart]:
    chart = QChart()
    chart.setTitle(title)
    chart.setBackgroundBrush(QBrush(QColor(COLORS["bg_dark"])))
    chart.setBackgroundRoundness(10)
    chart.setTitleBrush(QBrush(QColor(COLORS["fg"])))
    title_font = QFont()
    title_font.setPointSizeF(10.5)
    title_font.setBold(True)
    chart.setTitleFont(title_font)
    chart.legend().setLabelColor(QColor(COLORS["fg"]))
    chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
    chart.setMargins(QMargins(6, 4, 10, 4))
    chart.layout().setContentsMargins(0, 0, 0, 0)
    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(170)
    view.setStyleSheet(f"background: {COLORS['bg']}; border: none;")
    return view, chart


def style_axis(axis) -> None:
    axis.setLabelsBrush(QBrush(QColor(COLORS["comment"])))
    axis.setTitleBrush(QBrush(QColor(COLORS["comment"])))
    axis.setGridLineColor(QColor(COLORS["border"]))
    axis.setLinePenColor(QColor(COLORS["border2"]))


def reset_chart(chart: QChart, title: str) -> None:
    chart.removeAllSeries()
    for axis in chart.axes():
        chart.removeAxis(axis)
    chart.setTitle(title)


def line_series(name: str, xs, ys, color: str, width: float = 2.0, dashed: bool = False) -> QLineSeries:
    series = QLineSeries()
    series.setName(name)
    series.replace([QPointF(float(x), float(y)) for x, y in zip(xs, ys)])
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    series.setPen(pen)
    return series


def plot_lines(
    chart: QChart, title: str, series_list: list[tuple[str, list, list]], x_title: str = "", legend: bool = True,
) -> None:
    reset_chart(chart, title if series_list else f"{title} — no data")
    chart.legend().setVisible(legend)
    if not series_list:
        return
    x_axis, y_axis = QValueAxis(), QValueAxis()
    x_axis.setTitleText(x_title)
    for axis, align in ((x_axis, Qt.AlignmentFlag.AlignBottom), (y_axis, Qt.AlignmentFlag.AlignLeft)):
        style_axis(axis)
        chart.addAxis(axis, align)
    all_x = [x for _, xs, _ in series_list for x in xs]
    all_y = [y for _, _, ys in series_list for y in ys]
    for i, (name, xs, ys) in enumerate(series_list):
        series = line_series(name, xs, ys, SERIES_COLORS[i % len(SERIES_COLORS)], width=1.6)
        chart.addSeries(series)
        series.attachAxis(x_axis)
        series.attachAxis(y_axis)
    x_axis.setRange(min(all_x), max(all_x))
    pad = (max(all_y) - min(all_y)) * 0.05 or 1.0
    y_axis.setRange(min(all_y) - pad, max(all_y) + pad)
    for axis in (x_axis, y_axis):
        axis.setTickCount(5)
        axis.applyNiceNumbers()
        axis.setLabelFormat("%.0f")


def plot_bars(chart: QChart, title: str, rows: list[tuple[str, int]], color: str, y_title: str = "") -> None:
    reset_chart(chart, title if rows else f"{title} — no data")
    chart.legend().setVisible(False)
    if not rows:
        return
    bar_set = QBarSet("count")
    bar_set.append([float(count) for _, count in rows])
    bar_set.setColor(QColor(color))
    bar_set.setBorderColor(QColor(color))
    series = QBarSeries()
    series.append(bar_set)
    series.setBarWidth(0.85)
    chart.addSeries(series)
    x_axis = QBarCategoryAxis()
    x_axis.append([label for label, _ in rows])
    y_axis = QValueAxis()
    top = max(count for _, count in rows)
    # Counts are whole numbers: integer labels and one tick per count.
    y_axis.setRange(0, top)
    y_axis.setTickCount(top + 1 if top <= 12 else 7)
    y_axis.setLabelFormat("%d")
    y_axis.setTitleText(y_title)
    for axis, align in ((x_axis, Qt.AlignmentFlag.AlignBottom), (y_axis, Qt.AlignmentFlag.AlignLeft)):
        style_axis(axis)
        chart.addAxis(axis, align)
        series.attachAxis(axis)


def to_msecs(day: date) -> float:
    return float(QDateTime(QDate(day.year, day.month, day.day), QTime(12, 0)).toMSecsSinceEpoch())


def plot_dated_lines(
    chart: QChart, title: str, days: list[date], lines: list[tuple[str, list[float], str]],
    markers: list[tuple[date, str]] = (), dashed_after: date | None = None,
) -> None:
    """Lines over calendar days, with optional vertical marker lines (e.g. today); the part of
    each line after `dashed_after` (a projection) is drawn dashed."""
    reset_chart(chart, title)
    chart.legend().setVisible(True)
    if not days:
        return
    x_axis = QDateTimeAxis()
    x_axis.setFormat("dd.MM")
    x_axis.setTickCount(9)
    y_axis = QValueAxis()
    for axis, align in ((x_axis, Qt.AlignmentFlag.AlignBottom), (y_axis, Qt.AlignmentFlag.AlignLeft)):
        style_axis(axis)
        chart.addAxis(axis, align)
    xs = [to_msecs(d) for d in days]
    all_y = [y for _, ys, _ in lines for y in ys]
    low, high = min(all_y + [0.0]), max(all_y + [1.0])
    split = len(days) if dashed_after is None else next((i for i, d in enumerate(days) if d > dashed_after), len(days))
    for name, ys, color in lines:
        series = line_series(name, xs[:split], ys[:split], color, width=2.2)
        chart.addSeries(series)
        series.attachAxis(x_axis)
        series.attachAxis(y_axis)
        if split < len(days):
            start = max(split - 1, 0)  # overlap one point so the dashed part continues the line
            projected = line_series(f"{name} (planned)", xs[start:], ys[start:], color, width=2.0, dashed=True)
            chart.addSeries(projected)
            projected.attachAxis(x_axis)
            projected.attachAxis(y_axis)
            chart.legend().markers(projected)[0].setVisible(False)
    for day, color in markers:
        marker = line_series("", [to_msecs(day)] * 2, [low, high], color, width=1.2, dashed=True)
        chart.addSeries(marker)
        marker.attachAxis(x_axis)
        marker.attachAxis(y_axis)
        chart.legend().markers(marker)[0].setVisible(False)
    x_axis.setRange(QDateTime.fromMSecsSinceEpoch(int(xs[0])), QDateTime.fromMSecsSinceEpoch(int(xs[-1])))
    y_axis.setRange(low - 2, high + 2)
    y_axis.applyNiceNumbers()
    y_axis.setLabelFormat("%.0f")


def card(widget: QWidget) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    return frame
