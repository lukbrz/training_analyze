import math
import shutil
import subprocess
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalGroup, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static
from textual_plotext import PlotextPlot

import dedupe
import fit_parser
import garmin_client
import geocode
import routes
import storage
import workouts
from models import (
    PACE_SPORTS,
    SWIM_SPORTS,
    ActivitySummary,
    Lap,
    Route,
    ZoneTime,
    format_distance,
    format_duration,
    format_duration_tenths,
    format_elevation,
    format_hr,
    format_lap_distance,
    format_power,
    format_speed_or_pace,
    format_split_value,
    parse_split_value,
)

INITIAL_SYNC_DAYS = 365

ACTIVITY_COLUMNS = (
    "Date", "Activity", "Sport", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Route"
)

ALL_SPORTS = "__all__"


LEG_NAMES = {"swimming": "Swim", "cycling": "Bike", "running": "Run", "transition": "Trans"}


def _common_sport(laps: list[Lap], fallback: str) -> str:
    """The sport all `laps` share (multisport laps carry their own), else `fallback`."""
    sports = {lap.sport for lap in laps}
    return sports.pop() if len(sports) == 1 and None not in sports else fallback


def _legs(laps: list[Lap]) -> list[tuple[str, Lap]]:
    """A multisport activity's legs (Swim, T1, Bike, T2, Run, ...): consecutive device laps of
    the same sport aggregated into one Lap each, labelled for display."""
    groups: list[list[Lap]] = []
    for lap in laps:
        if groups and groups[-1][0].sport == lap.sport:
            groups[-1].append(lap)
        else:
            groups.append([lap])

    def weighted(attr: str, group: list[Lap]) -> float | None:
        samples = [(getattr(l, attr), l.duration_s) for l in group if getattr(l, attr) is not None]
        total = sum(d for _, d in samples)
        return sum(v * d for v, d in samples) / total if total else None

    legs = []
    transitions = 0
    for group in groups:
        sport = group[0].sport or "other"
        if sport == "transition":
            transitions += 1
            name = f"T{transitions}"
        else:
            name = LEG_NAMES.get(sport, sport.replace("_", " ").title())
        duration = sum(l.duration_s for l in group)
        distance = sum(l.distance_m for l in group)
        hr = weighted("avg_hr", group)
        ascents = [l.total_ascent_m for l in group if l.total_ascent_m is not None]
        legs.append((name, Lap(
            lap_index=group[0].lap_index,
            start_time_local=group[0].start_time_local,
            duration_s=duration,
            distance_m=distance,
            avg_speed_mps=distance / duration if duration else None,
            avg_hr=round(hr) if hr is not None else None,
            avg_power=weighted("avg_power", group),
            total_ascent_m=sum(ascents) if ascents else None,
            sport=sport,
        )))
    return legs


_CLIPBOARD_COMMANDS = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


def _cell_text(cell) -> str:
    return (cell.plain if isinstance(cell, Text) else str(cell)).strip()


def _table_as_tsv(table: DataTable) -> str:
    """The table exactly as displayed (current filter, phase-split rows and all) as
    tab-separated text, which pastes into a spreadsheet as cells."""
    lines = ["\t".join(_cell_text(col.label) for col in table.columns.values())]
    for row in range(table.row_count):
        lines.append("\t".join(_cell_text(cell) for cell in table.get_row_at(row)))
    return "\n".join(lines)


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


def _activity_row(activity: ActivitySummary, route_name: str) -> tuple:
    return (
        activity.start_time_local.replace("T", " ")[:16],
        activity.name,
        activity.sport,
        Text(format_duration(activity.duration_s), justify="right"),
        format_distance(activity.distance_m),
        format_speed_or_pace(activity.sport, activity.avg_speed_mps),
        format_hr(activity.avg_hr),
        format_power(activity.avg_power),
        route_name,
    )


def _assign_route(activity_id: str, fit_bytes: bytes, sport: str, routes_list: list[Route]) -> str | None:
    """Compute an activity's route_id, matching against `routes_list` (mutated in place —
    a newly created route is appended and persisted immediately, so a later activity in the
    same sync/reprocess run can match it). Returns None if there isn't enough GPS data."""
    records = fit_parser.load_records(activity_id, fit_bytes)
    signature = routes.build_signature(records)
    if signature is None:
        return None
    route, is_new = routes.match_or_create(signature, sport, routes_list, lambda: uuid.uuid4().hex)
    if is_new:
        lat, lon = signature.points[0]
        route.place = geocode.reverse_geocode(lat, lon)
        routes_list.append(route)
        storage.save_routes(routes_list)
    return route.route_id


def _merge_duplicates(
    activities: list[ActivitySummary], recompute_existing: bool
) -> tuple[list[ActivitySummary], int]:
    """Detect activities that are the same physical session recorded by two devices and
    fold each into its primary (see dedupe.py) — filling in HR/power/GPS coverage the
    primary's own device missed, and dropping the secondary from the visible list.

    Fully idempotent and safe to run on every sync/reprocess: a secondary's raw .fit is
    never deleted, only excluded from the index via the merge ledger, so re-running this
    just recomputes the same result from scratch rather than accumulating state.

    Rebuilding a merge means fully re-parsing both FIT files (tens of seconds for a few long
    activities), so with `recompute_existing=False` (sync) only pairs that are new or have no
    saved merged stream are rebuilt — `activities` loaded from the index already carry the
    merged summaries of the rest. Reprocess passes True, since it has just re-parsed every
    summary from scratch and the merge has to be reapplied on top.
    """
    ledger = storage.load_merge_ledger()
    visible = [a for a in activities if a.activity_id not in ledger]

    newly_merged = 0
    new_primaries: set[str] = set()
    for a, b in dedupe.find_duplicate_pairs(visible):
        primary, secondary = dedupe.choose_primary(a, b)
        if secondary.activity_id in ledger:
            continue
        ledger[secondary.activity_id] = primary.activity_id
        new_primaries.add(primary.activity_id)
        newly_merged += 1
    if newly_merged:
        storage.save_merge_ledger(ledger)

    by_id = {a.activity_id: a for a in activities}
    secondaries_by_primary: dict[str, list[str]] = {}
    for secondary_id, primary_id in ledger.items():
        secondaries_by_primary.setdefault(primary_id, []).append(secondary_id)

    for primary_id, secondary_ids in secondaries_by_primary.items():
        primary = by_id.get(primary_id)
        if primary is None:
            continue
        if not (recompute_existing or primary_id in new_primaries or not storage.has_merged_records(primary_id)):
            continue

        streams = []
        if storage.has_fit(primary_id):
            streams.append(fit_parser.parse_records(storage.fit_path(primary_id).read_bytes()))

        secondary_summaries = []
        for secondary_id in secondary_ids:
            if not storage.has_fit(secondary_id):
                continue
            secondary_fit_bytes = storage.fit_path(secondary_id).read_bytes()
            streams.append(fit_parser.parse_records(secondary_fit_bytes))
            # name/start_time_local only affect display fields we discard here — the
            # summary-level HR/power/ascent/speed fields come straight from the FIT
            # session message regardless.
            secondary_summary, _ = fit_parser.parse(
                secondary_fit_bytes, secondary_id, "", primary.start_time_local
            )
            secondary_summaries.append(secondary_summary)

        if len(streams) >= 2:
            storage.save_merged_records(primary_id, dedupe.merge_records(streams))
        if secondary_summaries:
            by_id[primary_id] = dedupe.merge_summary(primary, secondary_summaries)

    merged_activities = [a for a in by_id.values() if a.activity_id not in ledger]
    return merged_activities, newly_merged


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
    BINDINGS = [
        Binding("s", "sync", "Sync"),
        Binding("r", "reprocess", "Reprocess"),
        Binding("o", "routes", "Routes"),
        Binding("w", "workouts", "Workouts"),
        Binding("y", "copy", "Copy table"),
    ]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.activities_by_id: dict[str, ActivitySummary] = {}
        self.all_activities: list[ActivitySummary] = []
        self.routes_by_id: dict[str, Route] = {}
        self.sport_filter: str = ALL_SPORTS

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield HorizontalGroup(
            Button("Sync (s)", id="sync_button", variant="primary"),
            Button("Reprocess (r)", id="reprocess_button"),
            Button("Routes (o)", id="routes_button"),
            Button("Workouts (w)", id="workouts_button"),
            Select([("All sports", ALL_SPORTS)], value=ALL_SPORTS, id="sport_filter_select"),
        )
        yield DataTable(id="activities_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*ACTIVITY_COLUMNS)
        self._refresh_table()

    def _refresh_table(self) -> None:
        _, self.all_activities = storage.load_index()
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
                *_activity_row(activity, route.name if route else "–"), key=activity.activity_id
            )
        total = len(self.all_activities)
        shown = len(activities)
        status = f"{shown} activities synced" if shown == total else f"{shown} of {total} activities"
        self.query_one("#status", Static).update(status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync_button":
            self.action_sync()
        elif event.button.id == "reprocess_button":
            self.action_reprocess()
        elif event.button.id == "routes_button":
            self.action_routes()
        elif event.button.id == "workouts_button":
            self.action_workouts()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sport_filter_select":
            self.sport_filter = event.value
            self._render_table()

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

    def action_routes(self) -> None:
        self.app.push_screen(RoutesScreen(self.client))

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
    def _sync(self) -> None:
        last_synced_date, activities = storage.load_index()
        routes_list = storage.load_routes()
        # Also treat any activity already folded into another as "known" — otherwise a
        # future sync would re-download and re-add it, undoing a prior merge.
        known_ids = {a.activity_id for a in activities} | set(storage.load_merge_ledger())
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

            summary.route_id = _assign_route(activity_id, fit_bytes, summary.sport, routes_list)
            storage.save_fit(activity_id, fit_bytes)
            storage.save_laps(activity_id, laps)
            activities.append(summary)
            known_ids.add(activity_id)
            new_count += 1

            activity_date = summary.start_time_local[:10]
            if not last_synced_date or activity_date > last_synced_date:
                last_synced_date = activity_date
            storage.save_index(last_synced_date, activities)

        self.app.call_from_thread(self._set_status, "Checking for duplicate recordings...")
        activities, merged_count = _merge_duplicates(activities, recompute_existing=False)
        storage.save_index(last_synced_date, activities)

        message = f"Sync complete: {new_count} new activities"
        if merged_count:
            message += f", {merged_count} merged as duplicates"
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
        routes_list = storage.load_routes()
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

            old_laps = storage.load_laps(activity.activity_id)
            old_marks = {lap.lap_index: lap.is_interval for lap in old_laps}
            if len(old_laps) > 1 and all(old_marks.values()):
                # Every lap marked is the old parser stamping all 'active' laps of an
                # unstructured activity as intervals, not marks anyone made by hand.
                old_marks = {}
            for lap in new_laps:
                if old_marks.get(lap.lap_index):
                    lap.is_interval = True

            new_summary.route_id = _assign_route(
                activity.activity_id, fit_bytes, new_summary.sport, routes_list
            )
            by_id[activity.activity_id] = new_summary
            storage.save_laps(activity.activity_id, new_laps)
            storage.save_index(last_synced_date, list(by_id.values()))
            reprocessed += 1

        self.app.call_from_thread(self._set_status, "Re-merging duplicate recordings...")
        merged_activities, merged_count = _merge_duplicates(list(by_id.values()), recompute_existing=True)
        storage.save_index(last_synced_date, merged_activities)

        message = f"Reprocessed {reprocessed} activities"
        if merged_count:
            message += f", {merged_count} merged as duplicates"
        if skipped:
            message += f", {skipped} skipped (no local FIT file)"
        self.app.call_from_thread(self._sync_done, message)


class LapsScreen(Screen):
    CSS = """
    #split_value_input {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("m", "toggle_interval", "Mark interval"),
        Binding("z", "zones", "Zones"),
        Binding("c", "compare", "Compare intervals"),
        Binding("p", "physical_laps", "Physical laps"),
        Binding("i", "split_intervals", "Split intervals"),
        Binding("f", "toggle_interval_filter", "Interval-only"),
        Binding("h", "phase_metrics", "Phase time/pace/power"),
        Binding("y", "copy", "Copy table"),
    ]

    LAP_COLUMNS = (
        "#", "Start", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Ascent", "Descent", "Int"
    )
    DEFAULT_INTERVAL_SPLIT_M = 300.0

    def __init__(self, activity: ActivitySummary, client) -> None:
        super().__init__()
        self.activity = activity
        self.client = client
        self.current_laps: list[Lap] = []
        self.viewing_device_laps = True
        self._records: list[fit_parser.RecordPoint] | None = None
        self._offset: timedelta | None = None
        # lap_index -> phase splits of that (marked-interval) lap, shown as child rows
        # "2.1", "2.2", ... under their parent — recomputed whenever current_laps changes.
        self.interval_splits: dict[int, list[Lap]] = {}
        self.show_intervals_only = False
        # Row position -> index into current_laps, or None for a child phase-split row —
        # lets action_toggle_interval and similar map a DataTable cursor row back to a lap
        # even though rendered rows no longer correspond 1:1 with current_laps.
        self._row_map: list[int | None] = []
        self.is_multisport = activity.sport == fit_parser.MULTISPORT

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._summary_text(), id="activity_summary")
        yield HorizontalGroup(
            Input(placeholder="400 = metres · 60s / 1m / 1:30 = time", id="split_value_input"),
            Button("Split", id="split_button"),
            Button("Split intervals (i)", id="split_intervals_button"),
            Button("Interval only (f)", id="interval_filter_button"),
            Button("Reset", id="reset_button"),
            id="split_bar",
        )
        yield DataTable(id="laps_table")
        yield Static("", id="interval_summary")
        yield Footer()

    def _summary_text(self) -> str:
        a = self.activity
        text = (
            f"{a.name}  |  {a.start_time_local}  |  {a.sport}\n"
            f"Duration: {format_duration(a.duration_s)}   "
            f"Distance: {format_distance(a.distance_m)}   "
            f"Avg: {format_speed_or_pace(a.sport, a.avg_speed_mps)}   "
            f"Avg HR: {format_hr(a.avg_hr)}   Avg Power: {format_power(a.avg_power)}"
        )
        if self.is_multisport:
            for name, leg in _legs(storage.load_laps(a.activity_id)):
                text += (
                    f"\n  {name:<5} {format_duration(leg.duration_s):>8}  "
                    f"{format_lap_distance(leg.sport, leg.distance_m) if leg.sport in SWIM_SPORTS else format_distance(leg.distance_m):>9}  "
                    f"{format_speed_or_pace(leg.sport, leg.avg_speed_mps):>12}  "
                    f"HR {format_hr(leg.avg_hr):>3}  {format_power(leg.avg_power):>6}  "
                    f"↑ {format_elevation(leg.total_ascent_m)}"
                )
        return text

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        columns = list(self.LAP_COLUMNS)
        if self.is_multisport:
            columns.insert(1, "Sport")
        table.add_columns(*columns)
        self.current_laps = storage.load_laps(self.activity.activity_id)
        self.viewing_device_laps = True
        self._rebuild_table()
        self._refresh_interval_summary()
        # Without this, Textual auto-focuses the split-by-distance Input, and every
        # single-letter binding (m/z/c/p) silently types into it instead of firing.
        table.focus()

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._row_map = []
        for idx, lap in enumerate(self.current_laps):
            if self.show_intervals_only and not lap.is_interval:
                continue
            table.add_row(*self._lap_row(lap, str(lap.lap_index), bold=lap.is_interval))
            self._row_map.append(idx)
            for phase_i, phase_lap in enumerate(self.interval_splits.get(lap.lap_index, []), start=1):
                table.add_row(*self._lap_row(phase_lap, f"  {lap.lap_index}.{phase_i}", dim=True))
                self._row_map.append(None)

    def _lap_row(self, lap: Lap, index_label: str, bold: bool = False, dim: bool = False) -> tuple:
        # Interval laps (bold) and their phase splits (dim) get tenths-of-a-second duration
        # precision — it's the only way a rep's phase durations visibly sum back to the rep's
        # own duration (see fit_parser._interpolate_at_distance/_interpolate_at_time); a
        # plain, unmarked lap has no such reconciliation to show and keeps whole seconds.
        marked = bold or dim
        duration_text = format_duration_tenths(lap.duration_s) if marked else format_duration(lap.duration_s)
        style = "bold" if bold else ("dim" if dim else "")

        def cell(value):
            return Text(str(value), style=style) if style else value

        sport = lap.sport or self.activity.sport
        sport_cells = (cell(LEG_NAMES.get(sport, sport)),) if self.is_multisport else ()
        return (
            cell(index_label),
            *sport_cells,
            cell(lap.start_time_local[11:19] if lap.start_time_local else "–"),
            Text(duration_text, justify="right", style=style),
            cell(format_lap_distance(sport, lap.distance_m)),
            cell(format_speed_or_pace(sport, lap.avg_speed_mps)),
            cell(format_hr(lap.avg_hr)),
            cell(format_power(lap.avg_power)),
            cell(format_elevation(lap.total_ascent_m)),
            cell(format_elevation(lap.total_descent_m)),
            cell("✓" if lap.is_interval else ""),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "split_button":
            self._do_split()
        elif event.button.id == "split_intervals_button":
            self.action_split_intervals()
        elif event.button.id == "interval_filter_button":
            self.action_toggle_interval_filter()
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
        self._records = fit_parser.load_records(self.activity.activity_id, fit_bytes)
        self._offset = fit_parser.session_utc_offset(fit_bytes, self.activity.start_time_local)
        return True

    def _read_split_value(self, default: tuple[str, float] | None = None) -> tuple[str, float] | None:
        """The split input as ("distance", m) or ("time", s) — see models.parse_split_value.
        Returns `default` when blank; notifies and returns None when unparseable."""
        raw_value = self.query_one("#split_value_input", Input).value.strip()
        if not raw_value and default is not None:
            return default
        try:
            return parse_split_value(raw_value)
        except ValueError:
            self.notify("Split size: 400 (metres), 1.5km, or a time like 60s, 1m, 1m30s, 1:30", severity="warning")
            return None

    def _split(self, records: list[fit_parser.RecordPoint], mode: str, value: float) -> list[Lap]:
        if mode == "distance":
            return fit_parser.split_by_distance(records, value, self._offset)
        return fit_parser.split_by_time(records, value, self._offset)

    def _do_split(self) -> None:
        parsed = self._read_split_value()
        if parsed is None:
            return
        if not self._ensure_records_loaded():
            return
        if not self._records:
            self.notify("No record data in this FIT file to split", severity="warning")
            return

        self.current_laps = self._split(self._records, *parsed)
        self.viewing_device_laps = False
        self.interval_splits = {}
        self._rebuild_table()
        self._refresh_interval_summary()

    def _do_reset(self) -> None:
        self.current_laps = storage.load_laps(self.activity.activity_id)
        self.viewing_device_laps = True
        self.interval_splits = {}
        self._rebuild_table()
        self._refresh_interval_summary()

    def action_toggle_interval(self) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._row_map):
            return
        lap_index = self._row_map[row]
        if lap_index is None:
            self.notify("Mark the parent lap, not a phase split", severity="warning")
            return
        lap = self.current_laps[lap_index]
        lap.is_interval = not lap.is_interval
        if not lap.is_interval:
            self.interval_splits.pop(lap.lap_index, None)
        if self.viewing_device_laps:
            storage.save_laps(self.activity.activity_id, self.current_laps)
        # A toggle changes more than the "Int" cell — duration precision and bold styling
        # follow is_interval too, and the row may need to appear/disappear under the active
        # filter — so a full rebuild is simpler and cheap at this table's size.
        self._rebuild_table()
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
        sport = _common_sport(marked, self.activity.sport)

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
            f"Intervals: {len(marked)}  |  Distance: {format_lap_distance(sport, total_distance)}  |  "
            f"Duration: {format_duration(total_duration)}  |  "
            f"Avg: {format_speed_or_pace(sport, avg_speed)}  |  "
            f"Avg HR: {format_hr(avg_hr)}  |  Avg Power: {format_power(avg_power)}"
        )

    def action_physical_laps(self) -> None:
        if not self._ensure_records_loaded():
            return
        if not self._records:
            self.notify("No record data in this FIT file to analyze", severity="warning")
            return

        # An optional value in the split input overrides the default "close to anchor"
        # radius — the auto-detect can miss real loops under tree cover/GPS drift (a
        # park loop never quite closing to within the default radius), so widening it
        # by hand is the escape hatch rather than a code change.
        parsed = self._read_split_value(default=("distance", fit_parser.DEFAULT_CLOSE_RADIUS_M))
        if parsed is None:
            return
        mode, close_radius_m = parsed
        if mode != "distance":
            self.notify("Loop radius must be a distance in metres, e.g. 100", severity="warning")
            return

        self.notify("Detecting course loops...")
        self._detect_physical_laps(close_radius_m)

    @work(thread=True)
    def _detect_physical_laps(self, close_radius_m: float) -> None:
        # Scores every candidate loop anchor across the whole track (see
        # fit_parser.detect_physical_laps) — can take several seconds on a long activity,
        # so it must not block the UI thread like the other split modes do.
        laps = fit_parser.detect_physical_laps(self._records, self._offset, close_radius_m=close_radius_m)
        self.app.call_from_thread(self._physical_laps_done, laps)

    def _physical_laps_done(self, laps: list[Lap] | None) -> None:
        if not laps:
            self.notify("No repeated course laps detected", severity="warning")
            return
        self.current_laps = laps
        self.viewing_device_laps = False
        self.interval_splits = {}
        self._rebuild_table()
        self._refresh_interval_summary()

    def action_split_intervals(self) -> None:
        marked = [lap for lap in self.current_laps if lap.is_interval]
        if not marked:
            self.notify("Mark at least one interval (m) before splitting it into phases", severity="warning")
            return

        parsed = self._read_split_value(default=("distance", self.DEFAULT_INTERVAL_SPLIT_M))
        if parsed is None:
            return

        if not self._ensure_records_loaded():
            return
        if not self._records:
            self.notify("No record data in this FIT file to split", severity="warning")
            return

        self.interval_splits = {}
        for lap in marked:
            lap_records = fit_parser.lap_record_slice(self._records, lap, self._offset)
            if lap_records:
                phases = self._split(lap_records, *parsed)
                for phase in phases:
                    phase.sport = lap.sport
                self.interval_splits[lap.lap_index] = phases
        self._rebuild_table()
        self.notify(f"Split {len(self.interval_splits)} interval(s) into {format_split_value(*parsed)} phases")

    def action_toggle_interval_filter(self) -> None:
        self.show_intervals_only = not self.show_intervals_only
        self.query_one("#interval_filter_button", Button).variant = (
            "primary" if self.show_intervals_only else "default"
        )
        self._rebuild_table()
        if self.show_intervals_only and not any(lap.is_interval for lap in self.current_laps):
            self.notify("No intervals marked yet (press m on a lap to mark it)", severity="warning")

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

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        _copy_to_clipboard(self, _table_as_tsv(table), f"{table.row_count} laps")

    def action_phase_metrics(self) -> None:
        # Interval phase-splits (finer-grained) take priority when present; otherwise
        # histogram whatever's currently in the table — a whole-activity split by
        # distance/time, detected physical laps, or (less usefully, but not blocked) the
        # plain device laps.
        if self.interval_splits:
            phases = [phase for phases in self.interval_splits.values() for phase in phases]
        else:
            phases = list(self.current_laps)
        if not phases:
            self.notify("No laps to show a histogram for", severity="warning")
            return
        self.app.push_screen(PhaseMetricsScreen(self.activity, phases))

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


def _histogram_bins(values: list[float], bin_width: float) -> list[tuple[float, int]]:
    """(bin_start, count) pairs covering every bin from the lowest to the highest occupied
    one, zero-count bins included — so the chart reads as a continuous distribution instead
    of skipping the gaps between whichever bins happen to have a phase in them."""
    if not values:
        return []
    counts = Counter(math.floor(v / bin_width) for v in values)
    lo, hi = min(counts), max(counts)
    return [(i * bin_width, counts.get(i, 0)) for i in range(lo, hi + 1)]


def _adaptive_bin_width(values: list[float], base_width: float, max_bins: int) -> float:
    """The narrowest whole multiple of `base_width` (base_width, 2x, 3x, ...) that keeps the
    value range within `max_bins` bins — a widely-spread set of phases (e.g. mixing short and
    long reps) grows from, say, 1s to 2s to 3s bins rather than flooding the histogram with
    dozens of mostly-empty bars."""
    if not values:
        return base_width
    span = max(values) - min(values)
    width = base_width
    while span / width > max_bins:
        width += base_width
    return width


def _bin_label(start: float, width: float, base_width: float, edge_fmt) -> str:
    """A bin's x-axis label: just its (formatted) start when the bin is its original,
    unwidened size, or a "start-end" range once `_adaptive_bin_width` has widened it —
    a single edge value would otherwise misread as the whole bin's worth of phases having
    that exact value."""
    if width <= base_width:
        return edge_fmt(start)
    return f"{edge_fmt(start)}-{edge_fmt(start + width)}"


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

    MAX_BINS = 12
    TIME_BIN_S = 1.0
    PACE_BIN_S_PER_KM = 5.0
    SPEED_BIN_KMH = 1.0
    POWER_BIN_W = 5.0

    def __init__(self, activity: ActivitySummary, phases: list[Lap]) -> None:
        super().__init__()
        self.activity = activity
        self.phases = phases
        # chart title -> [(bin label, count)], as last drawn — for copying to the clipboard
        self.histogram_data: dict[str, list[tuple[str, int]]] = {}

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
        phases = self.phases
        sport = _common_sport(phases, self.activity.sport)
        pace_sport = sport in PACE_SPORTS

        self._draw_histogram(
            "phase_time_plot",
            [p.duration_s for p in phases],
            self.TIME_BIN_S,
            edge_fmt=lambda v: f"{int(round(v))}s",
            unit_suffix="s",
            title="Duration histogram",
        )

        if sport in SWIM_SPORTS:
            self._draw_histogram(
                "phase_pace_plot",
                [100 / p.avg_speed_mps for p in phases if p.avg_speed_mps],
                self.PACE_BIN_S_PER_KM,
                edge_fmt=format_duration,
                unit_suffix="s/100m",
                title="Pace histogram",
            )
        elif pace_sport:
            self._draw_histogram(
                "phase_pace_plot",
                [1000 / p.avg_speed_mps for p in phases if p.avg_speed_mps],
                self.PACE_BIN_S_PER_KM,
                edge_fmt=format_duration,  # a sec/km edge value reads directly as m:ss pace
                unit_suffix="s/km",
                title="Pace histogram",
            )
        else:
            self._draw_histogram(
                "phase_pace_plot",
                [p.avg_speed_mps * 3.6 for p in phases if p.avg_speed_mps],
                self.SPEED_BIN_KMH,
                edge_fmt=lambda v: f"{v:.0f} km/h",
                unit_suffix=" km/h",
                title="Speed histogram",
            )

        self._draw_histogram(
            "phase_power_plot",
            [p.avg_power for p in phases if p.avg_power is not None],
            self.POWER_BIN_W,
            edge_fmt=lambda v: f"{int(round(v))}W",
            unit_suffix="W",
            title="Power histogram",
        )

    def _draw_histogram(
        self, widget_id: str, values: list[float], base_bin_width: float, edge_fmt, unit_suffix: str, title: str
    ) -> None:
        plot = self.query_one(f"#{widget_id}", PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        bin_width = _adaptive_bin_width(values, base_bin_width, self.MAX_BINS)
        bins = _histogram_bins(values, bin_width)
        bins_desc = f"{int(bin_width)}{unit_suffix} bins"
        if bins:
            labels = [_bin_label(start, bin_width, base_bin_width, edge_fmt) for start, _ in bins]
            counts = [count for _, count in bins]
            self.histogram_data[f"{title} ({bins_desc})"] = list(zip(labels, counts))
            plt.bar(labels, counts)
            # A phase count is always a whole number — without this plotext picks fractional
            # y-axis ticks (e.g. 0.00, 0.67, 1.33) to fill the plot height, which misreads as
            # if fractional phase counts were possible.
            plt.yticks(range(0, max(counts) + 1))
        plt.title(f"{title} ({bins_desc})" if bins else f"{title} — no data")
        plt.ylabel("Phases")
        plot.refresh()

    def action_copy(self) -> None:
        lines = ["Chart\tBin\tPhases"]
        for chart, rows in self.histogram_data.items():
            lines.extend(f"{chart}\t{label}\t{count}" for label, count in rows)
        _copy_to_clipboard(self, "\n".join(lines), f"{len(self.histogram_data)} histograms")

    def action_back(self) -> None:
        self.app.pop_screen()


class RoutesScreen(Screen):
    CSS = """
    #rename_input {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("e", "rename", "Rename route"),
    ]

    ROUTE_COLUMNS = ("Name", "Sport", "Place", "Activities", "Distance")

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.all_routes: list[Route] = []
        self.routes_by_key: dict[str, Route] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Routes", id="routes_status")
        yield HorizontalGroup(
            Input(placeholder="New name", id="rename_input"),
            Button("Rename", id="rename_button"),
            id="rename_bar",
        )
        yield DataTable(id="routes_table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*self.ROUTE_COLUMNS)
        self._refresh_table()
        # Without this, Textual auto-focuses the rename Input, and the "e" binding
        # silently types into it instead of firing.
        table.focus()

    def _refresh_table(self) -> None:
        self.all_routes = storage.load_routes()
        _, activities = storage.load_index()

        counts: dict[str, int] = {}
        for activity in activities:
            if activity.route_id:
                counts[activity.route_id] = counts.get(activity.route_id, 0) + 1

        # Routes with only one activity aren't a "route" worth tracking yet.
        grouped = [r for r in self.all_routes if counts.get(r.route_id, 0) >= 2]
        self.routes_by_key = {r.route_id: r for r in grouped}

        table = self.query_one(DataTable)
        table.clear()
        for route in sorted(grouped, key=lambda r: (-counts[r.route_id], r.name)):
            table.add_row(
                route.name,
                route.sport,
                route.place or "–",
                str(counts[route.route_id]),
                format_distance(route.signature.distance_m),
                key=route.route_id,
            )
        self.query_one("#routes_status", Static).update(f"{len(grouped)} routes with 2+ activities")

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
        table.add_columns(*ACTIVITY_COLUMNS)
        _, activities = storage.load_index()
        matching = [a for a in activities if a.route_id == self.route.route_id]
        self.activities_by_id = {a.activity_id: a for a in matching}
        for activity in sorted(matching, key=lambda a: a.start_time_local, reverse=True):
            table.add_row(*_activity_row(activity, self.route.name), key=activity.activity_id)
        self.query_one("#route_status", Static).update(
            f"Route: {self.route.name}  |  {len(matching)} activities"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        activity = self.activities_by_id.get(event.row_key.value)
        if activity is not None:
            self.app.push_screen(LapsScreen(activity, self.client))

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


class WorkoutsScreen(Screen):
    """Workout JSON files in storage.WORKOUTS_DIR (written by hand or by another agent against
    workout_schema.json): validate, preview, and upload them to Garmin Connect.

    Loose files sit at the top. A week's plan delivered as `<week>.zip` is unpacked into
    `<week>/` (see workouts.extract_new_archives) and listed as a group that can be uploaded
    in one go with U. Rows are keyed by path relative to WORKOUTS_DIR ("week/file.json"),
    which is also the key in the uploaded-workouts ledger; week header rows are "week:<dir>".
    """

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
        Binding("r", "refresh", "Refresh"),
    ]

    COLUMNS = ("File", "Date", "Name", "Sport", "Status", "Uploaded")
    WEEK_KEY_PREFIX = "week:"

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        # relative path -> validated workout dict, or the validation error text
        self.entries: dict[str, dict | str] = {}
        # week folder name -> relative paths of its workouts, in date order
        self.week_dirs: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Workouts in {storage.WORKOUTS_DIR}", id="workouts_status")
        yield DataTable(id="workouts_table")
        yield Static("", id="workout_preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*self.COLUMNS)
        self.action_refresh()
        table.focus()

    def _load_entry(self, path: Path) -> dict | str:
        try:
            return workouts.load(path)
        except workouts.WorkoutError as e:
            return str(e)

    def _entry_cells(self, key: str, label: str, uploaded: dict[str, int]) -> tuple:
        entry = self.entries[key]
        uploaded_id = uploaded.get(key)
        if isinstance(entry, dict):
            return (label, entry.get("schedule_date", ""), entry["name"], entry["sport"], "OK",
                    str(uploaded_id) if uploaded_id else "")
        return (label, "", "–", "–", Text("invalid", style="red"), str(uploaded_id) if uploaded_id else "")

    def action_refresh(self) -> None:
        root = storage.WORKOUTS_DIR
        root.mkdir(parents=True, exist_ok=True)
        for message in workouts.extract_new_archives(root):
            self.notify(message, severity="error" if "Unpacked" not in message else "information")

        uploaded = storage.load_uploaded_workouts()
        previous_key = self._selected_key()
        self.entries = {}
        self.week_dirs = {}
        table = self.query_one(DataTable)
        table.clear()

        for path in sorted(root.glob("*.json")):
            self.entries[path.name] = self._load_entry(path)
            table.add_row(*self._entry_cells(path.name, path.name, uploaded), key=path.name)

        for week_dir in sorted(d for d in root.iterdir() if d.is_dir()):
            keys = []
            for path in week_dir.glob("*.json"):
                key = f"{week_dir.name}/{path.name}"
                self.entries[key] = self._load_entry(path)
                keys.append(key)
            if not keys:
                continue
            keys.sort(key=lambda k: (self._date_of(k), k))
            self.week_dirs[week_dir.name] = keys

            dates = [d for d in (self._date_of(k) for k in keys) if d]
            invalid = sum(not isinstance(self.entries[k], dict) for k in keys)
            done = sum(k in uploaded for k in keys)
            bold = "bold"
            table.add_row(
                Text(f"▾ {week_dir.name}", style=bold),
                Text(f"{dates[0]} – {dates[-1]}" if dates else "", style=bold),
                Text(f"{len(keys)} workouts", style=bold),
                "",
                Text(f"{invalid} invalid", style="bold red") if invalid else Text("OK", style=bold),
                Text(f"{done}/{len(keys)}", style=bold),
                key=f"{self.WEEK_KEY_PREFIX}{week_dir.name}",
            )
            for key in keys:
                table.add_row(*self._entry_cells(key, f"  {key.split('/', 1)[1]}", uploaded), key=key)

        self.query_one("#workouts_status", Static).update(
            f"{len(self.entries)} workout(s), {len(self.week_dirs)} week plan(s) in {root}"
        )
        if previous_key in table.rows:
            table.move_cursor(row=table.get_row_index(previous_key))
        self._show_preview(self._selected_key())

    def _date_of(self, key: str) -> str:
        entry = self.entries.get(key)
        return entry.get("schedule_date", "") if isinstance(entry, dict) else ""

    def _selected_key(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value

    def _week_of(self, key: str | None) -> str | None:
        """The week folder a row belongs to (its header row or one of its workouts)."""
        if key is None:
            return None
        if key.startswith(self.WEEK_KEY_PREFIX):
            return key[len(self.WEEK_KEY_PREFIX):]
        return key.split("/", 1)[0] if "/" in key else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_preview(event.row_key.value if event.row_key else None)

    def _show_preview(self, key: str | None) -> None:
        preview = self.query_one("#workout_preview", Static)
        if key is not None and key.startswith(self.WEEK_KEY_PREFIX):
            uploaded = storage.load_uploaded_workouts()
            lines = [f"Week plan {self._week_of(key)} — press U to upload the whole week", ""]
            for member in self.week_dirs.get(self._week_of(key), []):
                entry = self.entries[member]
                mark = "✓ uploaded" if member in uploaded else ""
                if isinstance(entry, dict):
                    lines.append(f"{entry.get('schedule_date', '          ')}  {workouts.describe(entry)[0]}  {mark}")
                else:
                    lines.append(f"{member}: INVALID")
            preview.update("\n".join(lines))
            return
        entry = self.entries.get(key) if key else None
        if entry is None:
            preview.update(
                "Put workout .json files, or a week's plan as a .zip of them, in the folder above "
                "(see workout_schema.json), then press r."
            )
        elif isinstance(entry, str):
            preview.update(Text(f"Validation errors:\n{entry}", style="red"))
        else:
            preview.update("\n".join(workouts.describe(entry)))

    def action_upload(self) -> None:
        key = self._selected_key()
        if key is not None and key.startswith(self.WEEK_KEY_PREFIX):
            self.notify("This is a week — press U to upload all of it, or select one workout", severity="warning")
            return
        entry = self.entries.get(key) if key else None
        if not isinstance(entry, dict):
            self.notify("Select a valid workout to upload", severity="warning")
            return
        if key in storage.load_uploaded_workouts():
            self.notify("Already uploaded — uploading again creates a second copy in Garmin", severity="warning")
        self._upload_many([(key, entry)])

    def action_upload_week(self) -> None:
        week = self._week_of(self._selected_key())
        if week is None or week not in self.week_dirs:
            self.notify("Select a week plan (or one of its workouts) to upload the week", severity="warning")
            return
        keys = self.week_dirs[week]
        invalid = [k for k in keys if not isinstance(self.entries[k], dict)]
        if invalid:
            self.notify(
                f"{len(invalid)} workout(s) in {week} are invalid — fix or remove them first: "
                + ", ".join(k.split("/", 1)[1] for k in invalid),
                severity="error",
            )
            return
        uploaded = storage.load_uploaded_workouts()
        pending = [k for k in keys if k not in uploaded]
        if not pending:
            self.notify(f"Everything in {week} is already uploaded")
            return

        today = date.today().isoformat()
        lines = [f"Upload {len(pending)} workout(s) from {week} to Garmin Connect and schedule them?", ""]
        for key in keys:
            entry = self.entries[key]
            when = entry.get("schedule_date") or "not scheduled"
            note = "  (already uploaded — skipped)" if key in uploaded else ""
            if key not in uploaded and entry.get("schedule_date") and entry["schedule_date"] < today:
                note = "  ⚠ date is in the past"
            lines.append(f"  {when:<13} {entry['name']} [{entry['sport']}]{note}")
        self.app.push_screen(
            ConfirmScreen("\n".join(lines), "Upload"),
            lambda confirmed: self._upload_many([(k, self.entries[k]) for k in pending]) if confirmed else None,
        )

    def _upload_one(self, key: str, entry: dict) -> str:
        """Create one workout in Garmin, record it in the ledger, then schedule it. Returns a
        warning when the workout was created but scheduling failed. The ledger is written
        before scheduling on purpose: an unrecorded upload would get uploaded again on retry,
        leaving a duplicate workout in Garmin."""
        workout_id = garmin_client.upload_workout(self.client, workouts.to_garmin(entry))
        uploaded = storage.load_uploaded_workouts()
        uploaded[key] = workout_id
        storage.save_uploaded_workouts(uploaded)
        if entry.get("schedule_date"):
            try:
                garmin_client.schedule_workout(self.client, workout_id, entry["schedule_date"])
            except Exception as e:  # the workout exists; only the calendar entry is missing
                return f"{entry['name']} uploaded but not scheduled: {e}"
        return ""

    @work(thread=True, exclusive=True, group="workout_upload")
    def _upload_many(self, items: list[tuple[str, dict]]) -> None:
        status = self.query_one("#workouts_status", Static)
        failures, warnings = [], []
        done = 0
        for i, (key, entry) in enumerate(items, start=1):
            self.app.call_from_thread(status.update, f"Uploading {i}/{len(items)}: {entry['name']}...")
            try:
                warning = self._upload_one(key, entry)
            except Exception as e:  # surface any API rejection (e.g. a malformed DTO) and carry on
                failures.append(f"{entry['name']}: {e}")
                continue
            done += 1
            if warning:
                warnings.append(warning)

        for message in failures:
            self.app.call_from_thread(self.notify, f"Upload failed — {message}", severity="error", timeout=15)
        for message in warnings:
            self.app.call_from_thread(self.notify, message, severity="warning", timeout=15)
        if done:
            self.app.call_from_thread(self.notify, f"Uploaded {done} of {len(items)} workout(s) to Garmin Connect")
        self.app.call_from_thread(self.action_refresh)

    def action_back(self) -> None:
        self.app.pop_screen()
