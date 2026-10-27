"""Browser GUI (NiceGUI): the TUI's screens with real web graphics — AG Grid tables, ECharts
charts, Leaflet maps, stat tiles — and the same single-key shortcuts, shown in a clickable
footer. All behaviour comes from the shared modules (services, laps_session,
workout_library, display); this file is only presentation.

Served on 127.0.0.1 only: the app holds a logged-in Garmin session, so it must not be
reachable from the network. Map tiles are loaded by the browser straight from
OpenStreetMap (with the attribution Leaflet shows).
"""

import argparse
from datetime import date, timedelta

from nicegui import app, background_tasks, run, ui

import display
import fit_parser
import garmin_client
import services
import settings
import storage
import training_export
import training_load
import workout_library
from theme import COLORS, SERIES_COLORS, SPORT_COLORS
from laps_session import MAP_METRICS, LapsError, LapsSession
from models import format_distance
from web_common import (
    APP_TITLE,
    ECHART_BASE,
    Frame,
    State,
    axis_style,
    copy,
    draw_track,
    escape,
    grid,
    grid_rows,
    layers_payload,
    legend_html,
    notify,
    on_row_change,
    run_with_progress,
    select_row,
    selected_row,
    set_selected,
    stat_tiles,
    track_payload,
)
from workout_library import LibraryError, WorkoutLibrary

ALL_SPORTS = "__all__"


# --- Login ---------------------------------------------------------------------------


@ui.page("/login")
def login_page() -> None:
    Frame("Log in to Garmin Connect")
    pending = {"garmin": None}
    with ui.card().classes("ta-card p-6 gap-3").style("width: 420px"):
        ui.label("Log in to Garmin Connect").classes("text-lg font-semibold")
        status = ui.label("").classes("ta-status")
        email = ui.input("Email").classes("w-full")
        password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")
        mfa = ui.input("MFA code").classes("w-full")
        mfa.visible = False
        submit = ui.button("Log in").classes("w-full")

    async def do_submit() -> None:
        submit.disable()
        try:
            if pending["garmin"] is not None:
                status.set_text("Verifying code...")
                client = await run.io_bound(garmin_client.complete_mfa, pending["garmin"], mfa.value.strip())
            else:
                status.set_text("Logging in...")
                client = await run.io_bound(garmin_client.begin_login, email.value.strip(), password.value)
        except garmin_client.MfaRequired as e:
            pending["garmin"] = e.garmin
            status.set_text("Enter the MFA code sent to your device")
            mfa.visible = True
            submit.enable()
            return
        except Exception as e:  # wrong password, network...
            status.set_text("")
            notify(str(e), "error")
            submit.enable()
            return
        State.client = client
        ui.navigate.to("/")

    submit.on_click(do_submit)
    for field in (email, password, mfa):
        field.on("keydown.enter", do_submit)


# --- Activities ----------------------------------------------------------------------


@ui.page("/")
def main_page() -> None:
    if State.client is None and not storage.SESSION_DIR.exists():
        ui.navigate.to("/login")
        return
    frame = Frame("Activities")
    state = {"shown": [], "sport": ALL_SPORTS}
    status = ui.label("").classes("ta-status")
    with ui.row().classes("items-center gap-2"):
        sync_button = ui.button("Sync (s)")
        reprocess_button = ui.button("Reprocess (r)").props("outline")
        ui.button("Routes (o)", on_click=lambda: ui.navigate.to("/routes")).props("outline")
        ui.button("Workouts (w)", on_click=lambda: ui.navigate.to("/workouts")).props("outline")
        ui.button("Calendar (c)", on_click=lambda: ui.navigate.to("/calendar")).props("outline")
        sport_select = ui.select({ALL_SPORTS: "All sports"}, value=ALL_SPORTS).classes("w-48").props("dense outlined")
    table = grid(display.ACTIVITY_COLUMNS, [], height="calc(100vh - 230px)")

    def render() -> None:
        activities, hidden = services.listed_activities()
        routes_by_id = {r.route_id: r for r in storage.load_routes()}
        sports = sorted({a.sport for a in activities})
        sport_select.set_options({ALL_SPORTS: "All sports", **{s: s.title() for s in sports}}, value=state["sport"] if state["sport"] in sports else ALL_SPORTS)
        shown = sorted((a for a in activities if state["sport"] == ALL_SPORTS or a.sport == state["sport"]), key=lambda a: a.start_time_local, reverse=True)
        state["shown"] = shown
        rows = [display.activity_row(a, routes_by_id[a.route_id].name if a.route_id in routes_by_id else "–") for a in shown]
        table.options["rowData"] = grid_rows(rows, [{"_sport_color": SPORT_COLORS.get(a.sport, COLORS["fg"])} for a in shown])
        table.options["columnDefs"][2][":cellStyle"] = "params => ({color: params.data._sport_color})"
        table.update()
        status.set_text(display.activities_status(len(shown), len(activities), hidden))

    def on_sport(e) -> None:
        state["sport"] = e.value or ALL_SPORTS
        render()

    sport_select.on_value_change(on_sport)

    def open_row(row: int | None) -> None:
        if row is not None and 0 <= row < len(state["shown"]):
            ui.navigate.to(f"/activity/{state['shown'][row].activity_id}")

    table.on("rowDoubleClicked", lambda e: open_row(e.args["data"]["_row"]))

    async def open_selected() -> None:
        open_row(await selected_row(table))

    async def job(fn, *args) -> None:
        sync_button.disable()
        reprocess_button.disable()
        try:
            message = await run_with_progress(status, fn, *args)
        except Exception as e:
            message = f"Failed: {e}"
        sync_button.enable()
        reprocess_button.enable()
        render()
        status.set_text(message)
        notify(message)

    async def sync() -> None:
        if State.client is None:
            notify("Not logged in to Garmin", "warning")
            return
        await job(services.sync_activities, State.client)

    async def sync_history() -> None:
        if State.client is None:
            notify("Not logged in to Garmin", "warning")
            return
        with ui.dialog() as dialog, ui.card().classes("ta-card p-6"):
            ui.html(escape(services.FULL_HISTORY_PROMPT)).classes("ta-pre")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("outline")
                ui.button("Sync full history", on_click=lambda: dialog.submit(True))
        confirmed = await dialog
        dialog.delete()
        if confirmed:
            await job(lambda progress: services.sync_activities(State.client, progress, full_history=True))

    sync_button.on_click(sync)
    reprocess_button.on_click(lambda: job(services.reprocess_activities))
    frame.bind("s", "Sync", sync)
    frame.bind("r", "Reprocess", lambda: job(services.reprocess_activities))
    frame.bind("o", "Routes", lambda: ui.navigate.to("/routes"))
    frame.bind("w", "Workouts", lambda: ui.navigate.to("/workouts"))
    frame.bind("c", "Calendar & load", lambda: ui.navigate.to("/calendar"))
    frame.bind("p", "Settings", lambda: ui.navigate.to("/settings"))
    frame.bind("H", "Sync full history", sync_history)
    frame.bind("Enter", "Open", open_selected, shown_key="enter")
    frame.bind("y", "Copy table", lambda: copy(display.tsv(display.ACTIVITY_COLUMNS, [display.activity_row(a, "") for a in state["shown"]]), f"{len(state['shown'])} activities"))
    render()


# --- Settings --------------------------------------------------------------------------


@ui.page("/settings")
def settings_page() -> None:
    frame = Frame("Settings")
    values = settings.load()
    inputs: dict[str, object] = {}
    with ui.column().classes("gap-1"):
        for field in settings.FIELDS:
            if field.kind == "bool":
                inputs[field.key] = ui.checkbox(field.label, value=values[field.key])
            else:
                ui.label(field.label).classes("font-semibold mt-2")
                with ui.row().classes("gap-2"):
                    inputs[field.key] = {
                        sport: ui.checkbox(sport, value=sport in values[field.key]) for sport in settings.SPORT_GROUPS
                    }
            ui.label(field.help).classes("ta-status")

    def save() -> None:
        new = {}
        for field in settings.FIELDS:
            widget = inputs[field.key]
            new[field.key] = widget.value if field.kind == "bool" else [s for s, box in widget.items() if box.value]
        settings.save(new)
        notify(f"Settings saved to {storage.SETTINGS_PATH}")
        ui.navigate.back()

    with ui.row().classes("gap-2 mt-4"):
        ui.button("Save", on_click=save)
        ui.button("Cancel", on_click=ui.navigate.back).props("outline")
    frame.bind_back()


# --- Laps (with map / phases / compare / zones tabs) --------------------------------

LAP_ROW_STYLE = (
    "params => params.data._kind === 'interval' ? {fontWeight: 600}"
    f" : params.data._kind === 'phase' ? {{color: '{COLORS['comment']}', fontStyle: 'italic'}}"
    f" : params.data._kind === 'header' ? {{fontWeight: 700, color: '{COLORS['accent']}', background: '{COLORS['surface2']}'}} : {{}}"
)
MAP_MODES = [(str(key), text, metric) for key, (metric, text) in enumerate(MAP_METRICS.items(), start=1)]


@ui.page("/activity/{activity_id}")
def laps_page(activity_id: str) -> None:
    _, activities = storage.load_index()
    activity = next((a for a in activities if a.activity_id == activity_id), None)
    frame = Frame(activity.name if activity else "Activity not found")
    if activity is None:
        frame.bind_back()
        return
    session = LapsSession(activity)
    state = {"rows": [], "map_mode": "speed_mps", "x_mode": "time"}

    with ui.row().classes("items-center gap-2"):
        ui.html(f"<span style='color:{SPORT_COLORS.get(activity.sport, COLORS['fg'])}; font-weight:600'>{activity.sport}</span>")
        ui.label(f"·  {activity.start_time_local.replace('T', ' ')[:16]}").classes("ta-status")
    stat_tiles(display.activity_stats(activity))
    metrics_area = ui.column().classes("w-full")  # filled once the record stream is loaded
    if session.multisport:
        with ui.row().classes("w-full gap-3 no-wrap"):
            for name, sport, stats in display.leg_stats(storage.load_laps(activity_id)):
                lines = "".join(
                    f"<div><span class='unit' style='margin:0'>{s.label}</span> <b style='color:{COLORS.get(s.color, COLORS['fg']) if s.color else COLORS['fg']}'>{s.value}</b><span class='unit'>{s.unit}</span></div>"
                    for s in stats
                )
                ui.html(f"<div class='val' style='font-size:1.1rem; color:{SPORT_COLORS.get(sport, COLORS['fg'])}'>{name}</div>{lines}").classes("ta-tile")

    with ui.tabs().classes("w-full").props("active-color=primary indicator-color=primary align=left") as tabs:
        laps_tab = ui.tab("Laps")
        map_tab = ui.tab("Map (g)")
        phases_tab = ui.tab("Phases (h)")
        compare_tab = ui.tab("Compare (c)")
        zones_tab = ui.tab("Zones (z)")
        sets_tab = ui.tab("Sets (t)") if services.is_strength(activity) else None
    # A gym session opens on its sets; everything else on its laps.
    with ui.tab_panels(tabs, value=sets_tab or laps_tab).classes("w-full").style("background: transparent"):
        with ui.tab_panel(laps_tab).classes("p-0 gap-2"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                split_input = ui.input(placeholder="400 = metres · 60s / 1m / 1:30 = time").classes("flex-grow").props("dense outlined")
                ui.button("Split", on_click=lambda: run_op(lambda: session.split(split_input.value or ""))).props("outline")
                ui.button("Split intervals (i)", on_click=lambda: split_intervals()).props("outline")
                filter_button = ui.button("Interval only (f)", on_click=lambda: toggle_filter()).props("outline")
                ui.button("Reset", on_click=lambda: run_op(session.reset)).props("outline")
            # The table only needs part of the width: the rest shows the track, with the selected
            # row's lap highlighted (g opens the big map tab).
            with ui.row().classes("w-full no-wrap gap-3"):
                table = grid(session.columns, [], height="calc(100vh - 470px)", row_style=LAP_ROW_STYLE).style("flex: 3 1 0")
                side_map = ui.leaflet(center=(52.0, 19.0), zoom=13).style("flex: 2 1 0; height: calc(100vh - 470px)")
            interval_area = ui.column().classes("w-full")
        with ui.tab_panel(map_tab).classes("p-0 gap-2"):
            with ui.row().classes("items-center gap-2"):
                map_buttons = {attr: ui.button(f"{text} ({key})", on_click=lambda a=attr: draw_map(a)).props("outline") for key, text, attr in MAP_MODES}
            leaflet = ui.leaflet(center=(52.0, 19.0), zoom=13).classes("w-full").style("height: calc(100vh - 420px)")
            map_legend = ui.html("")
        with ui.tab_panel(phases_tab).classes("p-0"):
            phases_area = ui.column().classes("w-full gap-2")
        with ui.tab_panel(compare_tab).classes("p-0"):
            with ui.row().classes("items-center gap-2"):
                x_buttons = {mode: ui.button(text, on_click=lambda m=mode: draw_compare(m)).props("outline") for text, mode in (("Time", "time"), ("Distance", "distance"), ("%", "percent"))}
            compare_area = ui.column().classes("w-full gap-2")
        with ui.tab_panel(zones_tab).classes("p-0"):
            zones_area = ui.column().classes("w-full gap-2")
        if sets_tab is not None:
            with ui.tab_panel(sets_tab).classes("p-0"):
                sets_area = ui.column().classes("w-full gap-2")

    def rebuild(keep_row: int | None = None) -> None:
        state["rows"] = session.rows()
        table.options["rowData"] = grid_rows([r.values for r in state["rows"]], [{"_kind": r.kind} for r in state["rows"]])
        for column in session.right_aligned_columns:
            table.options["columnDefs"][column]["cellStyle"] = {"textAlign": "right"}
        table.update()
        hint = session.take_cursor_hint()
        keep_row = hint if hint is not None else keep_row
        select_row(table, keep_row)
        if session.records is not None:  # laps/marks changed: redraw the side map's highlights
            background_tasks.create(refresh_side_map(keep_row))
        interval_area.clear()
        stats = display.interval_stats(session.marked_laps(), activity.sport)
        with interval_area:
            if stats:
                stat_tiles(stats)
            else:
                ui.label(session.interval_summary()).classes("ta-status")

    def run_op(operation, keep_row: int | None = None) -> bool:
        try:
            message = operation()
        except LapsError as e:
            notify(str(e), "warning")
            return False
        rebuild(keep_row)
        if message:
            notify(message)
        return True

    async def toggle_interval() -> None:
        row = await selected_row(table)
        if row is not None:
            run_op(lambda: session.toggle_interval(row), keep_row=row)

    def split_intervals() -> None:
        run_op(lambda: session.split_intervals(split_input.value or ""))

    def toggle_filter() -> None:
        def toggle():
            warning = session.toggle_filter()
            filter_button.props(remove="outline") if session.show_intervals_only else filter_button.props("outline")
            if warning:
                notify(warning, "warning")
        run_op(toggle)

    async def physical_laps() -> None:
        try:
            radius = session.physical_laps_radius(split_input.value or "")
        except LapsError as e:
            notify(str(e), "warning")
            return
        notify("Detecting course loops...")
        laps = await run.io_bound(session.detect_physical_laps, radius)
        run_op(lambda: session.apply_physical_laps(laps))

    async def initial_load() -> None:
        """Track on the side map, then the PHYSICAL section (seconds each on a long ride)."""
        await refresh_side_map(0, fit=True)
        if session.records is not None:
            extra = display.activity_metric_stats(await run.io_bound(session.advanced_metrics))
            if extra:
                with metrics_area:
                    stat_tiles(extra)
        if not session.auto_detect:
            return
        try:
            laps = await run.io_bound(session.detect_physical_laps)
        except LapsError:
            session.physical_laps_failed()
            rebuild(await selected_row(table))
            return
        run_op(lambda: session.apply_physical_laps(laps, explicit=False), keep_row=await selected_row(table))

    async def refresh_side_map(row: int | None = None, fit: bool = False) -> None:
        selected = session.row_lap(row) if row is not None else None
        try:
            # First open of a long activity parses its FIT file (seconds): off the event loop.
            layers = await run.io_bound(session.map_layers, "speed_mps", selected)
        except LapsError:
            return
        if layers is not None:
            await draw_track(side_map, await run.io_bound(layers_payload, layers), fit=fit)

    async def draw_map(attr: str | None = None) -> None:
        tabs.set_value(map_tab)
        state["map_mode"] = attr or state["map_mode"]
        for a, b in map_buttons.items():
            b.props(remove="outline") if a == state["map_mode"] else b.props("outline")
        row = await selected_row(table)
        selected_lap = session.row_lap(row) if row is not None else None
        mode = state["map_mode"]
        if session.records is None:
            map_legend.set_content("<span class='ta-status'>Loading track...</span>")
        try:
            layers = await run.io_bound(session.map_layers, mode, selected_lap)
        except LapsError as e:
            notify(str(e), "warning")
            return
        if layers is None:
            map_legend.set_content("<span class='ta-status'>No GPS track for this activity</span>")
            return
        payload = await run.io_bound(layers_payload, layers)
        await draw_track(leaflet, payload)
        map_legend.set_content(legend_html(mode, activity.sport, payload["range"]))

    def show_phases() -> None:
        try:
            histograms = display.phase_histograms(session.histogram_phases(), activity.sport)
        except LapsError as e:
            notify(str(e), "warning")
            return
        tabs.set_value(phases_tab)
        phases_area.clear()
        with phases_area:
            for (title, rows), color in zip(histograms, (COLORS["accent"], COLORS["green"], COLORS["magenta"])):
                ui.echart({
                    **ECHART_BASE, "title": {"text": title, "left": "center", "textStyle": {"color": COLORS["fg"], "fontSize": 13}},
                    "xAxis": axis_style(type="category", data=[bar.label for bar in rows]),
                    "yAxis": axis_style(type="value", minInterval=1, name="Phases"),
                    # Outlier edge bars ("<x" / "≥y") in grey, the regular ones in the chart colour.
                    "series": [{"type": "bar", "data": [
                        {"value": bar.count, "itemStyle": {"color": COLORS["comment"] if bar.outlier else color}} for bar in rows
                    ], "itemStyle": {"borderRadius": [4, 4, 0, 0]}}],
                }).classes("w-full").style("height: 30vh")

    def draw_compare(mode: str | None = None) -> None:
        try:
            marked, records, offset = session.compare_inputs()
        except LapsError as e:
            notify(str(e), "warning")
            return
        tabs.set_value(compare_tab)
        state["x_mode"] = mode or state["x_mode"]
        for m, b in x_buttons.items():
            b.props(remove="outline") if m == state["x_mode"] else b.props("outline")
        compare_area.clear()
        with compare_area:
            for i, (attr, title, scale) in enumerate(display.COMPARE_METRICS):
                series = display.compare_series(marked, records, offset, attr, state["x_mode"], scale)
                ui.echart({
                    **ECHART_BASE, "title": {"text": title, "left": "center", "textStyle": {"color": COLORS["fg"], "fontSize": 13}},
                    "legend": {"show": i == 0, "top": 22, "textStyle": {"color": COLORS["fg"]}},
                    "grid": {**ECHART_BASE["grid"], "top": 55 if i == 0 else 40},
                    "xAxis": axis_style(type="value", name=display.COMPARE_X_AXIS_LABELS[state["x_mode"]], nameLocation="middle", nameGap=25),
                    "yAxis": axis_style(type="value", scale=True),
                    "series": [{"type": "line", "name": label, "showSymbol": False, "data": [[x, round(y, 1)] for x, y in zip(xs, ys)],
                                "lineStyle": {"width": 1.6}, "color": SERIES_COLORS[n % len(SERIES_COLORS)]} for n, (label, xs, ys) in enumerate(series)],
                }).classes("w-full").style("height: 30vh")

    async def show_zones() -> None:
        tabs.set_value(zones_tab)
        zones_area.clear()
        cached = storage.load_zones(activity_id)
        if cached is None:
            if State.client is None:
                with zones_area:
                    ui.label("Not logged in — zones aren't cached for this activity").classes("ta-status")
                return
            with zones_area:
                waiting = ui.label("Fetching zones from Garmin Connect...").classes("ta-status")

            def fetch():
                hr = garmin_client.get_hr_zones(State.client, activity_id)
                power = garmin_client.get_power_zones(State.client, activity_id)
                storage.save_zones(activity_id, hr, power)
                return hr, power

            try:
                cached = await run.io_bound(fetch)
            except Exception as e:
                waiting.set_text(f"Couldn't fetch zones: {e}")
                return
            waiting.delete()
        with zones_area:
            for title, zones in zip(("Heart rate", "Power"), cached):
                ui.label(title).classes("font-semibold")
                grid(display.ZONE_COLUMNS, grid_rows(display.zone_rows(zones)), height="28vh")

    async def show_sets() -> None:
        if sets_tab is None:
            notify("Not a strength activity", "warning")
            return
        state["sets_loaded"] = True
        tabs.set_value(sets_tab)
        sets_area.clear()
        with sets_area:
            status = ui.label("Fetching sets from Garmin Connect...").classes("ta-status")
        try:
            sets = await run.io_bound(services.exercise_sets, State.client, activity_id)
        except Exception as e:
            status.set_text(f"Couldn't fetch sets: {e}")
            return
        if sets is None:
            status.set_text("Not logged in — sets aren't downloaded for this activity")
            return
        status.delete()
        with sets_area:
            ui.html(escape(display.strength_summary(sets))).classes("ta-pre")
            grid(display.SET_COLUMNS, grid_rows(display.strength_rows(sets)), height="55vh")

    async def go_back() -> None:
        if tabs.value != laps_tab and tabs.value != "Laps":
            tabs.set_value(laps_tab)
        else:
            ui.navigate.back()

    frame.bind_back(go_back)
    frame.bind("m", "Mark interval", toggle_interval)
    frame.bind("z", "Zones", show_zones)
    frame.bind("t", "Strength sets", show_sets)
    if sets_tab is not None:  # clicking the tab loads it too, not only the t key
        tabs.on_value_change(lambda e: show_sets() if e.value in (sets_tab, "Sets (t)") and not state.get("sets_loaded") else None)
    frame.bind("c", "Compare intervals", draw_compare)
    frame.bind("p", "Re-detect loops", physical_laps)
    frame.bind("i", "Split intervals", split_intervals)
    frame.bind("f", "Interval-only", toggle_filter)
    frame.bind("h", "Phase time/pace/power", show_phases)
    frame.bind("g", "Map", draw_map)
    for key, text, attr in MAP_MODES:
        frame.keys[key] = lambda a=attr: draw_map(a) if tabs.value in (map_tab, "Map (g)") else None
    frame.bind("y", "Copy table", lambda: copy(display.tsv(session.columns, [r.values for r in state["rows"]]), f"{len(state['rows'])} laps"))
    on_row_change(table, lambda row: set_selected(side_map, session.lap_coords(session.row_lap(row))))
    rebuild(0)
    ui.timer(0.2, initial_load, once=True)
    if sets_tab is not None:
        ui.timer(0.1, show_sets, once=True)


# --- Routes --------------------------------------------------------------------------


@ui.page("/routes")
def routes_page() -> None:
    frame = Frame("Routes")
    state = {"grouped": [], "routes": []}
    status = ui.label("").classes("ta-status")
    with ui.row().classes("w-full no-wrap gap-4"):
        with ui.column().classes("gap-2").style("flex: 1 1 45%"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                rename_input = ui.input(placeholder="New name").classes("flex-grow").props("dense outlined")
                ui.button("Rename", on_click=lambda: rename()).props("outline")
            table = grid(display.ROUTE_COLUMNS, [], height="calc(100vh - 230px)")
        with ui.column().classes("gap-2").style("flex: 1 1 55%"):
            caption = ui.html("")
            leaflet = ui.leaflet(center=(52.0, 19.0), zoom=12).classes("w-full").style("height: calc(100vh - 230px)")

    def refresh() -> None:
        state["routes"] = storage.load_routes()
        _, activities = storage.load_index()
        state["grouped"] = display.grouped_routes(state["routes"], activities)
        table.options["rowData"] = grid_rows([display.route_row(r, n) for r, n in state["grouped"]])
        table.update()
        status.set_text(f"{len(state['grouped'])} routes with 2+ activities — click one to see it on the map, double-click to list its activities")

    async def show(row: int) -> None:
        if not 0 <= row < len(state["grouped"]):
            return
        route, count = state["grouped"][row]
        caption.set_content(
            f"<b>{route.name}</b> <span class='ta-status'>· {route.sport} · {route.place or 'unknown place'} · "
            f"{format_distance(route.signature.distance_m)} · {count} activities</span>"
        )
        points = [[lat, lon] for lat, lon in route.signature.points]
        await draw_track(leaflet, track_payload(points, [None] * len(points)))
        # The stored signature is a coarse outline; swap in the latest activity's full track.
        _, activities = storage.load_index()
        latest = max((a for a in activities if a.route_id == route.route_id), key=lambda a: a.start_time_local, default=None)
        if latest is None or not storage.has_fit(latest.activity_id):
            return
        records = await run.io_bound(fit_parser.load_records, latest.activity_id, storage.fit_path(latest.activity_id).read_bytes())
        track = [[r.lat, r.lon] for r in records if r.lat is not None and r.lon is not None]
        if len(track) > len(points) and await selected_row(table) == row:
            await draw_track(leaflet, await run.io_bound(track_payload, track, [None] * len(track)))

    async def rename() -> None:
        row = await selected_row(table)
        if row is None:
            return
        name = (rename_input.value or "").strip()
        if not name:
            notify("Enter a new name", "warning")
            return
        state["grouped"][row][0].name = name
        storage.save_routes(state["routes"])
        rename_input.set_value("")
        refresh()

    on_row_change(table, show)
    table.on("rowDoubleClicked", lambda e: ui.navigate.to(f"/routes/{state['grouped'][e.args['data']['_row']][0].route_id}"))
    frame.bind_back()
    frame.bind("e", "Rename route", rename)
    refresh()
    ui.timer(0.8, lambda: select_row(table, 0) if state["grouped"] else None, once=True)  # show the top route on the map


@ui.page("/routes/{route_id}")
def route_activities_page(route_id: str) -> None:
    route = next((r for r in storage.load_routes() if r.route_id == route_id), None)
    frame = Frame(f"Route: {route.name}" if route else "Route not found")
    frame.bind_back()
    if route is None:
        return
    _, activities = storage.load_index()
    matching = sorted((a for a in activities if a.route_id == route_id), key=lambda a: a.start_time_local, reverse=True)
    ui.label(f"Route: {route.name}  |  {len(matching)} activities").classes("ta-status")
    table = grid(display.ACTIVITY_COLUMNS, grid_rows([display.activity_row(a, route.name) for a in matching]), height="calc(100vh - 180px)")
    table.on("rowDoubleClicked", lambda e: ui.navigate.to(f"/activity/{matching[e.args['data']['_row']].activity_id}"))


# --- Workouts ------------------------------------------------------------------------


@ui.page("/workouts")
def workouts_page() -> None:
    frame = Frame("Workouts")
    library = WorkoutLibrary()
    status = ui.label("").classes("ta-status")
    table = grid(workout_library.COLUMNS, [], height="45vh", row_style=(
        f"params => params.data._kind === 'week' ? {{fontWeight: 600, color: '{COLORS['accent']}'}}"
        f" : params.data._kind === 'invalid' ? {{color: '{COLORS['red']}'}} : {{}}"
    ))
    with ui.card().classes("ta-card w-full p-4"):
        preview = ui.html("").classes("ta-pre")

    def fill(select_key: str | None = None) -> None:
        rows = library.rows
        table.options["rowData"] = grid_rows([r.values for r in rows], [{"_kind": r.kind, "_key": r.key} for r in rows])
        table.update()
        keys = [r.key for r in rows]
        if select_key in keys:
            select_row(table, keys.index(select_key))
        status.set_text(library.status_text)

    def refresh() -> None:
        for message in library.refresh():
            notify(message, "information" if message.startswith("Unpacked") else "error")
        fill()

    async def selected_key() -> str | None:
        row = await table.get_selected_row()
        return row["_key"] if row else None

    def show_preview(key: str | None) -> None:
        text, is_error = library.preview(key)
        color = COLORS["red"] if is_error else COLORS["fg"]
        preview.set_content(f"<span style='color:{color}'>{escape(text)}</span>")

    def toggle(key: str | None) -> None:
        if key is None or not library.toggle_week(key):
            notify("Not part of a week plan", "warning")
            return
        fill(f"{workout_library.WEEK_KEY_PREFIX}{library.week_of(key)}")

    async def upload(items) -> None:
        if State.client is None:
            notify("Not logged in to Garmin", "warning")
            return
        notes = await run_with_progress(status, workout_library.upload_many, State.client, items)
        for severity, message in notes:
            notify(message, severity)
        refresh()

    async def upload_one() -> None:
        try:
            items, warning = library.single_upload(await selected_key())
        except LibraryError as e:
            notify(str(e), e.severity)
            return
        if warning:
            notify(warning, "warning")
        await upload(items)

    async def upload_week() -> None:
        try:
            items, prompt = library.week_upload(await selected_key())
        except LibraryError as e:
            notify(str(e), e.severity)
            return
        with ui.dialog() as dialog, ui.card().classes("ta-card p-6"):
            ui.html(escape(prompt)).classes("ta-pre")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("Cancel (n)", on_click=lambda: dialog.submit(False)).props("outline")
                ui.button("Upload (y)", on_click=lambda: dialog.submit(True))
        confirmed = await dialog
        if confirmed:
            await upload(items)

    on_row_change(table, lambda row: show_preview(library.rows[row].key if row < len(library.rows) else None))
    table.on("rowDoubleClicked", lambda e: toggle(e.args["data"]["_key"]) if e.args["data"]["_kind"] == "week" else None)

    async def toggle_selected() -> None:
        toggle(await selected_key())

    frame.bind_back()
    frame.bind("u", "Upload workout", upload_one)
    frame.bind("U", "Upload week", upload_week)
    frame.bind("x", "Expand/collapse week", toggle_selected)
    frame.bind("r", "Refresh", refresh)
    refresh()
    show_preview(None)


# --- Training week -------------------------------------------------------------------


@ui.page("/calendar")
def calendar_page() -> None:
    frame = Frame("Calendar & training load")
    state = {"week": training_load.week_start(date.today()), "view": None}
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        ui.button("◀ Previous ([)", on_click=lambda: change(-7)).props("outline")
        week_title = ui.label("").classes("text-lg font-semibold flex-grow text-center")
        ui.button("Next (]) ▶", on_click=lambda: change(7)).props("outline")
        ui.button("This week (t)", on_click=lambda: this_week()).props("outline")
        ui.button("Refresh (r)", on_click=lambda: refresh()).props("outline")
    tiles_area = ui.column().classes("w-full")
    bar = ui.html("").classes("w-full")
    thresholds = ui.label("").classes("ta-status")
    table = grid(display.WEEK_COLUMNS, [], height="26vh")
    notes = ui.label("").classes("ta-notes")
    chart = ui.echart({**ECHART_BASE}).classes("w-full").style("height: 30vh")

    def load(keep_row: int | None = None) -> None:
        view = services.load_training_week(state["week"])
        state["view"] = view
        today = date.today()
        week_title.set_text(display.week_title(state["week"]))
        tiles_area.clear()
        with tiles_area:
            stat_tiles(display.week_load_stats(view))
            stat_tiles(display.week_metric_stats(view.load_metrics))
        parts = display.week_tss_parts(view)
        total = sum(parts.values()) or 1
        segments = "".join(
            f"<div style='width:{value / total * 100:.2f}%; background:{color}'>{name} {value}</div>"
            for name, value, color in (("done", parts["done"], COLORS["green"]), ("remaining", parts["remaining"], COLORS["accent"]), ("missed", parts["missed"], COLORS["red"]))
            if value
        )
        missed = f" · {parts['missed']} missed" if parts["missed"] else ""
        bar.set_content(
            f"<div class='ta-status' style='margin-bottom:4px'>Week TSS — {parts['done']} done + {parts['remaining']} remaining = "
            f"{parts['done'] + parts['remaining']}{missed}</div><div class='ta-bar'>{segments}</div>"
        )
        thresholds.set_text(display.format_thresholds(view.thresholds))
        rows, extras = [], []
        for item in view.visible_items:
            cells, color = display.week_item_cells(item, today)
            rows.append(cells)
            extras.append({"_color": COLORS.get(color, COLORS["fg"]) if color else COLORS["fg"],
                           "_sport_color": SPORT_COLORS.get(item.sport, COLORS["fg"]), "_today": item.day == today})
        table.options["rowData"] = grid_rows(rows, extras)
        table.options["columnDefs"][2][":cellStyle"] = "params => ({color: params.data._color})"
        table.options["columnDefs"][3][":cellStyle"] = "params => ({color: params.data._sport_color})"
        table.options[":getRowStyle"] = "params => params.data._today ? {fontWeight: 600} : {}"
        table.update()
        if keep_row is not None and rows:
            select_row(table, min(keep_row, len(rows) - 1))
        note_lines = list(view.notes)
        if not view.calendar_cached:
            note_lines.insert(0, "This week's calendar isn't downloaded yet — refreshing...")
        notes.set_text("\n".join(note_lines))
        points = view.pmc
        days = [p.day.isoformat() for p in points]
        split = next((i for i, p in enumerate(points) if p.day > today), len(points))

        def series(name, values, color):
            actual = [round(v, 1) if i < split else None for i, v in enumerate(values)]
            planned = [round(v, 1) if i >= split - 1 else None for i, v in enumerate(values)] if split < len(points) else []
            out = [{"type": "line", "name": name, "data": actual, "showSymbol": False, "lineStyle": {"width": 2.2}, "color": color}]
            if planned:
                out.append({"type": "line", "name": name, "data": planned, "showSymbol": False, "lineStyle": {"width": 2, "type": "dashed"}, "color": color})
            return out

        all_series = (
            series("CTL fitness", [p.ctl for p in points], COLORS["accent"])
            + series("ATL fatigue", [p.atl for p in points], COLORS["magenta"])
            + series("TSB form", [p.tsb for p in points], COLORS["yellow"])
        )
        all_series[0]["markLine"] = {"symbol": "none", "label": {"formatter": "today", "color": COLORS["comment"]},
                                     "lineStyle": {"color": COLORS["comment"], "type": "dashed"}, "data": [{"xAxis": today.isoformat()}]}
        chart.options.clear()
        chart.options.update({
            **ECHART_BASE,
            "title": {"text": "Fitness (CTL) / fatigue (ATL) / form (TSB) — dashed: planned", "left": "center", "textStyle": {"color": COLORS["fg"], "fontSize": 13}},
            "legend": {"data": ["CTL fitness", "ATL fatigue", "TSB form"], "bottom": 0, "textStyle": {"color": COLORS["fg"]}},
            "xAxis": axis_style(type="category", data=days, axisLabel={"color": COLORS["comment"], "formatter": "{value}"}),
            "yAxis": axis_style(type="value"),
            "series": all_series,
        })
        chart.update()

    async def refresh() -> None:
        if State.client is None:
            notify("Not logged in to Garmin — showing cached data", "warning")
            return
        try:
            warnings = await run_with_progress(week_title, services.refresh_training_data, State.client, state["week"])
        except Exception as e:
            notify(f"Refresh failed: {e}", "error")
            warnings = []
        for message in warnings:
            notify(message, "warning")
        load()

    async def change(days: int) -> None:
        state["week"] += timedelta(days=days)
        load()
        if not state["view"].calendar_cached:
            await refresh()

    async def this_week() -> None:
        state["week"] = training_load.week_start(date.today())
        load()

    async def selected_item() -> training_load.WeekItem | None:
        row = await selected_row(table)
        items = state["view"].visible_items
        return items[row] if row is not None and row < len(items) else None

    async def dialog_result(build) -> object:
        """Show a dialog built by `build(dialog)`; single-key shortcuts pause while it's open."""
        state["dialog"] = True
        with ui.dialog() as dialog, ui.card().classes("ta-card p-6 gap-2").style("min-width: 480px"):
            build(dialog)
        try:
            return await dialog
        finally:
            state["dialog"] = False
            dialog.delete()

    async def choose(prompt: str, options: list[str]) -> int | None:
        def build(dialog) -> None:
            ui.label(prompt).classes("font-semibold")
            for n, text in enumerate(options):
                ui.button(text, on_click=lambda n=n: dialog.submit(n)).props("flat no-caps align=left").classes("w-full")
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("outline")
        return await dialog_result(build)

    async def confirm(prompt: str, label: str) -> bool:
        def build(dialog) -> None:
            ui.html(escape(prompt)).classes("ta-pre")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("outline")
                ui.button(label, on_click=lambda: dialog.submit(True))
        return bool(await dialog_result(build))

    async def garmin_change(operation) -> None:
        row = await selected_row(table)
        try:
            notify(await run.io_bound(operation))
        except services.CalendarError as e:
            notify(str(e), "warning")
        except Exception as e:  # network/auth trouble: Garmin is unchanged or partly changed
            notify(f"Garmin change failed: {e}", "error")
        load(row)

    async def local_change(operation) -> None:
        row = await selected_row(table)
        try:
            notify(operation())
        except services.CalendarError as e:
            notify(str(e), "warning")
        load(row)

    def unless_dialog(action):
        async def run_action() -> None:
            if not state.get("dialog"):
                await action()
        return run_action

    async def move_workout() -> None:
        item = await selected_item()
        try:
            days = services.move_targets(item)
        except services.CalendarError as e:
            notify(str(e), "warning")
            return
        if State.client is None:
            notify("Not logged in to Garmin", "warning")
            return
        index = await choose(services.move_prompt(item), [display.week_day_choice(d, state["view"].week.items) for d in days])
        if index is not None:
            await garmin_change(lambda: services.move_planned(State.client, item, days[index]))

    async def cancel_workout() -> None:
        item = await selected_item()
        try:
            prompt = services.cancel_prompt(item)
        except services.CalendarError as e:
            notify(str(e), "warning")
            return
        if State.client is None:
            notify("Not logged in to Garmin", "warning")
            return
        if await confirm(prompt, "Remove from calendar"):
            await garmin_change(lambda: services.cancel_planned(State.client, item))

    async def link() -> None:
        item = await selected_item()
        if item is not None and item.status == "done (planned)":
            try:
                prompt = services.unlink_prompt(item)
            except services.CalendarError as e:
                notify(str(e), "warning")
                return
            if await confirm(prompt, "Unlink"):
                await local_change(lambda: services.unlink(item))
            return
        try:
            candidates = services.link_candidates(state["view"].week, item)
        except services.CalendarError as e:
            notify(str(e), "warning")
            return
        index = await choose(services.link_prompt(item), [display.week_item_choice(c) for c in candidates])
        if index is not None:
            await local_change(lambda: services.link(item, candidates[index]))

    async def export() -> None:
        index = await choose(training_export.EXPORT_PROMPT, training_export.choice_labels(date.today()))
        if index is None:
            return
        try:
            path, warnings = await run_with_progress(week_title, services.export_training, State.client, training_export.CHOICES[index])
        except Exception as e:
            notify(f"Export failed: {e}", "error")
            load()
            return
        for message in warnings:
            notify(message, "warning")
        notify(f"Exported to {path}")
        ui.download(path)  # a copy in the browser's downloads too
        load()

    frame.bind_back()
    frame.bind("[", "Prev week", lambda: change(-7))
    frame.bind("]", "Next week", lambda: change(7))
    frame.bind("t", "This week", this_week)
    frame.bind("r", "Refresh from Garmin", refresh)
    frame.bind("m", "Move workout", unless_dialog(move_workout))
    frame.bind("x", "Cancel workout", unless_dialog(cancel_workout))
    frame.bind("l", "Link/unlink plan", unless_dialog(link))
    frame.bind("e", "Export weeks", unless_dialog(export))
    frame.bind("y", "Copy table", lambda: copy(display.week_summary_text(state["view"]) + "\n\n" + display.tsv(
        display.WEEK_COLUMNS, [display.week_item_cells(i, date.today())[0] for i in state["view"].visible_items]), "week"))
    load()
    ui.timer(0.1, refresh, once=True)


# --- Startup -------------------------------------------------------------------------


async def _cached_login() -> None:
    State.client = await run.io_bound(garmin_client.try_cached_login)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Training Analyze in the browser")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()
    app.on_startup(_cached_login)
    ui.run(host="127.0.0.1", port=args.port, title=APP_TITLE, dark=True, reload=False, show=not args.no_browser,
           favicon="🏃", show_welcome_message=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
