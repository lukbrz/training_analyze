import math
import shutil
import subprocess
from datetime import date, timedelta

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalGroup, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, OptionList, Select, Static
from textual_plotext import PlotextPlot

import display
import fit_parser
import garmin_client
import services
import settings
import storage
import leaderboard
import training_export
import training_load
import workout_library
from laps_session import LapsError, LapsSession
from models import ActivitySummary, Lap, Route, ZoneTime
from theme import COLORS
from workout_library import LibraryError, WorkoutLibrary

ALL_SPORTS = "__all__"


def _rgb(name: str) -> tuple[int, int, int]:
    """A theme palette colour as the (r, g, b) tuple plotext accepts."""
    value = COLORS[name].lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _activity_cells(activity: ActivitySummary, route_name: str) -> tuple:
    values = list(display.activity_row(activity, route_name))
    values[display.ACTIVITY_DURATION_COLUMN] = Text(values[display.ACTIVITY_DURATION_COLUMN], justify="right")
    return tuple(values)


_CLIPBOARD_COMMANDS = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


def _cell_text(cell) -> str:
    return (cell.plain if isinstance(cell, Text) else str(cell)).strip()


def _table_as_tsv(table: DataTable) -> str:
    """The table exactly as displayed (current filter, phase-split rows and all) as
    tab-separated text, which pastes into a spreadsheet as cells."""
    return display.tsv(
        [_cell_text(col.label) for col in table.columns.values()],
        [[_cell_text(cell) for cell in table.get_row_at(row)] for row in range(table.row_count)],
    )


def _copy_to_clipboard(screen: Screen, text: str, what: str) -> None:
    """Copy via the terminal (OSC 52 — works in most terminals, but some ignore it) and, if
    one is installed, a system clipboard tool too, which works regardless of the terminal."""
    screen.app.copy_to_clipboard(text)
    for command in _CLIPBOARD_COMMANDS:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=5)
            except (subprocess.SubprocessError, OSError):
                continue
            screen.notify(f"Copied {what}")
            return
    screen.notify(f"Copied {what} via the terminal (if pasting gives nothing, install xclip)")


class LoginScreen(Screen):
    CSS = """
    #login_box {
        width: 50;
        margin: 2 4;
    }

    #login_box Input {
        margin-bottom: 1;
    }

    #login_status {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.mfa_pending_garmin = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Log in to Garmin Connect", id="title")
        yield Static("", id="login_status")
        yield Input(placeholder="Email", id="email_input")
        yield Input(placeholder="Password", password=True, id="password_input")
        yield Input(placeholder="MFA code", id="mfa_input")
        yield Button("Log in", id="login_button", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mfa_input", Input).display = False
        self.query_one("#email_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "email_input":
            self.query_one("#password_input", Input).focus()
        else:
            self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login_button":
            self.action_submit()

    def action_submit(self) -> None:
        if self.mfa_pending_garmin is not None:
            code = self.query_one("#mfa_input", Input).value.strip()
            if not code:
                self.notify("Enter the MFA code", severity="warning")
                return
            self._complete_mfa(code)
        else:
            email = self.query_one("#email_input", Input).value.strip()
            password = self.query_one("#password_input", Input).value
            if not email or not password:
                self.notify("Enter email and password", severity="warning")
                return
            self._login(email, password)

    @work(thread=True)
    def _login(self, email: str, password: str) -> None:
        self.app.call_from_thread(self._set_status, "Logging in...")
        self.app.call_from_thread(self._set_inputs_disabled, True)
        try:
            garmin = garmin_client.begin_login(email, password)
        except garmin_client.MfaRequired as exc:
            self.mfa_pending_garmin = exc.garmin
            self.app.call_from_thread(self._show_mfa_step)
            return
        except (
            garmin_client.GarminConnectAuthenticationError,
            garmin_client.GarminConnectConnectionError,
        ) as e:
            self.app.call_from_thread(self._login_failed, str(e))
            return
        self.app.call_from_thread(self._login_succeeded, garmin)

    @work(thread=True)
    def _complete_mfa(self, code: str) -> None:
        self.app.call_from_thread(self._set_status, "Verifying code...")
        self.app.call_from_thread(self._set_inputs_disabled, True)
        try:
            garmin = garmin_client.complete_mfa(self.mfa_pending_garmin, code)
        except (
            garmin_client.GarminConnectAuthenticationError,
            garmin_client.GarminConnectConnectionError,
        ) as e:
            self.app.call_from_thread(self._login_failed, str(e))
            return
        self.app.call_from_thread(self._login_succeeded, garmin)

    def _show_mfa_step(self) -> None:
        self._set_status("Enter the MFA code sent to your device")
        self._set_inputs_disabled(False)
        self.query_one("#email_input", Input).disabled = True
        self.query_one("#password_input", Input).disabled = True
        mfa_input = self.query_one("#mfa_input", Input)
        mfa_input.display = True
        mfa_input.focus()

    def _login_failed(self, message: str) -> None:
        self._set_status("")
        self._set_inputs_disabled(False)
        self.notify(message, severity="error")

    def _login_succeeded(self, garmin) -> None:
        self._set_status("")
        self.app.push_screen(MainScreen(garmin))

    def _set_status(self, text: str) -> None:
        self.query_one("#login_status", Static).update(text)

    def _set_inputs_disabled(self, disabled: bool) -> None:
        self.query_one("#email_input", Input).disabled = disabled
        self.query_one("#password_input", Input).disabled = disabled
        self.query_one("#login_button", Button).disabled = disabled


class ContextMenuMixin:
    """Right-click on a screen's table opens the selected row's menu (display.*_menu):
    a pick list of what applies to that row, each entry running the same action as its
    key. A screen lists its menu items in context_menu() and maps their shared action
    names to its own in MENU_ACTIONS."""

    MENU_ACTIONS: dict[str, str] = {}

    def context_menu(self) -> list[display.MenuItem]:
        return []

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 3:
            return
        # The table moves its cursor to the clicked row first; read the menu after that.
        self.call_after_refresh(self._open_context_menu)

    def _open_context_menu(self) -> None:
        items = [i for i in self.context_menu() if i.enabled and i.action in self.MENU_ACTIONS]
        if not items:
            return

        def chosen(index: int | None) -> None:
            if index is not None:
                self.call_later(self.run_action, self.MENU_ACTIONS[items[index].action])

        self.app.push_screen(ChoiceScreen("Actions for this row:", [i.text for i in items]), chosen)


class MainScreen(ContextMenuMixin, Screen):
    MENU_ACTIONS = {"open": "open_selected", "zones": "zones", "copy": "copy"}

    BINDINGS = [
        Binding("s", "sync", "Sync"),
        Binding("r", "reprocess", "Reprocess"),
        Binding("o", "routes", "Routes"),
        Binding("w", "workouts", "Workouts"),
        Binding("c", "calendar", "Calendar & load"),
        Binding("p", "settings", "Settings"),
        Binding("H", "sync_history", "Sync full history"),
        Binding("y", "copy", "Copy table"),
    ]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.hidden_count = 0
        self.activities_by_id: dict[str, ActivitySummary] = {}
        self.all_activities: list[ActivitySummary] = []
        self.routes_by_id: dict[str, Route] = {}
        self.sport_filter: str = ALL_SPORTS
        self.period_days: int = settings.get("activity_period_days")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield HorizontalGroup(
            Button("Sync (s)", id="sync_button", variant="primary"),
            Button("Reprocess (r)", id="reprocess_button"),
            Button("Routes (o)", id="routes_button"),
            Button("Workouts (w)", id="workouts_button"),
            Button("Calendar (c)", id="calendar_button"),
            Select([(label, days) for days, label in settings.PERIOD_CHOICES], value=self.period_days,
                   allow_blank=False, id="period_select"),
            Select([("All sports", ALL_SPORTS)], value=ALL_SPORTS, id="sport_filter_select"),
        )
        yield DataTable(id="activities_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.ACTIVITY_COLUMNS)
        self._refresh_table()

    def _refresh_table(self) -> None:
        self.all_activities, self.hidden_count = services.listed_activities(self.period_days)
        self.routes_by_id = {r.route_id: r for r in storage.load_routes()}
        self._update_sport_options()
        self._render_table()

    def _update_sport_options(self) -> None:
        sports = sorted({a.sport for a in self.all_activities})
        if self.sport_filter != ALL_SPORTS and self.sport_filter not in sports:
            self.sport_filter = ALL_SPORTS
        select = self.query_one("#sport_filter_select", Select)
        select.set_options([("All sports", ALL_SPORTS)] + [(sport.title(), sport) for sport in sports])
        select.value = self.sport_filter

    def _render_table(self) -> None:
        activities = [
            a for a in self.all_activities if self.sport_filter == ALL_SPORTS or a.sport == self.sport_filter
        ]
        self.activities_by_id = {a.activity_id: a for a in activities}
        table = self.query_one(DataTable)
        table.clear()
        for activity in sorted(activities, key=lambda a: a.start_time_local, reverse=True):
            route = self.routes_by_id.get(activity.route_id)
            table.add_row(
                *_activity_cells(activity, route.name if route else "–"), key=activity.activity_id
            )
        status = display.activities_status(len(activities), len(self.all_activities), self.hidden_count, self.period_days)
        self.query_one("#status", Static).update(status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync_button":
            self.action_sync()
        elif event.button.id == "reprocess_button":
            self.action_reprocess()
        elif event.button.id == "routes_button":
            self.action_routes()
        elif event.button.id == "calendar_button":
            self.action_calendar()
        elif event.button.id == "workouts_button":
            self.action_workouts()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sport_filter_select":
            self.sport_filter = event.value
            self._render_table()
        elif event.select.id == "period_select" and event.value != self.period_days:
            self.period_days = event.value
            settings.save({"activity_period_days": self.period_days})  # the next start opens on it too
            self._refresh_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        activity = self.activities_by_id.get(event.row_key.value)
        if activity is not None:
            self.app.push_screen(activity_screen(activity, self.client))

    def _selected_activity(self) -> ActivitySummary | None:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        return self.activities_by_id.get(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)

    def context_menu(self) -> list[display.MenuItem]:
        activity = self._selected_activity()
        return display.activity_menu(activity) if activity else []

    def action_open_selected(self) -> None:
        activity = self._selected_activity()
        if activity is not None:
            self.app.push_screen(activity_screen(activity, self.client))

    def action_zones(self) -> None:
        activity = self._selected_activity()
        if activity is not None:
            self.app.push_screen(ZonesScreen(activity, self.client))

    def action_sync(self) -> None:
        self._set_buttons_disabled(True)
        self._sync()

    def action_sync_history(self) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin", severity="warning")
            return

        def confirmed(yes: bool) -> None:
            if yes:
                self._set_buttons_disabled(True)
                self._sync(full_history=True)

        self.app.push_screen(ConfirmScreen(services.FULL_HISTORY_PROMPT, "Sync full history"), confirmed)

    def action_settings(self) -> None:
        def saved(changed: bool) -> None:
            if changed:
                self.period_days = settings.get("activity_period_days")
                self.query_one("#period_select", Select).value = self.period_days
                self._refresh_table()

        self.app.push_screen(SettingsScreen(), saved)

    def action_reprocess(self) -> None:
        self._set_buttons_disabled(True)
        self._reprocess()

    def action_routes(self) -> None:
        self.app.push_screen(RoutesScreen(self.client))

    def action_calendar(self) -> None:
        self.app.push_screen(TrainingWeekScreen(self.client))

    def action_workouts(self) -> None:
        self.app.push_screen(WorkoutsScreen(self.client))

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        _copy_to_clipboard(self, _table_as_tsv(table), f"{table.row_count} activities")

    def _set_buttons_disabled(self, disabled: bool) -> None:
        self.query_one("#sync_button", Button).disabled = disabled
        self.query_one("#reprocess_button", Button).disabled = disabled

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    @work(thread=True)
    def _sync(self, full_history: bool = False) -> None:
        message = services.sync_activities(
            self.client, progress=lambda m: self.app.call_from_thread(self._set_status, m), full_history=full_history,
        )
        self.app.call_from_thread(self._sync_done, message)

    def _sync_done(self, message: str) -> None:
        self._set_status(message)
        self._set_buttons_disabled(False)
        self._refresh_table()
        self.notify(message)

    @work(thread=True)
    def _reprocess(self) -> None:
        message = services.reprocess_activities(progress=lambda m: self.app.call_from_thread(self._set_status, m))
        self.app.call_from_thread(self._sync_done, message)


def activity_screen(activity: ActivitySummary, client) -> Screen:
    """The screen an activity opens on: its strength sets for a gym session (the laps view
    is one key away), its laps otherwise."""
    if services.is_strength(activity):
        return StrengthSetsScreen(activity, client)
    return LapsScreen(activity, client)


def _switch_screen(screen: Screen, other_type: type, make) -> None:
    """Show the other view of the same activity: back to it when it's the screen below
    (so sets <-> laps toggles instead of stacking), else pushed on top."""
    stack = screen.app.screen_stack
    if len(stack) > 1 and isinstance(stack[-2], other_type) and stack[-2].activity.activity_id == screen.activity.activity_id:
        screen.app.pop_screen()
    else:
        screen.app.push_screen(make())


class LapsScreen(ContextMenuMixin, Screen):
    """One activity's laps — a Textual shell around laps_session.LapsSession, which owns all
    the state and behaviour (shared with the Qt GUI)."""

    MENU_ACTIONS = {
        "toggle_interval": "toggle_interval", "split_intervals": "split_intervals",
        "toggle_filter": "toggle_interval_filter", "compare": "compare", "phase_metrics": "phase_metrics",
        "physical_laps": "physical_laps", "zones": "zones", "copy": "copy", "leaderboard": "leaderboard",
    }

    CSS = """
    #split_value_input {
        width: 1fr;
    }

    #laps_body {
        height: 1fr;
    }

    #laps_table {
        width: auto;
        max-width: 3fr;
    }

    #track_plot {
        width: 1fr;
        min-width: 30;
    }
    """

    # Below this terminal width the track outline is hidden so the table keeps its columns.
    MIN_WIDTH_FOR_TRACK = 150
    TRACK_POINTS = 1500  # braille resolution doesn't need more

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("m", "toggle_interval", "Mark interval"),
        Binding("z", "zones", "Zones"),
        Binding("c", "compare", "Compare intervals"),
        Binding("p", "physical_laps", "Re-detect loops"),
        Binding("i", "split_intervals", "Split intervals"),
        Binding("f", "toggle_interval_filter", "Interval-only"),
        Binding("h", "phase_metrics", "Phase time/pace/power"),
        Binding("t", "strength_sets", "Strength sets"),
        Binding("b", "leaderboard", "Route leaderboard"),
        Binding("y", "copy", "Copy table"),
    ]

    STYLE_BY_KIND = {
        "interval": "bold", "phase": "dim", "plain": "",
        "header": f"bold {COLORS['accent']} on {COLORS['surface2']}",
    }

    def __init__(self, activity: ActivitySummary, client) -> None:
        super().__init__()
        self.activity = activity
        self.client = client
        self.session = LapsSession(activity)
        self.track_layers = None  # laps_session.MapLayers once records are loaded (None: no GPS)
        self._redraw_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.session.summary_text(), id="activity_summary")
        yield HorizontalGroup(
            Input(placeholder="400 = metres · 60s / 1m / 1:30 = time", id="split_value_input"),
            Button("Split", id="split_button"),
            Button("Split intervals (i)", id="split_intervals_button"),
            Button("Interval only (f)", id="interval_filter_button"),
            Button("Reset", id="reset_button"),
            id="split_bar",
        )
        yield Horizontal(DataTable(id="laps_table"), PlotextPlot(id="track_plot"), id="laps_body")
        yield Static("", id="interval_summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*self.session.columns)
        self._rebuild_table()
        # Without this, Textual auto-focuses the split-by-distance Input, and every
        # single-letter binding (m/z/c/p) silently types into it instead of firing.
        table.focus()
        self._load_track()

    @work(thread=True)
    def _load_track(self) -> None:
        # First open of a long activity parses its FIT file (seconds) — keep the UI live.
        try:
            layers = self.session.map_layers()
        except LapsError:
            self.session.physical_laps_failed()
            self.app.call_from_thread(self._track_loaded, None)
            self.app.call_from_thread(self._rebuild_table)
            return
        self.app.call_from_thread(self._track_loaded, layers)
        if not self.session.auto_detect:
            return
        # Then fill the PHYSICAL section (seconds again on a long ride).
        laps = self.session.detect_physical_laps()
        self.app.call_from_thread(self._run, lambda: self.session.apply_physical_laps(laps, explicit=False))

    def _track_loaded(self, layers) -> None:
        self.track_layers = layers
        self._draw_track()
        if self.session.records is not None:
            extra = display.activity_metrics_text(self.session.advanced_metrics(), self.session.pause_stat())
            if extra:
                self.query_one("#activity_summary", Static).update(f"{self.session.summary_text()}\n{extra}")

    def on_resize(self, event) -> None:
        self.query_one("#track_plot", PlotextPlot).display = self.size.width >= self.MIN_WIDTH_FOR_TRACK

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Debounced: holding an arrow key shouldn't redraw the outline for every row passed.
        if self._redraw_timer is not None:
            self._redraw_timer.stop()
        self._redraw_timer = self.set_timer(0.15, self._draw_track)

    def _draw_track(self) -> None:
        """Braille outline of the GPS track: marked intervals yellow, the selected lap cyan."""
        plot = self.query_one("#track_plot", PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.clear_figure()
        if self.track_layers is None:
            plt.title("No GPS track" if self.session.records is not None else "Loading track...")
            plot.refresh()
            return
        lats = [lat for lat, _ in self.track_layers.points]
        # Scale longitude by cos(latitude) so the outline isn't stretched east-west.
        scale = math.cos(math.radians(sum(lats) / len(lats)))

        def xy(points):
            step = max(1, len(points) // self.TRACK_POINTS)
            sampled = points[::step]
            return [lon * scale for _, lon in sampled], [lat for lat, _ in sampled]

        # Exact RGB colours: textual-plotext's theme remaps named ones ("yellow" draws red).
        plt.plot(*xy(self.track_layers.points), marker="braille", color=_rgb("comment"))
        for highlight in self.track_layers.highlights:
            plt.plot(*xy(highlight), marker="braille", color=_rgb("yellow"))
        selected = self.session.lap_coords(self.session.row_lap(self.query_one(DataTable).cursor_row or 0))
        if len(selected) > 1:
            plt.plot(*xy(selected), marker="braille", color=_rgb("cyan"))
        start, end = xy([self.track_layers.points[0]]), xy([self.track_layers.points[-1]])
        plt.scatter(*start, marker="●", color=_rgb("green"))
        plt.scatter(*end, marker="●", color=_rgb("red"))
        plt.xticks([])
        plt.yticks([])
        plt.title("Track — intervals yellow, selected lap cyan")
        plot.refresh()

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        cursor_row = table.cursor_row
        table.clear()
        right = self.session.right_aligned_columns
        for row in self.session.rows():
            style = self.STYLE_BY_KIND[row.kind]
            table.add_row(*(
                Text(v, style=style, justify="right" if i in right else "left")
                for i, v in enumerate(row.values)
            ))
        # Stay on the same row (e.g. when marking several laps in a row with m), or follow
        # the row an operation moved.
        hint = self.session.take_cursor_hint()
        if table.row_count:
            table.move_cursor(row=min(hint if hint is not None else cursor_row or 0, table.row_count - 1))
        self.query_one("#interval_summary", Static).update(self.session.interval_summary())
        if self.session.records is not None:  # laps/marks changed: refresh the outline's highlights
            self.track_layers = self.session.map_layers()
            self._draw_track()

    def _split_text(self) -> str:
        return self.query_one("#split_value_input", Input).value

    def _run(self, operation) -> bool:
        """Run a session operation; show its LapsError as a warning. True on success."""
        try:
            message = operation()
        except LapsError as e:
            self.notify(str(e), severity="warning")
            return False
        self._rebuild_table()
        if message:
            self.notify(message)
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "split_button":
            self._run(lambda: self.session.split(self._split_text()))
        elif event.button.id == "split_intervals_button":
            self.action_split_intervals()
        elif event.button.id == "interval_filter_button":
            self.action_toggle_interval_filter()
        elif event.button.id == "reset_button":
            self._run(self.session.reset)

    def context_menu(self) -> list[display.MenuItem]:
        return display.lap_menu(self.session.row(self.query_one(DataTable).cursor_row or 0))

    def action_toggle_interval(self) -> None:
        row = self.query_one(DataTable).cursor_row
        if row is not None:
            self._run(lambda: self.session.toggle_interval(row))

    def action_split_intervals(self) -> None:
        self._run(lambda: self.session.split_intervals(self._split_text()))

    def action_toggle_interval_filter(self) -> None:
        def toggle():
            warning = self.session.toggle_filter()
            self.query_one("#interval_filter_button", Button).variant = (
                "primary" if self.session.show_intervals_only else "default"
            )
            if warning:
                self.notify(warning, severity="warning")
        self._run(toggle)

    def action_physical_laps(self) -> None:
        try:
            radius = self.session.physical_laps_radius(self._split_text())
        except LapsError as e:
            self.notify(str(e), severity="warning")
            return
        self.notify("Detecting course loops...")
        self._detect_physical_laps(radius)

    @work(thread=True)
    def _detect_physical_laps(self, radius: float) -> None:
        # Scores every candidate loop anchor across the whole track — can take several
        # seconds on a long activity, so it must not block the UI thread.
        laps = self.session.detect_physical_laps(radius)
        self.app.call_from_thread(self._run, lambda: self.session.apply_physical_laps(laps))

    def action_zones(self) -> None:
        self.app.push_screen(ZonesScreen(self.activity, self.client))

    def action_leaderboard(self) -> None:
        route = next((r for r in storage.load_routes() if r.route_id == self.activity.route_id), None)
        if route is None:
            self.notify("This activity has no route (no GPS track) — sync or reprocess to match one", severity="warning")
            return
        self.app.push_screen(LeaderboardScreen(route, self.client, self.activity.activity_id))

    def action_strength_sets(self) -> None:
        if not services.is_strength(self.activity):
            self.notify("Not a strength activity", severity="warning")
            return
        _switch_screen(self, StrengthSetsScreen, lambda: StrengthSetsScreen(self.activity, self.client))

    def action_compare(self) -> None:
        try:
            marked, records, offset = self.session.compare_inputs()
        except LapsError as e:
            self.notify(str(e), severity="warning")
            return
        self.app.push_screen(CompareScreen(self.activity, marked, records, offset))

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        _copy_to_clipboard(self, _table_as_tsv(table), f"{table.row_count} laps")

    def action_phase_metrics(self) -> None:
        try:
            phases = self.session.histogram_phases()
        except LapsError as e:
            self.notify(str(e), severity="warning")
            return
        self.app.push_screen(PhaseMetricsScreen(self.activity, phases))

    def action_back(self) -> None:
        self.app.pop_screen()


class ZonesScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back", priority=True)]


    def __init__(self, activity: ActivitySummary, client) -> None:
        super().__init__()
        self.activity = activity
        self.client = client

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Zones — {self.activity.name}", id="zones_status")
        yield Static("Heart Rate", classes="zones_label")
        yield DataTable(id="hr_zones_table")
        yield Static("Power", classes="zones_label")
        yield DataTable(id="power_zones_table")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hr_zones_table", DataTable).add_columns(*display.ZONE_COLUMNS)
        self.query_one("#power_zones_table", DataTable).add_columns(*display.ZONE_COLUMNS)

        cached = storage.load_zones(self.activity.activity_id)
        if cached is not None:
            hr_zones, power_zones = cached
            self._render_zones(hr_zones, power_zones)
        else:
            self.query_one("#zones_status", Static).update("Fetching zones from Garmin Connect...")
            self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            hr_zones = garmin_client.get_hr_zones(self.client, self.activity.activity_id)
            power_zones = garmin_client.get_power_zones(self.client, self.activity.activity_id)
        except (
            garmin_client.GarminConnectAuthenticationError,
            garmin_client.GarminConnectConnectionError,
            garmin_client.GarminConnectNotFoundError,
            garmin_client.GarminConnectTooManyRequestsError,
        ) as e:
            self.app.call_from_thread(self._show_error, f"Couldn't fetch zones: {e}")
            return
        storage.save_zones(self.activity.activity_id, hr_zones, power_zones)
        self.app.call_from_thread(self._render_zones, hr_zones, power_zones)

    def _render_zones(self, hr_zones: list[ZoneTime], power_zones: list[ZoneTime]) -> None:
        self.query_one("#zones_status", Static).update(f"Zones — {self.activity.name}")
        self._render_table("#hr_zones_table", hr_zones)
        self._render_table("#power_zones_table", power_zones)

    def _render_table(self, table_id: str, zones: list[ZoneTime]) -> None:
        table = self.query_one(table_id, DataTable)
        table.clear()
        for row in display.zone_rows(zones):
            table.add_row(*row)

    def _show_error(self, message: str) -> None:
        self.query_one("#zones_status", Static).update(message)
        self.notify(message, severity="error")

    def action_back(self) -> None:
        self.app.pop_screen()


class CompareScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back", priority=True)]

    X_MODE_BUTTONS = {
        "x_mode_time_button": "time",
        "x_mode_distance_button": "distance",
        "x_mode_percent_button": "percent",
    }

    def __init__(
        self,
        activity: ActivitySummary,
        marked_laps: list[Lap],
        records: list[fit_parser.RecordPoint],
        offset: timedelta,
    ) -> None:
        super().__init__()
        self.activity = activity
        self.marked_laps = marked_laps
        self.records = records
        self.utc_offset = offset
        self.x_mode = "time"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Comparing {len(self.marked_laps)} intervals — {self.activity.name}", id="compare_status"
        )
        yield HorizontalGroup(
            Button("Time", id="x_mode_time_button", variant="primary"),
            Button("Distance", id="x_mode_distance_button"),
            Button("%", id="x_mode_percent_button"),
            id="x_mode_bar",
        )
        yield Vertical(
            PlotextPlot(id="power_plot"),
            PlotextPlot(id="hr_plot"),
            PlotextPlot(id="speed_plot"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._redraw()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mode = self.X_MODE_BUTTONS.get(event.button.id)
        if mode is None:
            return
        self.x_mode = mode
        for button_id in self.X_MODE_BUTTONS:
            self.query_one(f"#{button_id}", Button).variant = (
                "primary" if button_id == event.button.id else "default"
            )
        self._redraw()

    def _redraw(self) -> None:
        for widget_id, (attr, title, scale) in zip(("power_plot", "hr_plot", "speed_plot"), display.COMPARE_METRICS):
            plot = self.query_one(f"#{widget_id}", PlotextPlot)
            plt = plot.plt
            plt.clear_data()
            series = display.compare_series(self.marked_laps, self.records, self.utc_offset, attr, self.x_mode, scale)
            for label, xs, ys in series:
                plt.plot(xs, ys, label=label)
            plt.title(title if series else f"{title} — no data")
            plt.xlabel(display.COMPARE_X_AXIS_LABELS[self.x_mode])
            plot.refresh()

    def action_back(self) -> None:
        self.app.pop_screen()


class StrengthSetsScreen(Screen):
    """A strength activity's sets (exercise, reps, weight) as recorded by the watch."""

    CSS = """
    #sets_summary {
        padding: 0 1;
        border: round $primary;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("l", "laps", "Laps & HR"),
        Binding("z", "zones", "Zones"),
        Binding("y", "copy", "Copy table"),
    ]

    def __init__(self, activity: ActivitySummary, client) -> None:
        super().__init__()
        self.activity = activity
        self.client = client

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(display.activity_summary_text(self.activity, []), id="sets_activity")
        yield Static(f"Strength sets — {self.activity.name}", id="sets_status")
        yield Static("", id="sets_summary")
        yield DataTable(id="sets_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.SET_COLUMNS)
        table.focus()
        cached = storage.load_exercise_sets(self.activity.activity_id)
        if cached is not None:
            self._render_sets(cached)
        elif self.client is None:
            self.query_one("#sets_status", Static).update("Not logged in — sets aren't downloaded for this activity")
        else:
            self.query_one("#sets_status", Static).update("Fetching sets from Garmin Connect...")
            self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            sets = services.exercise_sets(self.client, self.activity.activity_id)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Couldn't fetch sets: {e}", severity="error")
            return
        self.app.call_from_thread(self._render_sets, sets)

    def _render_sets(self, sets: list[dict]) -> None:
        self.query_one("#sets_status", Static).update(f"Strength sets — {self.activity.name}")
        self.query_one("#sets_summary", Static).update(display.strength_summary(sets))
        table = self.query_one(DataTable)
        table.clear()
        for row in display.strength_rows(sets):
            table.add_row(*row)

    def action_laps(self) -> None:
        _switch_screen(self, LapsScreen, lambda: LapsScreen(self.activity, self.client))

    def action_zones(self) -> None:
        self.app.push_screen(ZonesScreen(self.activity, self.client))

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        _copy_to_clipboard(self, _table_as_tsv(table), f"{table.row_count} sets")

    def action_back(self) -> None:
        self.app.pop_screen()


class PhaseMetricsScreen(Screen):
    """Histograms of duration/pace/power across a set of splits — interval phase-splits
    ("2.1", "2.2", ...), or a whole-activity split by distance/time when no intervals are
    marked: how many splits fell in each time/pace/power bucket. Answers "how consistent are
    my 300m reps" (a tight single peak) or "am I fading" (a spread with a slow tail) better
    than a table of numbers or a per-split bar chart, where every split gets its own bar."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("y", "copy", "Copy data"),
    ]

    def __init__(self, activity: ActivitySummary, phases: list[Lap]) -> None:
        super().__init__()
        self.activity = activity
        self.phases = phases
        # chart title -> [(bin label, count)], as last drawn — for copying to the clipboard
        self.histogram_data: dict[str, list[display.HistogramBar]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Phase time/pace/power — {self.activity.name}", id="phase_metrics_status")
        yield Vertical(
            PlotextPlot(id="phase_time_plot"),
            PlotextPlot(id="phase_pace_plot"),
            PlotextPlot(id="phase_power_plot"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        self.histogram_data = dict(display.phase_histograms(self.phases, self.activity.sport))
        for widget_id, (title, rows) in zip(
            ("phase_time_plot", "phase_pace_plot", "phase_power_plot"), self.histogram_data.items()
        ):
            plot = self.query_one(f"#{widget_id}", PlotextPlot)
            plt = plot.plt
            plt.clear_data()
            if rows:
                counts = [bar.count for bar in rows]
                # Outlier edge bars ("<x" / "≥y") in grey, the regular ones in the theme colour.
                plt.bar([bar.label for bar in rows], [0 if bar.outlier else bar.count for bar in rows], color=_rgb("accent"))
                if any(bar.outlier for bar in rows):
                    plt.bar([bar.label for bar in rows], [bar.count if bar.outlier else 0 for bar in rows], color=_rgb("comment"))
                # A phase count is always a whole number — without this plotext picks
                # fractional y-axis ticks (e.g. 0.00, 0.67, 1.33) to fill the plot height.
                top = max(counts)
                plt.yticks(range(0, top + 1, display.count_tick_step(top)))
            plt.title(title if rows else f"{title} — no data")
            plt.ylabel("Phases")
            plot.refresh()

    def action_copy(self) -> None:
        _copy_to_clipboard(self, display.histograms_tsv(self.histogram_data), f"{len(self.histogram_data)} histograms")

    def action_back(self) -> None:
        self.app.pop_screen()


class RoutesScreen(Screen):
    CSS = """
    #rename_input {
        width: 1fr;
    }

    #routes_body {
        height: 1fr;
    }

    #routes_table {
        width: 3fr;
    }

    #route_plot {
        width: 2fr;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("e", "rename", "Rename route"),
        Binding("b", "leaderboard", "Leaderboard"),
    ]


    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.all_routes: list[Route] = []
        self.routes_by_key: dict[str, Route] = {}
        self.discipline_filter = display.ALL_ROUTES
        self.region_filter = display.ALL_ROUTES

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Routes", id="routes_status")
        yield HorizontalGroup(
            Select([("All disciplines", display.ALL_ROUTES)], value=display.ALL_ROUTES, allow_blank=False, id="discipline_select"),
            Select([("All regions", display.ALL_ROUTES)], value=display.ALL_ROUTES, allow_blank=False, id="region_select"),
            Input(placeholder="New name", id="rename_input"),
            Button("Rename", id="rename_button"),
            id="rename_bar",
        )
        yield Horizontal(DataTable(id="routes_table"), PlotextPlot(id="route_plot"), id="routes_body")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.ROUTE_COLUMNS)
        self._refresh_table()
        # Without this, Textual auto-focuses the rename Input, and the "e" binding
        # silently types into it instead of firing.
        table.focus()

    def _refresh_table(self) -> None:
        self.all_routes = storage.load_routes()
        self.listed = services.listed_routes()
        disciplines, regions = display.route_filter_options(self.listed)
        for select_id, options, attr in (("#discipline_select", disciplines, "discipline_filter"), ("#region_select", regions, "region_filter")):
            if getattr(self, attr) not in [v for v, _ in options]:
                setattr(self, attr, display.ALL_ROUTES)
            select = self.query_one(select_id, Select)
            with select.prevent(Select.Changed):
                select.set_options([(text, value) for value, text in options])
                select.value = getattr(self, attr)
        self._render_routes()

    def _render_routes(self) -> None:
        shown = display.filter_routes(self.listed, self.discipline_filter, self.region_filter)
        # Rename / leaderboard / map look routes up here; keep the same Route objects.
        by_id = {r.route_id: r for r in self.all_routes}
        self.routes_by_key = {route.route_id: by_id.get(route.route_id, route) for route, _, _ in shown}
        table = self.query_one(DataTable)
        table.clear()
        for route, count, region in shown:
            table.add_row(*display.route_row(route, count, region), key=route.route_id)
        self.query_one("#routes_status", Static).update(f"{len(shown)} of {len(self.listed)} routes with 2+ activities")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "discipline_select":
            self.discipline_filter = event.value
        elif event.select.id == "region_select":
            self.region_filter = event.value
        else:
            return
        self._render_routes()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        route = self.routes_by_key.get(event.row_key.value) if event.row_key else None
        plot = self.query_one("#route_plot", PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.clear_figure()
        if route is not None and route.signature.points:
            lats = [lat for lat, _ in route.signature.points]
            # Scale longitude by cos(latitude) so the outline isn't stretched east-west.
            scale = math.cos(math.radians(sum(lats) / len(lats)))
            xs = [lon * scale for _, lon in route.signature.points]
            plt.plot(xs, lats, marker="braille", color="cyan")
            plt.scatter([xs[0]], [lats[0]], marker="●", color="green")
            plt.scatter([xs[-1]], [lats[-1]], marker="●", color="red")
            plt.xticks([])
            plt.yticks([])
            plt.title(f"{route.name} — outline (start green, finish red)")
        plot.refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rename_button":
            self.action_rename()

    def action_rename(self) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= table.row_count:
            return
        route_id = table.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value
        route = self.routes_by_key.get(route_id)
        if route is None:
            return
        new_name = self.query_one("#rename_input", Input).value.strip()
        if not new_name:
            self.notify("Enter a new name", severity="warning")
            return
        route.name = new_name
        storage.save_routes(self.all_routes)
        self.query_one("#rename_input", Input).value = ""
        self._refresh_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        route = self.routes_by_key.get(event.row_key.value)
        if route is not None:
            self.app.push_screen(RouteActivitiesScreen(route, self.client))

    def action_leaderboard(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        route = self.routes_by_key.get(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        if route is not None:
            self.app.push_screen(LeaderboardScreen(route, self.client))

    def action_back(self) -> None:
        self.app.pop_screen()


class LeaderboardScreen(Screen):
    """A route's personal leaderboard (leaderboard.route_leaderboard): the athlete's loops —
    or whole passes — of it, fastest first; `highlight` (an activity id) shows bold."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("w", "toggle_kind", "Loops / whole route"),
        Binding("o", "toggle_best", "Best per ride / all"),
        Binding("y", "copy", "Copy table"),
    ]

    def __init__(self, route: Route, client, highlight: str | None = None) -> None:
        super().__init__()
        self.route = route
        self.client = client
        self.highlight = highlight
        self.best_only = False
        self.kind = "loops"
        self.board: leaderboard.Board | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"{self.route.name} — finding loops...", id="leaderboard_status")
        yield DataTable(id="leaderboard_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.LEADERBOARD_COLUMNS)
        table.focus()
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        status = self.query_one("#leaderboard_status", Static)
        board = leaderboard.route_leaderboard(
            self.route.route_id, progress=lambda m: self.app.call_from_thread(status.update, m),
        )
        self.app.call_from_thread(self._loaded, board)

    def _loaded(self, board: leaderboard.Board) -> None:
        self.board = board
        self.kind = "loops" if board.loops else "routes"
        self._render_board()

    def _shown(self) -> list:
        if self.board is None:
            return []
        if self.kind == "routes":
            return self.board.routes
        return leaderboard.best_per_activity(self.board.loops) if self.best_only else self.board.loops

    def _render_board(self) -> None:
        shown = self._shown()
        b = self.board
        other = len(b.routes if self.kind == "loops" else b.loops) if b else 0
        length = (b.loop_m if self.kind == "loops" else b.route_m) if b else None
        self.query_one("#leaderboard_status", Static).update(
            display.leaderboard_status(self.route.name, self.kind, length, shown, self.best_only, other)
        )
        table = self.query_one(DataTable)
        table.clear()
        for effort, row in zip(shown, display.leaderboard_rows(shown, self.route.sport)):
            style = f"bold {COLORS['yellow']}" if effort.activity_id == self.highlight else ""
            table.add_row(*(Text(v, style=style) for v in row))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        shown = self._shown()
        if 0 <= event.cursor_row < len(shown):
            _, activities = storage.load_index()
            activity = next((a for a in activities if a.activity_id == shown[event.cursor_row].activity_id), None)
            if activity is not None:
                self.app.push_screen(activity_screen(activity, self.client))

    def action_toggle_best(self) -> None:
        self.best_only = not self.best_only
        self._render_board()

    def action_toggle_kind(self) -> None:
        self.kind = "routes" if self.kind == "loops" else "loops"
        self._render_board()

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        _copy_to_clipboard(self, _table_as_tsv(table), f"{table.row_count} efforts")

    def action_back(self) -> None:
        self.app.pop_screen()


class RouteActivitiesScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back", priority=True)]

    def __init__(self, route: Route, client) -> None:
        super().__init__()
        self.route = route
        self.client = client
        self.activities_by_id: dict[str, ActivitySummary] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Route: {self.route.name}", id="route_status")
        yield DataTable(id="route_activities_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.ACTIVITY_COLUMNS)
        _, activities = storage.load_index()
        matching = [a for a in activities if a.route_id == self.route.route_id]
        self.activities_by_id = {a.activity_id: a for a in matching}
        for activity in sorted(matching, key=lambda a: a.start_time_local, reverse=True):
            table.add_row(*_activity_cells(activity, self.route.name), key=activity.activity_id)
        self.query_one("#route_status", Static).update(
            f"Route: {self.route.name}  |  {len(matching)} activities"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        activity = self.activities_by_id.get(event.row_key.value)
        if activity is not None:
            self.app.push_screen(activity_screen(activity, self.client))

    def action_back(self) -> None:
        self.app.pop_screen()


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no dialog; dismisses with True only on an explicit confirm."""

    CSS = """
    ConfirmScreen {
        align: center middle;
    }

    #confirm_box {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #confirm_buttons {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, prompt_text: str, confirm_label: str) -> None:
        super().__init__()
        self.prompt_text = prompt_text
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.prompt_text),
            HorizontalGroup(
                Button(f"{self.confirm_label} (y)", id="confirm_yes", variant="primary"),
                Button("Cancel (n)", id="confirm_no"),
                id="confirm_buttons",
            ),
            id="confirm_box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm_yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SettingsScreen(Screen[bool]):
    """The settings in settings.FIELDS as a form; dismisses with True when saved."""

    CSS = """
    #settings_body {
        padding: 1 2;
        height: 1fr;
    }

    .setting_label {
        text-style: bold;
        margin-top: 1;
    }

    .setting_help {
        color: $text-muted;
    }

    #sports_row {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def compose(self) -> ComposeResult:
        values = settings.load()
        yield Header()
        widgets = []
        for field in settings.FIELDS:
            if field.kind == "bool":
                widgets.append(Checkbox(field.label, values[field.key], id=f"setting_{field.key}"))
            elif field.kind == "choice":
                widgets.append(Static(field.label, classes="setting_label"))
                widgets.append(Select([(label, value) for value, label in field.options], value=values[field.key],
                                      allow_blank=False, id=f"setting_{field.key}"))
            else:
                widgets.append(Static(field.label, classes="setting_label"))
                widgets.append(HorizontalGroup(*(
                    Checkbox(sport, sport in values[field.key], id=f"setting_{field.key}__{sport}")
                    for sport in settings.SPORT_GROUPS
                ), id="sports_row"))
            widgets.append(Static(field.help, classes="setting_help"))
        widgets.append(HorizontalGroup(
            Button("Save (ctrl+s)", id="settings_save", variant="primary"),
            Button("Cancel (esc)", id="settings_cancel"),
        ))
        yield Vertical(*widgets, id="settings_body")
        yield Footer()

    def _values(self) -> dict:
        values = {}
        for field in settings.FIELDS:
            if field.kind == "bool":
                values[field.key] = self.query_one(f"#setting_{field.key}", Checkbox).value
            elif field.kind == "choice":
                values[field.key] = self.query_one(f"#setting_{field.key}", Select).value
            else:
                values[field.key] = [
                    sport for sport in settings.SPORT_GROUPS
                    if self.query_one(f"#setting_{field.key}__{sport}", Checkbox).value
                ]
        return values

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings_save":
            self.action_save()
        elif event.button.id == "settings_cancel":
            self.action_back()

    def action_save(self) -> None:
        settings.save(self._values())
        self.notify(f"Settings saved to {storage.SETTINGS_PATH}")
        self.dismiss(True)

    def action_back(self) -> None:
        self.dismiss(False)


class ChoiceScreen(ModalScreen[int | None]):
    """Pick one of several options; dismisses with the chosen index, or None on cancel."""

    CSS = """
    ChoiceScreen {
        align: center middle;
    }

    #choice_box {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #choice_list {
        height: auto;
        max-height: 20;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, prompt_text: str, options: list[str]) -> None:
        super().__init__()
        self.prompt_text = prompt_text
        self.options = options

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.prompt_text),
            OptionList(*self.options, id="choice_list"),
            Static("enter choose · esc cancel", classes="hint"),
            id="choice_box",
        )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WorkoutsScreen(ContextMenuMixin, Screen):
    """Workout JSON files and week-plan zips in storage.WORKOUTS_DIR — a Textual shell around
    workout_library.WorkoutLibrary (shared with the Qt GUI): validate, preview, upload one
    workout with u or a whole week with U (after confirmation)."""

    MENU_ACTIONS = {"upload": "upload", "upload_week": "upload_week", "toggle_week": "toggle_week", "refresh": "refresh"}

    CSS = """
    #workouts_table {
        height: 1fr;
    }

    #workout_preview {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("u", "upload", "Upload workout"),
        Binding("U", "upload_week", "Upload week"),
        Binding("x", "toggle_week", "Expand/collapse week"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.library = WorkoutLibrary()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Workouts in {storage.WORKOUTS_DIR}", id="workouts_status")
        yield DataTable(id="workouts_table")
        yield Static("", id="workout_preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*workout_library.COLUMNS)
        self.action_refresh()
        table.focus()

    def action_refresh(self) -> None:
        for message in self.library.refresh():
            self.notify(message, severity="information" if message.startswith("Unpacked") else "error")
        self._fill_table()

    def _fill_table(self, select_key: str | None = None) -> None:
        table = self.query_one(DataTable)
        previous_key = select_key or self._selected_key()
        table.clear()
        for row in self.library.rows:
            if row.kind == "week":
                cells = [Text(v, style="bold") for v in row.values]
                if row.values[4] != "OK":
                    cells[4] = Text(row.values[4], style="bold red")
            elif row.kind == "invalid":
                cells = list(row.values)
                cells[4] = Text(row.values[4], style="red")
            else:
                cells = list(row.values)
            table.add_row(*cells, key=row.key)
        self.query_one("#workouts_status", Static).update(self.library.status_text)
        if previous_key in table.rows:
            table.move_cursor(row=table.get_row_index(previous_key))
        self._show_preview(self._selected_key())

    def _selected_key(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_preview(event.row_key.value if event.row_key else None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter / click on a week header expands or collapses it.
        key = event.row_key.value if event.row_key else None
        if key and key.startswith(workout_library.WEEK_KEY_PREFIX):
            self._toggle(key)

    def context_menu(self) -> list[display.MenuItem]:
        key = self._selected_key()
        return display.workout_menu(key, self.library.week_of(key) in self.library.week_dirs)

    def action_toggle_week(self) -> None:
        self._toggle(self._selected_key())

    def _toggle(self, key: str | None) -> None:
        if not self.library.toggle_week(key):
            self.notify("Not part of a week plan", severity="warning")
            return
        # Keep the cursor on the week's header (its workout rows may have just disappeared).
        self._fill_table(select_key=f"{workout_library.WEEK_KEY_PREFIX}{self.library.week_of(key)}")

    def _show_preview(self, key: str | None) -> None:
        text, is_error = self.library.preview(key)
        self.query_one("#workout_preview", Static).update(Text(text, style="red") if is_error else text)

    def action_upload(self) -> None:
        try:
            items, warning = self.library.single_upload(self._selected_key())
        except LibraryError as e:
            self.notify(str(e), severity=e.severity)
            return
        if warning:
            self.notify(warning, severity="warning")
        self._upload_many(items)

    def action_upload_week(self) -> None:
        try:
            items, prompt = self.library.week_upload(self._selected_key())
        except LibraryError as e:
            self.notify(str(e), severity=e.severity)
            return
        self.app.push_screen(
            ConfirmScreen(prompt, "Upload"),
            lambda confirmed: self._upload_many(items) if confirmed else None,
        )

    @work(thread=True, exclusive=True, group="workout_upload")
    def _upload_many(self, items: list[tuple[str, dict]]) -> None:
        status = self.query_one("#workouts_status", Static)
        notes = workout_library.upload_many(
            self.client, items, progress=lambda m: self.app.call_from_thread(status.update, m)
        )
        for severity, message in notes:
            self.app.call_from_thread(self.notify, message, severity=severity, timeout=15)
        self.app.call_from_thread(self.action_refresh)

    def action_back(self) -> None:
        self.app.pop_screen()


class TrainingWeekScreen(ContextMenuMixin, Screen):
    """A week of training from the Garmin calendar: planned workouts and completed activities
    with their (estimated) TSS, and fitness/fatigue/form (CTL/ATL/TSB) — see training_load.
    Shows cached data at once, then refreshes from Garmin in the background."""

    MENU_ACTIONS = {
        "open": "open_activity", "move": "move_workout", "cancel": "cancel_workout", "link": "link",
        "export": "export", "copy": "copy",
    }

    CSS = """
    #week_summary {
        padding: 0 1;
        border: round $primary;
        height: auto;
    }

    #week_table {
        height: 1fr;
    }

    #pmc_plot {
        height: 16;
    }

    #week_notes {
        color: $warning;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("left_square_bracket", "prev_week", "Prev week"),
        Binding("right_square_bracket", "next_week", "Next week"),
        Binding("t", "this_week", "This week"),
        Binding("r", "refresh", "Refresh from Garmin"),
        Binding("m", "move_workout", "Move workout"),
        Binding("x", "cancel_workout", "Cancel workout"),
        Binding("l", "link", "Link/unlink plan"),
        Binding("e", "export", "Export weeks"),
        Binding("y", "copy", "Copy table"),
    ]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.week = training_load.week_start(date.today())
        self.view: services.TrainingWeekView | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="week_title")
        yield Static("", id="week_summary")
        yield DataTable(id="week_table")
        yield Static("", id="week_notes")
        yield PlotextPlot(id="pmc_plot")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*display.WEEK_COLUMNS)
        table.focus()
        self._load_view()
        self.action_refresh()

    def _load_view(self, keep_row: bool = False) -> None:
        self.view = services.load_training_week(self.week)
        self._render_view(keep_row)

    def _render_view(self, keep_row: bool = False) -> None:
        view = self.view
        today = date.today()
        self.query_one("#week_title", Static).update(Text(
            f"{display.week_title(self.week)}    [ previous   ] next   t this week   r refresh", style="bold"
        ))
        self.query_one("#week_summary", Static).update(display.week_summary_text(view))
        table = self.query_one(DataTable)
        cursor_row = table.cursor_row
        table.clear()
        for item in view.visible_items:
            cells, color = display.week_item_cells(item, today)
            style = f"{color} bold".strip() if item.day == today else color
            table.add_row(*(Text(str(c), style=style) for c in cells))
        if not view.visible_items:
            table.add_row("", "", "", "", "Nothing planned or done this week", "", "", "", "")
        if keep_row and cursor_row:  # after changing a row, stay on it (or near it)
            table.move_cursor(row=min(cursor_row, table.row_count - 1))
        notes = list(view.notes)
        if not view.calendar_cached:
            notes.insert(0, "This week's calendar isn't downloaded yet — refreshing...")
        self.query_one("#week_notes", Static).update("\n".join(notes))
        self._draw_pmc()

    def _draw_pmc(self) -> None:
        plot = self.query_one("#pmc_plot", PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.clear_figure()
        points = self.view.pmc
        if points:
            xs = list(range(len(points)))
            plt.plot(xs, [p.ctl for p in points], label="CTL fitness", color="blue")
            plt.plot(xs, [p.atl for p in points], label="ATL fatigue", color="magenta")
            plt.plot(xs, [p.tsb for p in points], label="TSB form", color="yellow")
            step = max(1, len(points) // 8)
            plt.xticks(xs[::step], [p.day.strftime("%d.%m") for p in points[::step]])
            today_index = next((i for i, p in enumerate(points) if p.day == date.today()), None)
            if today_index is not None:
                plt.vline(today_index, "gray")
            week_index = next((i for i, p in enumerate(points) if p.day == self.week), None)
            if week_index is not None and week_index != today_index:
                plt.vline(week_index, "cyan")
        plt.title("Fitness (CTL) / fatigue (ATL) / form (TSB) — gray line: today; after it: planned")
        plot.refresh()

    def _change_week(self, week: date) -> None:
        self.week = week
        self._load_view()
        if not self.view.calendar_cached:
            self.action_refresh()

    def action_prev_week(self) -> None:
        self._change_week(self.week - timedelta(days=7))

    def action_next_week(self) -> None:
        self._change_week(self.week + timedelta(days=7))

    def action_this_week(self) -> None:
        self._change_week(training_load.week_start(date.today()))

    def action_refresh(self) -> None:
        if self.client is None:
            self.notify("Not logged in to Garmin — showing cached data", severity="warning")
            return
        self._refresh_from_garmin(self.week)

    @work(thread=True, exclusive=True, group="training_refresh")
    def _refresh_from_garmin(self, week: date) -> None:
        title = self.query_one("#week_title", Static)
        try:
            warnings = services.refresh_training_data(
                self.client, week, progress=lambda m: self.app.call_from_thread(title.update, Text(m, style="italic"))
            )
        except Exception as e:  # network/auth trouble: keep showing the cached view
            self.app.call_from_thread(self.notify, f"Refresh failed: {e}", severity="error")
            warnings = []
        for message in warnings:
            self.app.call_from_thread(self.notify, message, severity="warning")
        self.app.call_from_thread(self._load_view)

    def _selected_item(self) -> training_load.WeekItem | None:
        row = self.query_one(DataTable).cursor_row
        items = self.view.visible_items if self.view else []
        return items[row] if row is not None and 0 <= row < len(items) else None

    def context_menu(self) -> list[display.MenuItem]:
        return display.week_menu(self._selected_item())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_activity()

    def action_open_activity(self) -> None:
        item = self._selected_item()
        _, activities = storage.load_index()
        activity = next((a for a in activities if item and a.activity_id == item.activity_id), None)
        if activity is not None:
            self.app.push_screen(activity_screen(activity, self.client))

    def action_move_workout(self) -> None:
        item = self._selected_item()
        try:
            days = services.move_targets(item)
        except services.CalendarError as e:
            self.notify(str(e), severity="warning")
            return
        if self.client is None:
            self.notify("Not logged in to Garmin", severity="warning")
            return

        def chosen(index: int | None) -> None:
            if index is not None:
                self._garmin_change(lambda: services.move_planned(self.client, item, days[index]))

        options = [display.week_day_choice(day, self.view.week.items) for day in days]
        self.app.push_screen(ChoiceScreen(services.move_prompt(item), options), chosen)

    def action_cancel_workout(self) -> None:
        item = self._selected_item()
        try:
            prompt = services.cancel_prompt(item)
        except services.CalendarError as e:
            self.notify(str(e), severity="warning")
            return
        if self.client is None:
            self.notify("Not logged in to Garmin", severity="warning")
            return

        def confirmed(yes: bool) -> None:
            if yes:
                self._garmin_change(lambda: services.cancel_planned(self.client, item))

        self.app.push_screen(ConfirmScreen(prompt, "Remove from calendar"), confirmed)

    @work(thread=True, group="calendar_change")
    def _garmin_change(self, operation) -> None:
        try:
            self.app.call_from_thread(self.notify, operation())
        except services.CalendarError as e:
            self.app.call_from_thread(self.notify, str(e), severity="warning")
        except Exception as e:  # network/auth trouble: Garmin is unchanged or partly changed
            self.app.call_from_thread(self.notify, f"Garmin change failed: {e}", severity="error")
        self.app.call_from_thread(self._load_view, True)

    def _local_change(self, operation) -> None:
        try:
            self.notify(operation())
        except services.CalendarError as e:
            self.notify(str(e), severity="warning")
        self._load_view(keep_row=True)

    def action_link(self) -> None:
        item = self._selected_item()
        if item is not None and item.status == "done (planned)":
            try:
                prompt = services.unlink_prompt(item)
            except services.CalendarError as e:
                self.notify(str(e), severity="warning")
                return
            self.app.push_screen(
                ConfirmScreen(prompt, "Unlink"),
                lambda yes: self._local_change(lambda: services.unlink(item)) if yes else None,
            )
            return
        try:
            candidates = services.link_candidates(self.view.week, item)
        except services.CalendarError as e:
            self.notify(str(e), severity="warning")
            return

        def chosen(index: int | None) -> None:
            if index is not None:
                self._local_change(lambda: services.link(item, candidates[index]))

        options = [display.week_item_choice(c) for c in candidates]
        self.app.push_screen(ChoiceScreen(services.link_prompt(item), options), chosen)

    def action_export(self) -> None:
        def chosen(index: int | None) -> None:
            if index is not None:
                self._export(training_export.CHOICES[index])

        labels = training_export.choice_labels(date.today())
        self.app.push_screen(ChoiceScreen(training_export.EXPORT_PROMPT, labels), chosen)

    @work(thread=True, group="calendar_change")
    def _export(self, choice: int) -> None:
        title = self.query_one("#week_title", Static)
        try:
            path, warnings = services.export_training(
                self.client, choice, progress=lambda m: self.app.call_from_thread(title.update, Text(m, style="italic")),
            )
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Export failed: {e}", severity="error")
            self.app.call_from_thread(self._load_view, True)
            return
        for message in warnings:
            self.app.call_from_thread(self.notify, message, severity="warning")
        self.app.call_from_thread(self.notify, f"Exported to {path}", timeout=15)
        self.app.call_from_thread(self._load_view, True)

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        text = display.week_summary_text(self.view) + "\n\n" + _table_as_tsv(table)
        _copy_to_clipboard(self, text, f"week {self.week:%d.%m}")

    def action_back(self) -> None:
        self.app.pop_screen()
