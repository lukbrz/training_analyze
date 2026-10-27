"""Building blocks of the Qt GUI (gui_app.py): theme, running work off the UI thread, the Page
base class that mirrors a Textual Screen (title, key bindings shown in a footer), and table
and chart helpers. The GUI deliberately mirrors the TUI's screens and keys one to one; all
behaviour lives in the shared modules (services, laps_session, workout_library, display)."""

import math
import threading
import traceback
from collections.abc import Callable
from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSet,
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QDate, QDateTime, QMargins, QObject, QPointF, QRectF, Qt, QTime, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import display
from theme import COLORS, SERIES_COLORS, SPORT_COLORS

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
QWidget#footer {{
    background: {COLORS['bg_dark']}; border-top: 1px solid {COLORS['border']};
}}
QPushButton#footerKey {{
    background: transparent; border: none; border-radius: 6px; padding: 0; min-height: 26px;
}}
QPushButton#footerKey:hover {{ background: {COLORS['surface2']}; color: {COLORS['fg']}; }}
QLabel#footerCap {{
    background: {COLORS['surface2']}; color: {COLORS['accent']}; font-weight: 600;
    border-radius: 4px; padding: 1px 6px;
}}
QLabel#footerText {{ background: transparent; color: {COLORS['comment']}; }}
QPushButton#footerKey:hover QLabel#footerText {{ color: {COLORS['fg']}; }}
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
    background: {COLORS['accent']}; color: {COLORS['bg']}; border-color: {COLORS['accent']};
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
QFrame#tile {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 12px; }}
QFrame#tile QLabel {{ background: transparent; }}
QLabel#tileCaption {{ color: {COLORS['comment']}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
QLabel#tileHint {{ color: {COLORS['comment']}; font-size: 8.5pt; }}
QLabel#tileHeading {{ color: {COLORS['accent']}; font-weight: 600; min-width: 48px; }}
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
        self.bindings: list[tuple[str, str, Callable]] = []  # (key shown, label, action)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(14, 12, 14, 10)
        self.layout_.setSpacing(10)

    def bind(self, key: str, label: str, fn: Callable, display_key: str | None = None) -> None:
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(fn)
        self.bindings.append((display_key or key, label, fn))

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
    """Replace a table's rows. Each style dict may hold bold/italic/dim/color/color_columns/
    background."""
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
            if style.get("background"):
                item.setBackground(QBrush(QColor(style["background"])))
            table.setItem(r, c, item)
    table.setUpdatesEnabled(True)
    table.resizeColumnsToContents()
    stretch = table.property("stretch_column")
    if stretch is not None and stretch < table.columnCount():
        table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    if rows:
        table.selectRow(min(max(current, 0), len(rows) - 1) if keep_row else 0)


def table_as_tsv(table: QTableWidget) -> str:
    """The table exactly as displayed, as tab-separated text (see display.tsv)."""
    columns = range(table.columnCount())
    return display.tsv(
        [table.horizontalHeaderItem(c).text() for c in columns],
        [[table.item(r, c).text() if table.item(r, c) else "" for c in columns] for r in range(table.rowCount())],
    )


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


def plot_bars(chart: QChart, title: str, bars, color: str, y_title: str = "") -> None:
    """A histogram from display.HistogramBar items; outlier edge bars are drawn grey."""
    reset_chart(chart, title if bars else f"{title} — no data")
    chart.legend().setVisible(False)
    if not bars:
        return
    series = QStackedBarSeries()  # two stacked sets: regular counts, and grey outlier counts
    for outlier, bar_color in ((False, color), (True, COLORS["comment"])):
        bar_set = QBarSet("outliers" if outlier else "count")
        bar_set.append([float(bar.count if bar.outlier == outlier else 0) for bar in bars])
        bar_set.setColor(QColor(bar_color))
        bar_set.setBorderColor(QColor(bar_color))
        series.append(bar_set)
    series.setBarWidth(0.85)
    chart.addSeries(series)
    x_axis = QBarCategoryAxis()
    x_axis.append([bar.label for bar in bars])
    y_axis = QValueAxis()
    top = max(bar.count for bar in bars)
    # Counts are whole numbers: integer labels on a whole-number tick step.
    step = display.count_tick_step(top)
    y_axis.setRange(0, step * math.ceil(top / step))
    y_axis.setTickCount(math.ceil(top / step) + 1)
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


# --- Graphical summaries (stat tiles, stacked bar) ---------------------------------


def palette(name: str) -> str:
    return COLORS.get(name, COLORS["fg"]) if name else COLORS["fg"]


class StatTile(QFrame):
    """A dashboard tile: small caption, big coloured value with unit, optional hint line."""

    def __init__(self, stat) -> None:
        super().__init__()
        self.setObjectName("tile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(0)
        caption = QLabel(stat.label.upper())
        caption.setObjectName("tileCaption")
        value = QLabel(
            f"<span style='font-size:18pt; font-weight:600; color:{palette(stat.color)}'>{stat.value}</span>"
            f"<span style='font-size:9.5pt; color:{COLORS['comment']}'>&nbsp;{stat.unit}</span>"
        )
        layout.addWidget(caption)
        layout.addWidget(value)
        if stat.hint:
            hint = QLabel(stat.hint)
            hint.setObjectName("tileHint")
            layout.addWidget(hint)


class StatRow(QWidget):
    """A row of StatTiles that can be refilled."""

    def __init__(self) -> None:
        super().__init__()
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(10)

    def set_stats(self, stats, title: str = "") -> None:
        while self.row.count():
            widget = self.row.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if title:
            heading = QLabel(title)
            heading.setObjectName("tileHeading")
            self.row.addWidget(heading)
        for stat in stats:
            self.row.addWidget(StatTile(stat), 1)
        self.setVisible(bool(stats))


class StatStrip(QLabel):
    """Stats inline on one line for a dense header: label, coloured value, unit. Groups are
    separated by a bar; a group's hints show inline (short ones like "→ 53") or, for
    explanations, in the strip's tooltip. `caption` explains the first group's hints and
    follows it."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("statStrip")
        self.setTextFormat(Qt.TextFormat.RichText)

    def set_groups(self, groups: list[tuple[list, bool]], caption: str = "") -> None:
        muted = COLORS["comment"]
        html_groups, tips = [], []
        for stats, inline_hints in groups:
            items = []
            for s in stats:
                hint = f"<span style='color:{muted}'>&nbsp;{s.hint}</span>" if inline_hints and s.hint else ""
                unit = f"<span style='color:{muted}; font-size:9pt'>&nbsp;{s.unit}</span>" if s.unit else ""
                items.append(
                    f"<span style='color:{muted}'>{s.label}</span>&nbsp;"
                    f"<b style='font-size:13pt; color:{palette(s.color)}'>{s.value}</b>{unit}{hint}"
                )
                if s.hint and not inline_hints:
                    tips.append(f"{s.label}: {s.hint}")
            html_groups.append("&nbsp;&nbsp;·&nbsp;&nbsp;".join(items))
        if caption and html_groups:
            html_groups[0] += f"&nbsp;&nbsp;<span style='color:{muted}; font-size:8.5pt'>({caption})</span>"
        self.setText(f"&nbsp;&nbsp;<span style='color:{COLORS['border2']}'>│</span>&nbsp;&nbsp;".join(html_groups))
        self.setToolTip("\n".join(tips))


class StackedBar(QWidget):
    """A rounded horizontal bar split into coloured parts, each labelled with its value."""

    def __init__(self, bar_height: int = 26) -> None:
        super().__init__()
        self.bar_height = bar_height
        self.setFixedHeight(bar_height + 22)
        self.parts: list[tuple[str, int, str]] = []  # (label, value, colour)
        self.title = ""

    def set_parts(self, title: str, parts: list[tuple[str, int, str]]) -> None:
        self.title, self.parts = title, parts
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPointSizeF(9)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["comment"]))
        painter.drawText(QRectF(0, 0, self.width(), 18), Qt.AlignmentFlag.AlignLeft, self.title)
        bar = QRectF(0, 20, self.width(), self.bar_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(COLORS["surface"])))
        painter.drawRoundedRect(bar, 8, 8)
        total = sum(value for _, value, _ in self.parts)
        if not total:
            return
        path = QPainterPath()
        path.addRoundedRect(bar, 8, 8)
        painter.setClipPath(path)
        x = bar.left()
        for text, value, color in self.parts:
            if not value:
                continue
            width = bar.width() * value / total
            segment = QRectF(x, bar.top(), width, bar.height())
            painter.setBrush(QBrush(QColor(color)))
            painter.drawRect(segment)
            painter.setPen(QColor(COLORS["bg"]))
            bold = QFont(font)
            bold.setBold(True)
            painter.setFont(bold)
            label_text = f"{text} {value}"
            if painter.fontMetrics().horizontalAdvance(label_text) + 12 > width:
                label_text = str(value)
            if painter.fontMetrics().horizontalAdvance(label_text) + 6 <= width:
                painter.drawText(segment, Qt.AlignmentFlag.AlignCenter, label_text)
            painter.setPen(Qt.PenStyle.NoPen)
            x += width


class LegCard(QFrame):
    """One multisport leg (Swim / T1 / Bike / ...) as a compact card."""

    def __init__(self, name: str, sport: str, stats) -> None:
        super().__init__()
        self.setObjectName("tile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(1)
        color = SPORT_COLORS.get(sport, COLORS["fg"])
        title = QLabel(f"<span style='font-size:12pt; font-weight:600; color:{color}'>{name}</span>")
        layout.addWidget(title)
        for stat in stats:
            line = QLabel(
                f"<span style='color:{COLORS['comment']}'>{stat.label}</span>&nbsp; "
                f"<span style='color:{palette(stat.color)}; font-weight:600'>{stat.value}</span>"
                f"<span style='color:{COLORS['comment']}'>&nbsp;{stat.unit}</span>"
            )
            layout.addWidget(line)
