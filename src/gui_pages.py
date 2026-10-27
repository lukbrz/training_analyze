"""The Qt GUI's pages — one per TUI screen in screens.py, same layout and same keys. Each is a
thin shell over the shared logic (services, laps_session, workout_library, display), exactly
like its Textual counterpart."""

from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import display
import fit_parser
import garmin_client
import services
import settings
import storage
import training_export
import training_load
import workout_library
from gui_common import (
    COLORS,
    SPORT_COLORS,
    LegCard,
    Page,
    StackedBar,
    StatRow,
    StatStrip,
    fill_table,
    label,
    make_chart_view,
    make_table,
    on_ui,
    plot_bars,
    plot_dated_lines,
    plot_lines,
    run_background,
    table_as_tsv,
)
from gui_map import TrackData, TrackMap
from laps_session import MAP_METRICS, LapsError, LapsSession
from models import ActivitySummary, format_distance
from workout_library import LibraryError, WorkoutLibrary

ALL_SPORTS = "__all__"


def button(text: str, on_click, primary: bool = False, checkable: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.setCheckable(checkable)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep keyboard focus on the table, like the TUI
    widget.clicked.connect(on_click)
    return widget


def row_layout(*widgets, stretch_index: int | None = None) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(8)
    for i, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if i == stretch_index else 0)
    if stretch_index is None:
        layout.addStretch(1)
    return layout


def copy_text(page: Page, text: str, what: str) -> None:
    QApplication.clipboard().setText(text)
    page.notify(f"Copied {what}")


def activity_styles(activities: list[ActivitySummary]) -> list[dict]:
    return [{"color_columns": {2: SPORT_COLORS.get(a.sport, COLORS["fg"])}} for a in activities]


# --- Login -------------------------------------------------------------------------


class LoginPage(Page):
    title = "Log in to Garmin Connect"

    def __init__(self, window) -> None:
        super().__init__(window)
        self.mfa_pending = None
        self.status = label("", "status")
        self.email = QLineEdit(placeholderText="Email")
        self.password = QLineEdit(placeholderText="Password", echoMode=QLineEdit.EchoMode.Password)
        self.mfa = QLineEdit(placeholderText="MFA code")
        self.mfa.hide()
        self.login_button = button("Log in", self.submit, primary=True)
        for widget in (label("Log in to Garmin Connect", "pageTitle"), self.status, self.email, self.password, self.mfa, self.login_button):
            widget.setMaximumWidth(420)
            self.layout_.addWidget(widget)
        self.layout_.addStretch(1)
        for field in (self.email, self.password, self.mfa):
            field.returnPressed.connect(self.submit)

    def focus_widget(self):
        return self.email

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.status.setText(text)
        for widget in (self.email, self.password, self.login_button):
            widget.setEnabled(not busy)
        self.mfa.setEnabled(not busy)

    def submit(self) -> None:
        if self.mfa_pending is not None:
            code = self.mfa.text().strip()
            if not code:
                self.notify("Enter the MFA code", "warning")
                return
            self._set_busy(True, "Verifying code...")
            pending = self.mfa_pending
            run_background(lambda: garmin_client.complete_mfa(pending, code), self.window_.logged_in, self._failed)
            return
        email, password = self.email.text().strip(), self.password.text()
        if not email or not password:
            self.notify("Enter email and password", "warning")
            return
        self._set_busy(True, "Logging in...")
        run_background(lambda: garmin_client.begin_login(email, password), self.window_.logged_in, self._failed)

    def _failed(self, error: Exception) -> None:
        if isinstance(error, garmin_client.MfaRequired):
            self.mfa_pending = error.garmin
            self._set_busy(False, "Enter the MFA code sent to your device")
            self.email.setEnabled(False)
            self.password.setEnabled(False)
            self.mfa.show()
            self.mfa.setFocus()
            return
        self._set_busy(False)
        self.notify(str(error), "error")


# --- Activities --------------------------------------------------------------------


class MainPage(Page):
    title = "Activities"

    def __init__(self, window, client) -> None:
        super().__init__(window)
        self.client = client
        self.all_activities: list[ActivitySummary] = []
        self.shown: list[ActivitySummary] = []
        self.status = label("", "status")
        self.sync_button = button("Sync (s)", self.sync, primary=True)
        self.reprocess_button = button("Reprocess (r)", self.reprocess)
        self.sport_filter = QComboBox()
        self.sport_filter.setMinimumWidth(180)
        self.sport_filter.currentIndexChanged.connect(lambda _: self._render_table())
        self.table = make_table(display.ACTIVITY_COLUMNS, stretch_column=1)
        self.table.cellActivated.connect(lambda row, _: self._open(row))
        self.layout_.addWidget(self.status)
        self.layout_.addLayout(row_layout(
            self.sync_button, self.reprocess_button, button("Routes (o)", self.routes),
            button("Workouts (w)", self.workouts), button("Calendar (c)", self.calendar), self.sport_filter,
        ))
        self.layout_.addWidget(self.table, 1)
        self.bind("s", "Sync", self.sync)
        self.bind("r", "Reprocess", self.reprocess)
        self.bind("o", "Routes", self.routes)
        self.bind("w", "Workouts", self.workouts)
        self.bind("c", "Calendar & load", self.calendar)
        self.bind("p", "Settings", lambda: self.window_.push(SettingsPage(self.window_)))
        self.bind("Shift+H", "Sync full history", self.sync_history, display_key="H")
        self.bind("y", "Copy table", lambda: copy_text(self, table_as_tsv(self.table), f"{self.table.rowCount()} activities"))
        self.refresh_table()

    def focus_widget(self):
        return self.table

    def refresh_table(self) -> None:
        self.all_activities, self.hidden_count = services.listed_activities()
        self.routes_by_id = {r.route_id: r for r in storage.load_routes()}
        current = self.sport_filter.currentData() or ALL_SPORTS
        sports = sorted({a.sport for a in self.all_activities})
        self.sport_filter.blockSignals(True)
        self.sport_filter.clear()
        self.sport_filter.addItem("All sports", ALL_SPORTS)
        for sport in sports:
            self.sport_filter.addItem(sport.title(), sport)
        index = self.sport_filter.findData(current)
        self.sport_filter.setCurrentIndex(max(index, 0))
        self.sport_filter.blockSignals(False)
        self._render_table()

    def _render_table(self) -> None:
        sport = self.sport_filter.currentData() or ALL_SPORTS
        self.shown = sorted(
            (a for a in self.all_activities if sport == ALL_SPORTS or a.sport == sport),
            key=lambda a: a.start_time_local, reverse=True,
        )
        rows = []
        for activity in self.shown:
            route = self.routes_by_id.get(activity.route_id)
            rows.append(display.activity_row(activity, route.name if route else "–"))
        fill_table(self.table, rows, activity_styles(self.shown), right_aligned={display.ACTIVITY_DURATION_COLUMN})
        self.status.setText(display.activities_status(len(self.shown), len(self.all_activities), self.hidden_count))

    def _open(self, row: int) -> None:
        if 0 <= row < len(self.shown):
            self.window_.push(activity_page(self.window_, self.shown[row], self.client))

    def _run_job(self, job) -> None:
        self.sync_button.setEnabled(False)
        self.reprocess_button.setEnabled(False)

        def done(message: str) -> None:
            self.sync_button.setEnabled(True)
            self.reprocess_button.setEnabled(True)
            self.status.setText(message)
            self.refresh_table()
            self.notify(message)

        def failed(error: Exception) -> None:
            done(f"Failed: {error}")

        run_background(job, done, failed)

    def _progress(self, message: str) -> None:
        on_ui(self.status.setText, message)

    def sync(self) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin", "warning")
            return
        self._run_job(lambda: services.sync_activities(self.client, progress=self._progress))

    def sync_history(self) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin", "warning")
            return
        if QMessageBox.question(self, "Sync full history", services.FULL_HISTORY_PROMPT) == QMessageBox.StandardButton.Yes:
            self._run_job(lambda: services.sync_activities(self.client, progress=self._progress, full_history=True))

    def reprocess(self) -> None:
        self._run_job(lambda: services.reprocess_activities(progress=self._progress))

    def routes(self) -> None:
        self.window_.push(RoutesPage(self.window_, self.client))

    def workouts(self) -> None:
        self.window_.push(WorkoutsPage(self.window_, self.client))

    def calendar(self) -> None:
        self.window_.push(TrainingWeekPage(self.window_, self.client))

    def on_shown(self) -> None:
        self.refresh_table()


class SettingsPage(Page):
    """The settings in settings.FIELDS as a form (data/settings.json)."""

    title = "Settings"

    def __init__(self, window) -> None:
        super().__init__(window)
        values = settings.load()
        self.checks: dict[str, QCheckBox | dict[str, QCheckBox]] = {}
        for field in settings.FIELDS:
            if field.kind == "bool":
                check = QCheckBox(field.label)
                check.setChecked(values[field.key])
                self.checks[field.key] = check
                self.layout_.addWidget(check)
            else:
                self.layout_.addWidget(label(field.label, "pageTitle"))
                boxes = {}
                for sport in settings.SPORT_GROUPS:
                    box = QCheckBox(sport)
                    box.setChecked(sport in values[field.key])
                    boxes[sport] = box
                self.checks[field.key] = boxes
                self.layout_.addLayout(row_layout(*boxes.values()))
            self.layout_.addWidget(label(field.help, "status", wrap=True))
        self.layout_.addLayout(row_layout(
            button("Save (Ctrl+S)", self.save, primary=True), button("Cancel (esc)", self.window_.pop),
        ))
        self.layout_.addStretch(1)
        self.bind_back()
        self.bind("Ctrl+S", "Save", self.save)

    def save(self) -> None:
        values = {}
        for field in settings.FIELDS:
            checks = self.checks[field.key]
            if field.kind == "bool":
                values[field.key] = checks.isChecked()
            else:
                values[field.key] = [sport for sport, box in checks.items() if box.isChecked()]
        settings.save(values)
        self.notify(f"Settings saved to {storage.SETTINGS_PATH}")
        self.window_.pop()


# --- Laps ----------------------------------------------------------------------------


def activity_page(window, activity: ActivitySummary, client) -> Page:
    """The page an activity opens on: its strength sets for a gym session (the laps page is
    one key away), its laps otherwise."""
    if services.is_strength(activity):
        return StrengthSetsPage(window, activity, client)
    return LapsPage(window, activity, client)


def switch_page(page: Page, other_type: type, make) -> None:
    """Show the other view of the same activity: back to it when it's the page below (so
    sets <-> laps toggles instead of stacking), else pushed on top."""
    stack = page.window_.stack
    below = stack.widget(stack.count() - 2) if stack.count() > 1 else None
    if isinstance(below, other_type) and below.activity.activity_id == page.activity.activity_id:
        page.window_.pop()
    else:
        page.window_.push(make())


class LapsPage(Page):
    STYLE_BY_KIND = {
        "interval": {"bold": True}, "phase": {"dim": True, "italic": True}, "plain": {},
        "header": {"bold": True, "color": COLORS["accent"], "background": COLORS["surface2"]},
    }

    def __init__(self, window, activity: ActivitySummary, client) -> None:
        super().__init__(window)
        self.title = activity.name
        self.activity = activity
        self.client = client
        self.session = LapsSession(activity)
        a = activity
        heading = label(
            f"<span style='color:{SPORT_COLORS.get(a.sport, COLORS['fg'])}; font-weight:600'>{a.sport}</span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{a.start_time_local.replace('T', ' ')[:16]}", "status",
        )
        activity_tiles = StatRow()
        activity_tiles.set_stats(display.activity_stats(a))
        self.metric_tiles = StatRow()  # filled once the record stream is loaded
        self.metric_tiles.setVisible(False)
        self.split_input = QLineEdit(placeholderText="400 = metres · 60s / 1m / 1:30 = time")
        self.split_input.returnPressed.connect(lambda: self._run(lambda: self.session.split(self.split_input.text())))
        self.filter_button = button("Interval only (f)", self.toggle_filter, checkable=True)
        self.table = make_table(self.session.columns)
        self.interval_tiles = StatRow()
        self.interval_hint = label("", "status")
        self.layout_.addWidget(heading)
        self.layout_.addWidget(activity_tiles)
        self.layout_.addWidget(self.metric_tiles)
        if self.session.multisport:
            legs_row = QHBoxLayout()
            legs_row.setSpacing(10)
            for name, sport, stats in display.leg_stats(storage.load_laps(a.activity_id)):
                legs_row.addWidget(LegCard(name, sport, stats), 1)
            self.layout_.addLayout(legs_row)
        self.layout_.addLayout(row_layout(
            self.split_input,
            button("Split", lambda: self._run(lambda: self.session.split(self.split_input.text()))),
            button("Split intervals (i)", self.split_intervals),
            self.filter_button,
            button("Reset", lambda: self._run(self.session.reset)),
            stretch_index=0,
        ))
        # The table only needs part of the width: the rest shows the track, with the selected
        # row's lap highlighted (g still opens the full map page).
        self.side_map = TrackMap()
        self.side_map.setMinimumHeight(200)
        self.side_map.set_placeholder("Loading track...")
        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.side_map)
        splitter.setStretchFactor(1, 1)
        self.table.currentCellChanged.connect(lambda row, *_: self.side_map.set_selected(self.session.lap_coords(self.session.row_lap(row))))
        self.layout_.addWidget(splitter, 1)
        QTimer.singleShot(0, self._fit_table_width)
        self.layout_.addWidget(self.interval_tiles)
        self.layout_.addWidget(self.interval_hint)
        self.bind_back()
        self.bind("m", "Mark interval", self.toggle_interval)
        self.bind("z", "Zones", lambda: self.window_.push(ZonesPage(self.window_, activity, client)))
        self.bind("c", "Compare intervals", self.compare)
        self.bind("p", "Re-detect loops", self.physical_laps)
        self.bind("i", "Split intervals", self.split_intervals)
        self.bind("f", "Interval-only", self.toggle_filter)
        self.bind("h", "Phase time/pace/power", self.histograms)
        self.bind("t", "Strength sets", self.strength_sets)
        self.bind("g", "Map", self.open_map)
        self.bind("y", "Copy table", lambda: copy_text(self, table_as_tsv(self.table), f"{self.table.rowCount()} laps"))
        self._rebuild()
        # First open of a long activity parses its FIT file (seconds): load off the UI thread,
        # then fill the PHYSICAL section (seconds again on a long ride).
        run_background(self.session.map_layers, self._track_loaded, self._track_failed)

    def _track_loaded(self, _) -> None:
        self._refresh_side_map(refit=True)
        stats = display.activity_metric_stats(self.session.advanced_metrics())
        if stats:
            self.metric_tiles.set_stats(stats)
            self.metric_tiles.setVisible(True)
        if not self.session.auto_detect:
            return
        run_background(
            self.session.detect_physical_laps,
            lambda laps: self._run(lambda: self.session.apply_physical_laps(laps, explicit=False)),
            lambda e: self.notify(f"Loop detection failed: {e}", "error"),
        )

    def _track_failed(self, error) -> None:
        self.side_map.set_placeholder(str(error))
        self.session.physical_laps_failed()
        self._rebuild()

    def focus_widget(self):
        return self.table

    def _fit_table_width(self) -> None:
        """Give the table exactly the width its columns need; the map takes the rest."""
        needed = self.table.horizontalHeader().length() + self.table.verticalScrollBar().sizeHint().width() + 6
        total = self.splitter.width()
        self.splitter.setSizes([min(needed, total - 320), max(320, total - needed)])

    def _refresh_side_map(self, refit: bool = False) -> None:
        if self.session.records is None:
            return
        layers = self.session.map_layers(selected_lap=self.session.row_lap(self.table.currentRow()))
        if layers is None:
            self.side_map.set_placeholder("No GPS track for this activity")
            return
        self.side_map.set_track(track_data(layers, "speed_mps", self.activity.sport), refit=refit)

    def _rebuild(self) -> None:
        rows = self.session.rows()
        fill_table(
            self.table, [r.values for r in rows], [self.STYLE_BY_KIND[r.kind] for r in rows],
            right_aligned=self.session.right_aligned_columns,
        )
        hint = self.session.take_cursor_hint()
        if hint is not None and rows:
            self.table.selectRow(min(hint, len(rows) - 1))
        self._refresh_side_map()
        stats = display.interval_stats(self.session.marked_laps(), self.activity.sport)
        self.interval_tiles.set_stats(stats, "Marked\nintervals")
        self.interval_hint.setText("" if stats else self.session.interval_summary())
        self.interval_hint.setVisible(not stats)

    def _run(self, operation) -> bool:
        try:
            message = operation()
        except LapsError as e:
            self.notify(str(e), "warning")
            return False
        self._rebuild()
        if message:
            self.notify(message)
        return True

    def toggle_interval(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self._run(lambda: self.session.toggle_interval(row))

    def split_intervals(self) -> None:
        self._run(lambda: self.session.split_intervals(self.split_input.text()))

    def toggle_filter(self) -> None:
        def toggle():
            warning = self.session.toggle_filter()
            self.filter_button.setChecked(self.session.show_intervals_only)
            if warning:
                self.notify(warning, "warning")
        self._run(toggle)

    def physical_laps(self) -> None:
        try:
            radius = self.session.physical_laps_radius(self.split_input.text())
        except LapsError as e:
            self.notify(str(e), "warning")
            return
        self.notify("Detecting course loops...")
        run_background(
            lambda: self.session.detect_physical_laps(radius),
            lambda laps: self._run(lambda: self.session.apply_physical_laps(laps)),
            lambda e: self.notify(f"Loop detection failed: {e}", "error"),
        )

    def compare(self) -> None:
        try:
            marked, records, offset = self.session.compare_inputs()
        except LapsError as e:
            self.notify(str(e), "warning")
            return
        self.window_.push(ComparePage(self.window_, self.activity, marked, records, offset))

    def open_map(self) -> None:
        selected = self.session.row_lap(self.table.currentRow())

        def show(_=None) -> None:
            try:
                page = MapPage(self.window_, self.session, selected)
            except LapsError as e:
                self.notify(str(e), "warning")
                return
            self.window_.push(page)

        if self.session.records is not None:
            show()
            return
        # First open of this activity: parsing a long FIT file takes seconds — keep the UI live.
        self.notify("Loading track...")
        run_background(self.session.ensure_records, show, lambda e: self.notify(str(e), "warning"))

    def strength_sets(self) -> None:
        if not services.is_strength(self.activity):
            self.notify("Not a strength activity", "warning")
            return
        switch_page(self, StrengthSetsPage, lambda: StrengthSetsPage(self.window_, self.activity, self.client))

    def histograms(self) -> None:
        try:
            phases = self.session.histogram_phases()
        except LapsError as e:
            self.notify(str(e), "warning")
            return
        self.window_.push(PhaseMetricsPage(self.window_, self.activity, phases))


def track_data(layers, metric: str, sport: str) -> TrackData:
    label_text, fmt = display.metric_legend(metric, sport)
    return TrackData(
        points=layers.points, values=layers.values, value_label=label_text, value_format=fmt,
        highlights=layers.highlights, selected=layers.selected, lap_markers=layers.markers,
    )


class MapPage(Page):
    """GUI-only: the activity's GPS track on a map (see gui_map), coloured by a metric, with
    marked intervals outlined and the lap selected on the laps page highlighted."""

    def __init__(self, window, session: LapsSession, selected_lap) -> None:
        super().__init__(window)
        self.title = f"Map — {session.activity.name}"
        self.session = session
        self.selected_lap = selected_lap
        if session.map_layers() is None:  # records are already loaded by the laps page
            raise LapsError("No GPS track for this activity (e.g. a pool swim or indoor session)")
        caption = f"{session.activity.name}  ·  {len(session.marked_laps())} marked interval(s) outlined"
        if selected_lap is not None:
            caption += "  ·  selected lap highlighted in cyan"
        self.mode_buttons = {}
        group = QButtonGroup(self)
        modes = list(enumerate(MAP_METRICS.items(), start=1))
        for key, (metric, text) in modes:
            b = button(f"{text} ({key})", lambda _=False, m=metric: self.set_mode(m), checkable=True)
            group.addButton(b)
            self.mode_buttons[metric] = b
        self.map = TrackMap()
        self.layout_.addWidget(label(caption, "status"))
        self.layout_.addLayout(row_layout(
            *self.mode_buttons.values(), button("Fit (0)", self.map.fit), button("+", self.map.zoom_in), button("−", self.map.zoom_out),
        ))
        self.layout_.addWidget(self.map, 1)
        self.bind_back()
        for key, (metric, text) in modes:
            self.bind(str(key), text, lambda m=metric: self.set_mode(m))
        self.bind("0", "Fit track", self.map.fit)
        self.bind("+", "Zoom in", self.map.zoom_in)
        self.bind("-", "Zoom out", self.map.zoom_out)
        self.set_mode("speed_mps", refit=True)

    def set_mode(self, metric: str, refit: bool = False) -> None:
        self.mode_buttons[metric].setChecked(True)
        layers = self.session.map_layers(metric, self.selected_lap)
        self.map.set_track(track_data(layers, metric, self.session.activity.sport), refit=refit)

    def focus_widget(self):
        return self.map


class ZonesPage(Page):
    def __init__(self, window, activity: ActivitySummary, client) -> None:
        super().__init__(window)
        self.title = f"Zones — {activity.name}"
        self.status = label(self.title, "status")
        self.hr_table = make_table(display.ZONE_COLUMNS)
        self.power_table = make_table(display.ZONE_COLUMNS)
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(label("Heart Rate", "pageTitle"))
        self.layout_.addWidget(self.hr_table, 1)
        self.layout_.addWidget(label("Power", "pageTitle"))
        self.layout_.addWidget(self.power_table, 1)
        self.bind_back()
        cached = storage.load_zones(activity.activity_id)
        if cached is not None:
            self._render(cached)
        elif client is None:
            self.status.setText("Not logged in — zones aren't cached for this activity")
        else:
            self.status.setText("Fetching zones from Garmin Connect...")

            def fetch():
                hr = garmin_client.get_hr_zones(client, activity.activity_id)
                power = garmin_client.get_power_zones(client, activity.activity_id)
                storage.save_zones(activity.activity_id, hr, power)
                return hr, power

            run_background(fetch, self._render, lambda e: self._error(f"Couldn't fetch zones: {e}"))

    def focus_widget(self):
        return self.hr_table

    def _render(self, zones) -> None:
        hr_zones, power_zones = zones
        self.status.setText(self.title)
        fill_table(self.hr_table, display.zone_rows(hr_zones))
        fill_table(self.power_table, display.zone_rows(power_zones))

    def _error(self, message: str) -> None:
        self.status.setText(message)
        self.notify(message, "error")


class StrengthSetsPage(Page):
    """A strength activity's sets (exercise, reps, weight) as recorded by the watch."""

    def __init__(self, window, activity: ActivitySummary, client) -> None:
        super().__init__(window)
        self.activity = activity
        self.client = client
        self.title = f"Strength sets — {activity.name}"
        self.status = label(self.title, "status")
        self.summary = label("", "notes", wrap=True)
        self.table = make_table(display.SET_COLUMNS, stretch_column=1)
        tiles = StatRow()
        tiles.set_stats(display.activity_stats(activity))
        self.layout_.addWidget(tiles)
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(self.summary)
        self.layout_.addWidget(self.table, 1)
        self.bind_back()
        self.bind("l", "Laps & HR", lambda: switch_page(self, LapsPage, lambda: LapsPage(self.window_, activity, client)))
        self.bind("z", "Zones", lambda: self.window_.push(ZonesPage(self.window_, activity, client)))
        self.bind("y", "Copy table", lambda: copy_text(self, table_as_tsv(self.table), f"{self.table.rowCount()} sets"))
        cached = storage.load_exercise_sets(activity.activity_id)
        if cached is not None:
            self._render(cached)
        elif client is None:
            self.status.setText("Not logged in — sets aren't downloaded for this activity")
        else:
            self.status.setText("Fetching sets from Garmin Connect...")
            run_background(
                lambda: services.exercise_sets(client, activity.activity_id), self._render,
                lambda e: self.notify(f"Couldn't fetch sets: {e}", "error"),
            )

    def focus_widget(self):
        return self.table

    def _render(self, sets: list[dict]) -> None:
        self.status.setText(self.title)
        self.summary.setText(display.strength_summary(sets))
        fill_table(self.table, display.strength_rows(sets), right_aligned={2, 3, 4, 5, 6})


class ComparePage(Page):
    X_MODES = (("Time", "time"), ("Distance", "distance"), ("%", "percent"))

    def __init__(self, window, activity, marked_laps, records, utc_offset) -> None:
        super().__init__(window)
        self.title = f"Comparing {len(marked_laps)} intervals — {activity.name}"
        self.marked_laps, self.records, self.utc_offset = marked_laps, records, utc_offset
        self.x_mode = "time"
        self.layout_.addWidget(label(self.title, "status"))
        group = QButtonGroup(self)
        buttons = []
        for text, mode in self.X_MODES:
            b = button(text, lambda _=False, m=mode: self._set_mode(m), checkable=True)
            b.setChecked(mode == self.x_mode)
            group.addButton(b)
            buttons.append(b)
        self.layout_.addLayout(row_layout(*buttons))
        self.charts = []
        for _ in display.COMPARE_METRICS:
            view, chart = make_chart_view()
            chart.legend().setAlignment(Qt.AlignmentFlag.AlignTop)
            self.layout_.addWidget(view, 1)
            self.charts.append(chart)
        self.bind_back()
        self._redraw()

    def _set_mode(self, mode: str) -> None:
        self.x_mode = mode
        self._redraw()

    def _redraw(self) -> None:
        last = len(self.charts) - 1
        for i, (chart, (attr, title, scale)) in enumerate(zip(self.charts, display.COMPARE_METRICS)):
            series = display.compare_series(self.marked_laps, self.records, self.utc_offset, attr, self.x_mode, scale)
            # Same laps on every chart: one legend (top) and one x-axis title (bottom) are enough,
            # leaving each plot the height its axis labels need.
            plot_lines(chart, title, series, display.COMPARE_X_AXIS_LABELS[self.x_mode] if i == last else "", legend=i == 0)


class PhaseMetricsPage(Page):
    COLORS_BY_CHART = (COLORS["accent"], COLORS["green"], COLORS["magenta"])

    def __init__(self, window, activity, phases) -> None:
        super().__init__(window)
        self.title = f"Phase time/pace/power — {activity.name}"
        self.histogram_data = dict(display.phase_histograms(phases, activity.sport))
        self.layout_.addWidget(label(self.title, "status"))
        for (title, rows), color in zip(self.histogram_data.items(), self.COLORS_BY_CHART):
            view, chart = make_chart_view()
            plot_bars(chart, title, rows, color, "Phases")
            self.layout_.addWidget(view, 1)
        self.bind_back()
        self.bind("y", "Copy data", self.copy)

    def copy(self) -> None:
        copy_text(self, display.histograms_tsv(self.histogram_data), f"{len(self.histogram_data)} histograms")


# --- Routes --------------------------------------------------------------------------


class RoutesPage(Page):
    title = "Routes"

    def __init__(self, window, client) -> None:
        super().__init__(window)
        self.client = client
        self.status = label("Routes", "status")
        self.rename_input = QLineEdit(placeholderText="New name")
        self.rename_input.returnPressed.connect(self.rename)
        self.table = make_table(display.ROUTE_COLUMNS, stretch_column=0)
        self.table.cellActivated.connect(lambda row, _: self._open(row))
        self.table.currentCellChanged.connect(lambda row, *_: self._show_route(row))
        self.map = TrackMap()
        self.map_caption = label("", "status")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(row_layout(self.rename_input, button("Rename", self.rename), stretch_index=0))
        left_layout.addWidget(self.table, 1)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.map_caption)
        right_layout.addWidget(self.map, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([560, 700])
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(splitter, 1)
        self.bind_back()
        self.bind("e", "Rename route", self.rename)
        self.bind("0", "Fit map", self.map.fit)
        self._refresh()

    def focus_widget(self):
        return self.table

    def _refresh(self) -> None:
        self.all_routes = storage.load_routes()
        _, activities = storage.load_index()
        self.grouped = display.grouped_routes(self.all_routes, activities)
        fill_table(self.table, [display.route_row(r, n) for r, n in self.grouped])
        self.status.setText(f"{len(self.grouped)} routes with 2+ activities — select one to see it on the map, Enter to list its activities")
        self._show_route(self.table.currentRow())

    def _show_route(self, row: int) -> None:
        if not 0 <= row < len(self.grouped):
            return
        route, count = self.grouped[row]
        points = [(lat, lon) for lat, lon in route.signature.points]
        self.map_caption.setText(
            f"<b>{route.name}</b> · {route.sport} · {route.place or 'unknown place'} · "
            f"{format_distance(route.signature.distance_m)} · {count} activities"
        )
        # The stored signature is a coarse 20-point outline: show it at once, then replace it with
        # the full GPS track of the route's latest activity, parsed off the UI thread.
        self.map.set_track(TrackData(points=points, values=[None] * len(points)), refit=True)
        _, activities = storage.load_index()
        latest = max((a for a in activities if a.route_id == route.route_id), key=lambda a: a.start_time_local, default=None)
        if latest is None or not storage.has_fit(latest.activity_id):
            return

        def load_track():
            records = fit_parser.load_records(latest.activity_id, storage.fit_path(latest.activity_id).read_bytes())
            return [(r.lat, r.lon) for r in records if r.lat is not None and r.lon is not None]

        def show(track):
            if self.table.currentRow() == row and len(track) > len(points):  # still the selected route
                self.map.set_track(TrackData(points=track, values=[None] * len(track)), refit=False)
                self.map_caption.setText(self.map_caption.text() + f" · track of {latest.start_time_local[:10]}")

        run_background(load_track, show, lambda e: None)

    def rename(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.grouped):
            return
        new_name = self.rename_input.text().strip()
        if not new_name:
            self.notify("Enter a new name", "warning")
            return
        self.grouped[row][0].name = new_name
        storage.save_routes(self.all_routes)
        self.rename_input.clear()
        self._refresh()

    def _open(self, row: int) -> None:
        if 0 <= row < len(self.grouped):
            self.window_.push(RouteActivitiesPage(self.window_, self.grouped[row][0], self.client))


class RouteActivitiesPage(Page):
    def __init__(self, window, route, client) -> None:
        super().__init__(window)
        self.title = f"Route: {route.name}"
        self.client = client
        _, activities = storage.load_index()
        self.activities = sorted((a for a in activities if a.route_id == route.route_id), key=lambda a: a.start_time_local, reverse=True)
        self.table = make_table(display.ACTIVITY_COLUMNS, stretch_column=1)
        self.table.cellActivated.connect(lambda row, _: self.window_.push(activity_page(self.window_, self.activities[row], client)))
        self.layout_.addWidget(label(f"Route: {route.name}  |  {len(self.activities)} activities", "status"))
        self.layout_.addWidget(self.table, 1)
        fill_table(
            self.table, [display.activity_row(a, route.name) for a in self.activities], activity_styles(self.activities),
            right_aligned={display.ACTIVITY_DURATION_COLUMN},
        )
        self.bind_back()

    def focus_widget(self):
        return self.table


# --- Workouts ------------------------------------------------------------------------


class WorkoutsPage(Page):
    title = "Workouts"

    def __init__(self, window, client) -> None:
        super().__init__(window)
        self.client = client
        self.library = WorkoutLibrary()
        self.status = label("", "status")
        self.table = make_table(workout_library.COLUMNS, stretch_column=2)
        self.table.currentCellChanged.connect(lambda *_: self._show_preview())
        self.table.cellActivated.connect(lambda row, _: self._activated(row))
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setObjectName("card")
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(self.table, 3)
        self.layout_.addWidget(self.preview, 2)
        self.bind_back()
        self.bind("u", "Upload workout", self.upload)
        self.bind("Shift+U", "Upload week", self.upload_week, display_key="U")
        self.bind("x", "Expand/collapse week", lambda: self._toggle(self._selected_key()))
        self.bind("r", "Refresh", self.refresh)
        self.refresh()

    def focus_widget(self):
        return self.table

    def _selected_key(self) -> str | None:
        row = self.table.currentRow()
        return self.library.rows[row].key if 0 <= row < len(self.library.rows) else None

    def refresh(self) -> None:
        previous = self._selected_key()  # before the library's rows change
        for message in self.library.refresh():
            self.notify(message, "information" if message.startswith("Unpacked") else "error")
        self._fill(previous)

    def _activated(self, row: int) -> None:
        # Enter / double-click on a week header expands or collapses it.
        rows = self.library.rows
        if 0 <= row < len(rows) and rows[row].kind == "week":
            self._toggle(rows[row].key)

    def _toggle(self, key: str | None) -> None:
        if key is None or not self.library.toggle_week(key):
            self.notify("Not part of a week plan", "warning")
            return
        self._fill(select_key=f"{workout_library.WEEK_KEY_PREFIX}{self.library.week_of(key)}")

    def _fill(self, select_key: str | None) -> None:
        rows = self.library.rows
        styles = []
        for row in rows:
            if row.kind == "week":
                styles.append({"bold": True, "color": COLORS["accent"], "color_columns": {4: COLORS["red"]} if row.values[4] != "OK" else {}})
            elif row.kind == "invalid":
                styles.append({"color_columns": {4: COLORS["red"]}})
            else:
                styles.append({"color_columns": {4: COLORS["green"]}})
        fill_table(self.table, [r.values for r in rows], styles)
        keys = [r.key for r in rows]
        if select_key in keys:
            self.table.selectRow(keys.index(select_key))
        self.status.setText(self.library.status_text)
        self._show_preview()

    def _show_preview(self) -> None:
        text, is_error = self.library.preview(self._selected_key())
        self.preview.setStyleSheet(f"color: {COLORS['red']};" if is_error else "")
        self.preview.setPlainText(text)

    def upload(self) -> None:
        try:
            items, warning = self.library.single_upload(self._selected_key())
        except LibraryError as e:
            self.notify(str(e), e.severity)
            return
        if warning:
            self.notify(warning, "warning")
        self._upload(items)

    def upload_week(self) -> None:
        try:
            items, prompt = self.library.week_upload(self._selected_key())
        except LibraryError as e:
            self.notify(str(e), e.severity)
            return
        answer = QMessageBox.question(self, "Upload week to Garmin Connect", prompt)
        if answer == QMessageBox.StandardButton.Yes:
            self._upload(items)

    def _upload(self, items) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin", "warning")
            return

        def done(notes) -> None:
            for severity, message in notes:
                self.notify(message, severity)
            self.refresh()

        run_background(
            lambda: workout_library.upload_many(self.client, items, progress=lambda m: on_ui(self.status.setText, m)),
            done,
            lambda e: self.notify(f"Upload failed: {e}", "error"),
        )


# --- Training week -------------------------------------------------------------------


class TrainingWeekPage(Page):
    title = "Calendar & training load"

    def __init__(self, window, client) -> None:
        super().__init__(window)
        self.client = client
        self.week = training_load.week_start(date.today())
        self.view = None
        self.week_title = label("", "pageTitle")
        # The week's rows are the page's main part: a one-line stat strip and a slim TSS bar
        # above them, a shorter fitness chart below.
        self.strip = StatStrip()
        self.tss_bar = StackedBar(bar_height=16)
        self.table = make_table(display.WEEK_COLUMNS, stretch_column=4)
        self.notes = label("", "notes", wrap=True)
        chart_view, self.chart = make_chart_view()
        chart_view.setMinimumHeight(170)
        self.layout_.addLayout(row_layout(
            button("◀ Previous ([)", self.prev_week), self.week_title, button("Next (]) ▶", self.next_week),
            button("This week (t)", self.this_week), button("Refresh (r)", self.refresh), stretch_index=1,
        ))
        self.layout_.addWidget(self.strip)
        self.layout_.addWidget(self.tss_bar)
        self.layout_.addWidget(self.table, 5)
        self.layout_.addWidget(self.notes)
        self.layout_.addWidget(chart_view, 2)
        self.bind_back()
        self.bind("[", "Prev week", self.prev_week)
        self.bind("]", "Next week", self.next_week)
        self.bind("t", "This week", self.this_week)
        self.bind("r", "Refresh from Garmin", self.refresh)
        self.bind("m", "Move workout", self.move_workout)
        self.bind("x", "Cancel workout", self.cancel_workout)
        self.bind("l", "Link/unlink plan", self.link)
        self.bind("e", "Export weeks", self.export)
        self.bind("y", "Copy table", self.copy)
        self._load_view()
        self.refresh()

    def focus_widget(self):
        return self.table

    def _load_view(self, keep_row: bool = False) -> None:
        self.view = services.load_training_week(self.week)
        today = date.today()
        self.week_title.setText(display.week_title(self.week))
        load, load_metrics, caption = display.week_strip_stats(self.view)
        self.strip.set_groups([(load, True), (load_metrics, False)], caption)
        parts = display.week_tss_parts(self.view)
        self.tss_bar.set_parts(
            display.week_tss_caption(self.view),
            [("done", parts["done"], COLORS["green"]), ("remaining", parts["remaining"], COLORS["accent"]),
             ("missed", parts["missed"], COLORS["red"])],
        )
        rows, styles = [], []
        for item in self.view.visible_items:
            cells, color = display.week_item_cells(item, today)
            rows.append(cells)
            styles.append({
                "bold": item.day == today,
                "color_columns": {2: COLORS.get(color, COLORS["fg"]) if color else COLORS["fg"], 3: SPORT_COLORS.get(item.sport, COLORS["fg"])},
            })
        if not rows:
            rows, styles = [("", "", "", "", "Nothing planned or done this week", "", "", "", "")], None
        fill_table(self.table, rows, styles, right_aligned={5, 7}, keep_row=keep_row)
        notes = list(self.view.notes)
        if not self.view.calendar_cached:
            notes.insert(0, "This week's calendar isn't downloaded yet — refreshing...")
        self.notes.setText("\n".join(notes))
        self.notes.setVisible(bool(notes))
        points = self.view.pmc
        markers = [(today, COLORS["comment"])]
        if self.week != training_load.week_start(today):
            markers.append((self.week, COLORS["cyan"]))
        plot_dated_lines(
            self.chart,
            "Fitness (CTL) / fatigue (ATL) / form (TSB) — dashed: today; after it: planned",
            [p.day for p in points],
            [
                ("CTL fitness", [p.ctl for p in points], COLORS["accent"]),
                ("ATL fatigue", [p.atl for p in points], COLORS["magenta"]),
                ("TSB form", [p.tsb for p in points], COLORS["yellow"]),
            ],
            markers,
            dashed_after=today,
        )

    def _change_week(self, week: date) -> None:
        self.week = week
        self._load_view()
        if not self.view.calendar_cached:
            self.refresh()

    def prev_week(self) -> None:
        self._change_week(self.week - timedelta(days=7))

    def next_week(self) -> None:
        self._change_week(self.week + timedelta(days=7))

    def this_week(self) -> None:
        self._change_week(training_load.week_start(date.today()))

    def refresh(self) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin — showing cached data", "warning")
            return
        week = self.week

        def done(warnings) -> None:
            for message in warnings:
                self.notify(message, "warning")
            self._load_view()

        def failed(error) -> None:
            self.notify(f"Refresh failed: {error}", "error")
            self._load_view()

        run_background(
            lambda: services.refresh_training_data(self.client, week, progress=lambda m: on_ui(self.week_title.setText, m)),
            done,
            failed,
        )

    def _selected_item(self) -> training_load.WeekItem | None:
        row = self.table.currentRow()
        items = self.view.visible_items if self.view else []
        return items[row] if 0 <= row < len(items) else None

    def _choose(self, title: str, prompt: str, options: list[str]) -> int | None:
        text, ok = QInputDialog.getItem(self, title, prompt, options, 0, False)
        return options.index(text) if ok and text in options else None

    def _confirm(self, title: str, prompt: str) -> bool:
        return QMessageBox.question(self, title, prompt) == QMessageBox.StandardButton.Yes

    def _garmin_change(self, operation) -> None:
        def failed(error) -> None:
            if isinstance(error, services.CalendarError):
                self.notify(str(error), "warning")
            else:
                self.notify(f"Garmin change failed: {error}", "error")
            self._load_view(keep_row=True)

        def done(message) -> None:
            self.notify(message)
            self._load_view(keep_row=True)

        run_background(operation, done, failed)

    def _local_change(self, operation) -> None:
        try:
            self.notify(operation())
        except services.CalendarError as e:
            self.notify(str(e), "warning")
        self._load_view(keep_row=True)

    def move_workout(self) -> None:
        item = self._selected_item()
        try:
            days = services.move_targets(item)
        except services.CalendarError as e:
            self.notify(str(e), "warning")
            return
        if self.client is None:
            self.notify("Not logged in to Garmin", "warning")
            return
        options = [display.week_day_choice(day, self.view.week.items) for day in days]
        index = self._choose("Move workout", services.move_prompt(item), options)
        if index is not None:
            self._garmin_change(lambda: services.move_planned(self.client, item, days[index]))

    def cancel_workout(self) -> None:
        item = self._selected_item()
        try:
            prompt = services.cancel_prompt(item)
        except services.CalendarError as e:
            self.notify(str(e), "warning")
            return
        if self.client is None:
            self.notify("Not logged in to Garmin", "warning")
            return
        if self._confirm("Cancel workout", prompt):
            self._garmin_change(lambda: services.cancel_planned(self.client, item))

    def link(self) -> None:
        item = self._selected_item()
        if item is not None and item.status == "done (planned)":
            try:
                prompt = services.unlink_prompt(item)
            except services.CalendarError as e:
                self.notify(str(e), "warning")
                return
            if self._confirm("Unlink plan", prompt):
                self._local_change(lambda: services.unlink(item))
            return
        try:
            candidates = services.link_candidates(self.view.week, item)
        except services.CalendarError as e:
            self.notify(str(e), "warning")
            return
        index = self._choose("Link plan and activity", services.link_prompt(item), [display.week_item_choice(c) for c in candidates])
        if index is not None:
            self._local_change(lambda: services.link(item, candidates[index]))

    def export(self) -> None:
        index = self._choose("Export weeks", training_export.EXPORT_PROMPT, training_export.choice_labels(date.today()))
        if index is None:
            return
        choice = training_export.CHOICES[index]

        def done(result) -> None:
            path, warnings = result
            for message in warnings:
                self.notify(message, "warning")
            self.notify(f"Exported to {path}")
            self._load_view(keep_row=True)

        def failed(error) -> None:
            self.notify(f"Export failed: {error}", "error")
            self._load_view(keep_row=True)

        run_background(
            lambda: services.export_training(self.client, choice, progress=lambda m: on_ui(self.week_title.setText, m)),
            done,
            failed,
        )

    def copy(self) -> None:
        copy_text(self, display.week_summary_text(self.view) + "\n\n" + table_as_tsv(self.table), f"week {self.week:%d.%m}")
