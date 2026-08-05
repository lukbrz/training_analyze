from datetime import date, timedelta

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalGroup, Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static
from textual_plotext import PlotextPlot

import fit_parser
import garmin_client
import storage
from models import (
    ActivitySummary,
    Lap,
    ZoneTime,
    format_distance,
    format_duration,
    format_hr,
    format_lap_distance,
    format_power,
    format_speed_or_pace,
)

INITIAL_SYNC_DAYS = 365


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


class MainScreen(Screen):
    BINDINGS = [Binding("s", "sync", "Sync"), Binding("r", "reprocess", "Reprocess")]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.activities_by_id: dict[str, ActivitySummary] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield HorizontalGroup(
            Button("Sync (s)", id="sync_button", variant="primary"),
            Button("Reprocess (r)", id="reprocess_button"),
        )
        yield DataTable(id="activities_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Date", "Activity", "Sport", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power"
        )
        self._refresh_table()

    def _refresh_table(self) -> None:
        _, activities = storage.load_index()
        self.activities_by_id = {a.activity_id: a for a in activities}
        table = self.query_one(DataTable)
        table.clear()
        for activity in sorted(activities, key=lambda a: a.start_time_local, reverse=True):
            table.add_row(
                activity.start_time_local.replace("T", " ")[:16],
                activity.name,
                activity.sport,
                Text(format_duration(activity.duration_s), justify="right"),
                format_distance(activity.distance_m),
                format_speed_or_pace(activity.sport, activity.avg_speed_mps),
                format_hr(activity.avg_hr),
                format_power(activity.avg_power),
                key=activity.activity_id,
            )
        self.query_one("#status", Static).update(f"{len(activities)} activities synced")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync_button":
            self.action_sync()
        elif event.button.id == "reprocess_button":
            self.action_reprocess()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        activity = self.activities_by_id.get(event.row_key.value)
        if activity is not None:
            self.app.push_screen(LapsScreen(activity, self.client))

    def action_sync(self) -> None:
        self._set_buttons_disabled(True)
        self._sync()

    def action_reprocess(self) -> None:
        self._set_buttons_disabled(True)
        self._reprocess()

    def _set_buttons_disabled(self, disabled: bool) -> None:
        self.query_one("#sync_button", Button).disabled = disabled
        self.query_one("#reprocess_button", Button).disabled = disabled

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    @work(thread=True)
    def _sync(self) -> None:
        last_synced_date, activities = storage.load_index()
        known_ids = {a.activity_id for a in activities}
        start_date = last_synced_date or (date.today() - timedelta(days=INITIAL_SYNC_DAYS)).isoformat()
        end_date = date.today().isoformat()

        self.app.call_from_thread(self._set_status, f"Fetching activity list since {start_date}...")
        try:
            raw_activities = garmin_client.list_activities(self.client, start_date, end_date)
        except garmin_client.GarminConnectTooManyRequestsError:
            self.app.call_from_thread(self._sync_done, "Rate limited by Garmin — try syncing again later.")
            return
        except (garmin_client.GarminConnectAuthenticationError, garmin_client.GarminConnectConnectionError) as e:
            self.app.call_from_thread(self._sync_done, f"Sync failed: {e}")
            return

        new_count = 0
        skipped = 0
        for raw in raw_activities:
            activity_id = str(raw.get("activityId"))
            if activity_id in known_ids:
                continue

            name = raw.get("activityName") or "Untitled"
            start_time_local = raw.get("startTimeLocal") or ""
            self.app.call_from_thread(
                self._set_status, f"Downloading {name} ({new_count + skipped + 1}/{len(raw_activities)})..."
            )

            try:
                fit_bytes = garmin_client.download_fit(self.client, activity_id)
                summary, laps = fit_parser.parse(fit_bytes, activity_id, name, start_time_local)
            except garmin_client.GarminConnectTooManyRequestsError:
                self.app.call_from_thread(
                    self._sync_done, f"Rate limited after {new_count} new activities — try again later."
                )
                return
            except (
                garmin_client.GarminConnectNotFoundError,
                garmin_client.GarminConnectInvalidFileFormatError,
                ValueError,
            ):
                skipped += 1
                known_ids.add(activity_id)  # avoid retrying a permanently-missing FIT within this run
                continue

            storage.save_fit(activity_id, fit_bytes)
            storage.save_laps(activity_id, laps)
            activities.append(summary)
            known_ids.add(activity_id)
            new_count += 1

            activity_date = summary.start_time_local[:10]
            if not last_synced_date or activity_date > last_synced_date:
                last_synced_date = activity_date
            storage.save_index(last_synced_date, activities)

        message = f"Sync complete: {new_count} new activities"
        if skipped:
            message += f", {skipped} skipped (no usable FIT file)"
        self.app.call_from_thread(self._sync_done, message)

    def _sync_done(self, message: str) -> None:
        self._set_status(message)
        self._set_buttons_disabled(False)
        self._refresh_table()
        self.notify(message)

    @work(thread=True)
    def _reprocess(self) -> None:
        last_synced_date, activities = storage.load_index()
        by_id = {a.activity_id: a for a in activities}

        reprocessed = 0
        skipped = 0
        for i, activity in enumerate(activities, start=1):
            self.app.call_from_thread(
                self._set_status, f"Reprocessing {activity.name} ({i}/{len(activities)})..."
            )
            if not storage.has_fit(activity.activity_id):
                skipped += 1
                continue

            fit_bytes = storage.fit_path(activity.activity_id).read_bytes()
            try:
                new_summary, new_laps = fit_parser.parse(
                    fit_bytes, activity.activity_id, activity.name, activity.start_time_local
                )
            except ValueError:
                skipped += 1
                continue

            old_marks = {lap.lap_index: lap.is_interval for lap in storage.load_laps(activity.activity_id)}
            for lap in new_laps:
                if old_marks.get(lap.lap_index):
                    lap.is_interval = True

            by_id[activity.activity_id] = new_summary
            storage.save_laps(activity.activity_id, new_laps)
            storage.save_index(last_synced_date, list(by_id.values()))
            reprocessed += 1

        message = f"Reprocessed {reprocessed} activities"
        if skipped:
            message += f", {skipped} skipped (no local FIT file)"
        self.app.call_from_thread(self._sync_done, message)


class LapsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("m", "toggle_interval", "Mark interval"),
        Binding("z", "zones", "Zones"),
        Binding("c", "compare", "Compare intervals"),
    ]

    LAP_COLUMNS = ("#", "Start", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Int")
    INT_COLUMN_INDEX = len(LAP_COLUMNS) - 1

    def __init__(self, activity: ActivitySummary, client) -> None:
        super().__init__()
        self.activity = activity
        self.client = client
        self.current_laps: list[Lap] = []
        self.viewing_device_laps = True
        self._records: list[fit_parser.RecordPoint] | None = None
        self._offset: timedelta | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._summary_text(), id="activity_summary")
        yield HorizontalGroup(
            Input(placeholder="e.g. 400 or 60", id="split_value_input"),
            Button("Split by distance (m)", id="split_distance_button"),
            Button("Split by time (s)", id="split_time_button"),
            Button("Reset", id="reset_button"),
            id="split_bar",
        )
        yield DataTable(id="laps_table")
        yield Static("", id="interval_summary")
        yield Footer()

    def _summary_text(self) -> str:
        a = self.activity
        return (
            f"{a.name}  |  {a.start_time_local}  |  {a.sport}\n"
            f"Duration: {format_duration(a.duration_s)}   "
            f"Distance: {format_distance(a.distance_m)}   "
            f"Avg: {format_speed_or_pace(a.sport, a.avg_speed_mps)}   "
            f"Avg HR: {format_hr(a.avg_hr)}   Avg Power: {format_power(a.avg_power)}"
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*self.LAP_COLUMNS)
        self.current_laps = storage.load_laps(self.activity.activity_id)
        self.viewing_device_laps = True
        self._rebuild_table()
        self._refresh_interval_summary()

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for lap in self.current_laps:
            table.add_row(
                str(lap.lap_index),
                lap.start_time_local[11:19] if lap.start_time_local else "–",
                Text(format_duration(lap.duration_s), justify="right"),
                format_lap_distance(self.activity.sport, lap.distance_m),
                format_speed_or_pace(self.activity.sport, lap.avg_speed_mps),
                format_hr(lap.avg_hr),
                format_power(lap.avg_power),
                "✓" if lap.is_interval else "",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "split_distance_button":
            self._do_split("distance")
        elif event.button.id == "split_time_button":
            self._do_split("time")
        elif event.button.id == "reset_button":
            self._do_reset()

    def _ensure_records_loaded(self) -> bool:
        if self._records is not None:
            return True
        path = storage.fit_path(self.activity.activity_id)
        if not path.exists():
            self.notify("FIT file not found for this activity", severity="error")
            return False
        fit_bytes = path.read_bytes()
        self._records = fit_parser.parse_records(fit_bytes)
        self._offset = fit_parser.session_utc_offset(fit_bytes, self.activity.start_time_local)
        return True

    def _do_split(self, mode: str) -> None:
        raw_value = self.query_one("#split_value_input", Input).value.strip()
        try:
            value = float(raw_value)
            if value <= 0:
                raise ValueError
        except ValueError:
            self.notify("Enter a positive number for the split size", severity="warning")
            return

        if not self._ensure_records_loaded():
            return
        if not self._records:
            self.notify("No record data in this FIT file to split", severity="warning")
            return

        if mode == "distance":
            laps = fit_parser.split_by_distance(self._records, value, self._offset)
        else:
            laps = fit_parser.split_by_time(self._records, value, self._offset)

        self.current_laps = laps
        self.viewing_device_laps = False
        self._rebuild_table()
        self._refresh_interval_summary()

    def _do_reset(self) -> None:
        self.current_laps = storage.load_laps(self.activity.activity_id)
        self.viewing_device_laps = True
        self._rebuild_table()
        self._refresh_interval_summary()

    def action_toggle_interval(self) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.current_laps):
            return
        lap = self.current_laps[row]
        lap.is_interval = not lap.is_interval
        table.update_cell_at(Coordinate(row, self.INT_COLUMN_INDEX), "✓" if lap.is_interval else "")
        if self.viewing_device_laps:
            storage.save_laps(self.activity.activity_id, self.current_laps)
        self._refresh_interval_summary()

    def _refresh_interval_summary(self) -> None:
        marked = [lap for lap in self.current_laps if lap.is_interval]
        static = self.query_one("#interval_summary", Static)
        if not marked:
            static.update("No intervals marked (press m on a lap to mark it)")
            return

        total_distance = sum(lap.distance_m for lap in marked)
        total_duration = sum(lap.duration_s for lap in marked)
        avg_speed = total_distance / total_duration if total_duration else None

        hr_samples = [(lap.avg_hr, lap.duration_s) for lap in marked if lap.avg_hr is not None]
        avg_hr = (
            round(sum(hr * d for hr, d in hr_samples) / sum(d for _, d in hr_samples))
            if hr_samples
            else None
        )
        power_samples = [(lap.avg_power, lap.duration_s) for lap in marked if lap.avg_power is not None]
        avg_power = (
            sum(p * d for p, d in power_samples) / sum(d for _, d in power_samples)
            if power_samples
            else None
        )

        static.update(
            f"Intervals: {len(marked)}  |  Distance: {format_lap_distance(self.activity.sport, total_distance)}  |  "
            f"Duration: {format_duration(total_duration)}  |  "
            f"Avg: {format_speed_or_pace(self.activity.sport, avg_speed)}  |  "
            f"Avg HR: {format_hr(avg_hr)}  |  Avg Power: {format_power(avg_power)}"
        )

    def action_zones(self) -> None:
        self.app.push_screen(ZonesScreen(self.activity, self.client))

    def action_compare(self) -> None:
        marked = [lap for lap in self.current_laps if lap.is_interval]
        if len(marked) < 2:
            self.notify("Mark at least 2 intervals to compare", severity="warning")
            return
        if not self._ensure_records_loaded():
            return
        self.app.push_screen(CompareScreen(self.activity, marked, self._records, self._offset))

    def action_back(self) -> None:
        self.app.pop_screen()


class ZonesScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back", priority=True)]

    ZONE_COLUMNS = ("#", "Range", "Time", "%")

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
        self.query_one("#hr_zones_table", DataTable).add_columns(*self.ZONE_COLUMNS)
        self.query_one("#power_zones_table", DataTable).add_columns(*self.ZONE_COLUMNS)

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
        if not zones:
            table.add_row("–", "Couldn't parse zone data", "–", "–")
            return
        total = sum(zone.seconds for zone in zones) or 1.0
        for zone in sorted(zones, key=lambda z: z.zone_number):
            if zone.low_boundary is not None and zone.high_boundary is not None:
                zone_range = f"{zone.low_boundary:.0f}–{zone.high_boundary:.0f}"
            elif zone.low_boundary is not None:
                zone_range = f"{zone.low_boundary:.0f}+"
            else:
                zone_range = "–"
            table.add_row(
                str(zone.zone_number),
                zone_range,
                format_duration(zone.seconds),
                f"{zone.seconds / total * 100:.0f}%",
            )

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
    X_AXIS_LABELS = {
        "time": "Elapsed seconds",
        "distance": "Elapsed meters",
        "percent": "% of interval",
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
        self._draw_metric("power_plot", "power", "Power (W)")
        self._draw_metric("hr_plot", "heart_rate", "Heart Rate (bpm)")
        self._draw_metric("speed_plot", "speed_mps", "Speed (km/h)", scale=3.6)

    def _draw_metric(self, widget_id: str, attr: str, title: str, scale: float = 1.0) -> None:
        plot = self.query_one(f"#{widget_id}", PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        has_data = False
        for lap in self.marked_laps:
            lap_records = fit_parser.lap_record_slice(self.records, lap, self.utc_offset)
            x_values = fit_parser.lap_x_values(lap_records, lap, self.x_mode)
            xs, ys = fit_parser.metric_series(lap_records, x_values, attr)
            if not xs:
                continue
            if scale != 1.0:
                ys = [y * scale for y in ys]
            plt.plot(xs, ys, label=f"Lap {lap.lap_index}")
            has_data = True
        plt.title(title if has_data else f"{title} — no data")
        plt.xlabel(self.X_AXIS_LABELS[self.x_mode])
        plot.refresh()

    def action_back(self) -> None:
        self.app.pop_screen()
