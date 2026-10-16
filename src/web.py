"""Browser GUI (NiceGUI): the TUI's screens with real web graphics — AG Grid tables, ECharts
charts, Leaflet maps, stat tiles — and the same single-key shortcuts, shown in a clickable
footer. All behaviour comes from the shared modules (services, laps_session,
workout_library, display); this file is only presentation.

Served on 127.0.0.1 only: the app holds a logged-in Garmin session, so it must not be
reachable from the network. Map tiles are loaded by the browser straight from
OpenStreetMap (with the attribution Leaflet shows).
"""

import argparse
import inspect
import json
from collections.abc import Callable
from datetime import date, timedelta

from nicegui import app, run, ui

import display
import fit_parser
import garmin_client
import services
import storage
import training_load
import workout_library
from theme import COLORS, SERIES_COLORS, SPORT_COLORS
from laps_session import LapsError, LapsSession
from models import PACE_SPORTS, SWIM_SPORTS, format_distance, format_speed_or_pace
from workout_library import LibraryError, WorkoutLibrary

APP_TITLE = "Training Analyze"
ALL_SPORTS = "__all__"


class State:
    client = None  # logged-in Garmin client, shared by all browser tabs (single-user app)


CSS = f"""
body {{ background: {COLORS['bg']}; color: {COLORS['fg']}; }}
.q-header {{ background: {COLORS['bg_dark']} !important; border-bottom: 1px solid {COLORS['border']}; }}
.q-footer {{ background: {COLORS['bg_dark']} !important; border-top: 1px solid {COLORS['border']}; }}
.ta-title {{ color: {COLORS['accent']}; font-weight: 600; font-size: 1.15rem; }}
.ta-status {{ color: {COLORS['comment']}; }}
.ta-notes {{ color: {COLORS['yellow']}; white-space: pre-line; }}
.ta-card {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 12px; }}
.ta-tile {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 12px;
            padding: 10px 16px; min-width: 120px; flex: 1; }}
.ta-tile .cap {{ color: {COLORS['comment']}; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }}
.ta-tile .val {{ font-size: 1.7rem; font-weight: 600; line-height: 1.2; }}
.ta-tile .unit {{ color: {COLORS['comment']}; font-size: 0.85rem; margin-left: 4px; }}
.ta-tile .hint {{ color: {COLORS['comment']}; font-size: 0.75rem; }}
.ta-chip {{ color: {COLORS['comment']} !important; }}
.ta-chip .cap {{ background: {COLORS['surface2']}; color: {COLORS['accent']}; font-weight: 600; border-radius: 4px;
                 padding: 1px 7px; margin-right: 6px; }}
.ta-pre {{ font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace; font-size: 0.85rem; white-space: pre-wrap; }}
.ta-bar {{ display: flex; height: 28px; border-radius: 8px; overflow: hidden; background: {COLORS['surface']}; }}
.ta-bar div {{ display: flex; align-items: center; justify-content: center; color: {COLORS['bg']};
               font-weight: 600; font-size: 0.85rem; white-space: nowrap; overflow: hidden; }}
.leaflet-tile-pane {{ filter: brightness(0.82) saturate(0.85); }}
.leaflet-tooltip.lap-label {{ background: {COLORS['fg']}; color: {COLORS['bg_dark']}; border: none; border-radius: 9px;
              font-weight: 700; font-size: 10px; padding: 0 5px; box-shadow: none; }}
[data-ag-theme-mode="dark"] {{
    --ag-background-color: {COLORS['bg_dark']}; --ag-odd-row-background-color: {COLORS['surface']};
    --ag-header-background-color: {COLORS['surface2']}; --ag-header-text-color: {COLORS['accent']};
    --ag-foreground-color: {COLORS['fg']}; --ag-border-color: {COLORS['border']};
    --ag-row-hover-color: #2f3549; --ag-selected-row-background-color: {COLORS['selection']};
}}
"""


# --- Page frame: header, footer key chips, keyboard ---------------------------------


class Frame:
    """The common page frame. `bind()` registers a TUI-style single-key binding: it's shown as
    a clickable chip in the footer and triggered by the key itself."""

    def __init__(self, title: str) -> None:
        ui.dark_mode(True)
        ui.colors(primary=COLORS["accent"], positive=COLORS["green"], negative=COLORS["red"], warning=COLORS["yellow"])
        ui.add_css(CSS)
        ui.page_title(f"{title} · {APP_TITLE}")
        with ui.header().classes("items-center px-4 py-2"):
            ui.label(APP_TITLE).classes("ta-title")
            ui.label("·").classes("ta-status")
            self.title = ui.label(title).classes("text-lg font-semibold")
        with ui.footer().classes("px-3 py-1"):
            self.footer = ui.row().classes("gap-1 items-center")
        self.keys: dict[str, Callable] = {}
        ui.keyboard(on_key=self._on_key)

    def bind(self, key: str, label: str, action: Callable, shown_key: str | None = None) -> None:
        self.keys[key] = action
        with self.footer:
            with ui.button(on_click=lambda: _call(action)).props("flat dense no-caps").classes("ta-chip"):
                ui.html(f"<span class='cap'>{shown_key or key}</span>{label}")

    def bind_back(self, action: Callable | None = None) -> None:
        self.bind("Escape", "Back", action or ui.navigate.back, shown_key="esc")

    async def _on_key(self, e) -> None:
        if not e.action.keydown or e.action.repeat:
            return
        action = self.keys.get(str(e.key.name))
        if action is not None:
            await _call(action)


async def _call(action: Callable) -> None:
    result = action()
    if inspect.isawaitable(result):
        await result


def notify(message: str, severity: str = "information") -> None:
    ui.notify(message, type={"information": "positive", "warning": "warning", "error": "negative"}.get(severity, "info"),
              position="bottom-right")


def stat_tiles(stats) -> None:
    with ui.row().classes("w-full gap-3 no-wrap"):
        for stat in stats:
            color = COLORS.get(stat.color, COLORS["fg"]) if stat.color else COLORS["fg"]
            hint = f"<div class='hint'>{stat.hint}</div>" if stat.hint else ""
            ui.html(
                f"<div class='cap'>{stat.label}</div>"
                f"<div><span class='val' style='color:{color}'>{stat.value}</span><span class='unit'>{stat.unit}</span></div>{hint}"
            ).classes("ta-tile")


def grid(columns: tuple[str, ...], rows: list[dict], height: str = "60vh", row_style: str | None = None, **options):
    """An AG Grid table; each row dict holds the column values plus hidden helper fields."""
    # cellDataType False: every value is already display text. Without it AG Grid infers a type
    # from the first row — e.g. "2026-10-05_Z2.json" makes a column "dateString", and every
    # value that then fails to parse as a date renders blank.
    column_defs = [
        {"field": f"c{i}", "headerName": name, "sortable": False, "resizable": True, "cellDataType": False}
        for i, name in enumerate(columns)
    ]
    opts = {
        "columnDefs": column_defs,
        "rowData": rows,
        "rowSelection": {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True},
        ":getRowId": "params => String(params.data._row)",
        "suppressCellFocus": False,
        "autoSizeStrategy": {"type": "fitCellContents"},
        **options,
    }
    if row_style:
        opts[":getRowStyle"] = row_style  # a JavaScript function (NiceGUI's ":" prefix on the option name)
    table = ui.aggrid(opts, theme="quartz").classes("w-full").style(f"height: {height}")
    # Keyboard navigation (arrow keys move the focused cell) also selects the row, so the
    # "current row" works like the TUI's cursor whether you click or use the keyboard.
    table.on("cellFocused", lambda e: table.run_row_method(str(e.args["rowIndex"]), "setSelected", True)
             if e.args.get("rowIndex") is not None else None)
    return table


def on_row_change(table, callback: Callable[[int], object]) -> None:
    """Call `callback(row)` whenever the selected row changes. AG Grid also fires a selection
    event for the row being *de*selected, and its event no longer says which kind it is, so
    ask the grid for the current selection instead of trusting the event's row."""
    state = {"row": None}

    async def changed(_) -> None:
        row = await selected_row(table)
        if row is not None and row != state["row"]:
            state["row"] = row
            await _call(lambda: callback(row))

    table.on("rowSelected", changed)


def grid_rows(rows: list[tuple[str, ...]], extra: list[dict] | None = None) -> list[dict]:
    return [{**{f"c{i}": v for i, v in enumerate(values)}, "_row": n, **(extra[n] if extra else {})} for n, values in enumerate(rows)]


async def selected_row(table) -> int | None:
    row = await table.get_selected_row()
    return row["_row"] if row else None


def select_row(table, row: int | None) -> None:
    if row is not None:
        table.run_row_method(str(row), "setSelected", True)
        table.run_grid_method("ensureIndexVisible", row)


def tsv(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    return "\n".join(["\t".join(columns), *("\t".join(str(v).strip() for v in r) for r in rows)])


def copy(text: str, what: str) -> None:
    ui.clipboard.write(text)
    notify(f"Copied {what}")


ECHART_BASE = {
    "backgroundColor": "transparent",
    "textStyle": {"color": COLORS["fg"]},
    "grid": {"left": 50, "right": 20, "top": 40, "bottom": 40},
    "tooltip": {"trigger": "axis"},
}


def axis_style(**extra) -> dict:
    return {
        "axisLine": {"lineStyle": {"color": COLORS["border2"]}},
        "axisLabel": {"color": COLORS["comment"]},
        "splitLine": {"lineStyle": {"color": COLORS["border"]}},
        **extra,
    }


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


class Progress:
    """Collects progress messages from a worker thread; a ui.timer shows the latest one."""

    def __init__(self) -> None:
        self.text = ""

    def __call__(self, message: str) -> None:
        self.text = message


async def run_with_progress(label, job: Callable, *args):
    progress = Progress()
    timer = ui.timer(0.3, lambda: label.set_text(progress.text) if progress.text else None)
    try:
        return await run.io_bound(job, *args, progress=progress)
    finally:
        timer.cancel()


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
        _, activities = storage.load_index()
        routes_by_id = {r.route_id: r for r in storage.load_routes()}
        sports = sorted({a.sport for a in activities})
        sport_select.set_options({ALL_SPORTS: "All sports", **{s: s.title() for s in sports}}, value=state["sport"] if state["sport"] in sports else ALL_SPORTS)
        shown = sorted((a for a in activities if state["sport"] == ALL_SPORTS or a.sport == state["sport"]), key=lambda a: a.start_time_local, reverse=True)
        state["shown"] = shown
        rows = [display.activity_row(a, routes_by_id[a.route_id].name if a.route_id in routes_by_id else "–") for a in shown]
        table.options["rowData"] = grid_rows(rows, [{"_sport_color": SPORT_COLORS.get(a.sport, COLORS["fg"])} for a in shown])
        table.options["columnDefs"][2][":cellStyle"] = "params => ({color: params.data._sport_color})"
        table.update()
        status.set_text(f"{len(shown)} activities synced" if len(shown) == len(activities) else f"{len(shown)} of {len(activities)} activities")

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

    sync_button.on_click(sync)
    reprocess_button.on_click(lambda: job(services.reprocess_activities))
    frame.bind("s", "Sync", sync)
    frame.bind("r", "Reprocess", lambda: job(services.reprocess_activities))
    frame.bind("o", "Routes", lambda: ui.navigate.to("/routes"))
    frame.bind("w", "Workouts", lambda: ui.navigate.to("/workouts"))
    frame.bind("c", "Calendar & load", lambda: ui.navigate.to("/calendar"))
    frame.bind("Enter", "Open", open_selected, shown_key="enter")
    frame.bind("y", "Copy table", lambda: copy(tsv(display.ACTIVITY_COLUMNS, [display.activity_row(a, "") for a in state["shown"]]), f"{len(state['shown'])} activities"))
    render()


# --- Laps (with map / phases / compare / zones tabs) --------------------------------

LAP_ROW_STYLE = (
    "params => params.data._kind === 'interval' ? {fontWeight: 600}"
    f" : params.data._kind === 'phase' ? {{color: '{COLORS['comment']}', fontStyle: 'italic'}} : {{}}"
)
MAP_MODES = (("1", "Speed / pace", "speed_mps"), ("2", "Heart rate", "heart_rate"), ("3", "Power", "power"), ("4", "Elevation", "altitude_m"))
RAMP = ["#7aa2f7", "#7dcfff", "#9ece6a", "#e0af68", "#f7768e"]
COLOR_BUCKETS = 16


def _ramp(t: float) -> str:
    t = min(max(t, 0.0), 1.0) * (len(RAMP) - 1)
    i = min(int(t), len(RAMP) - 2)
    f = t - i
    a, b = RAMP[i], RAMP[i + 1]
    channels = [round(int(a[k:k + 2], 16) + (int(b[k:k + 2], 16) - int(a[k:k + 2], 16)) * f) for k in (1, 3, 5)]
    return "#" + "".join(f"{c:02x}" for c in channels)


MAX_TRACK_POINTS = 8000  # Leaflet simplifies per zoom itself; this just bounds the payload


def track_payload(points, values, highlights=(), selected=(), markers=()) -> dict:
    """Everything needed to draw a track, as plain data: the casing line, one multi-polyline
    per colour bucket, outlines, lap markers and the colour range. Pure Python — run it off
    the event loop for long activities."""
    step = max(1, len(points) // MAX_TRACK_POINTS)
    points, values = points[::step], list(values)[::step]
    present = sorted(v for v in values if v is not None)
    value_range = None
    buckets: dict[int, list] = {}
    if present:
        low, high = present[len(present) // 20], present[-1 - len(present) // 20]
        high = high if high > low else low + 1
        value_range = (low, high)
        run, run_bucket = [points[0]], None
        for point, value in zip(points[1:], values[1:]):
            bucket = run_bucket if value is None else min(COLOR_BUCKETS - 1, max(0, int((value - low) / (high - low) * COLOR_BUCKETS)))
            if run_bucket is not None and bucket != run_bucket:
                buckets.setdefault(run_bucket, []).append(run)
                run = [run[-1]]
            run.append(point)
            run_bucket = bucket
        if run_bucket is not None and len(run) > 1:
            buckets.setdefault(run_bucket, []).append(run)

    def thin(path):
        return path[::max(1, len(path) // 1500)]

    return {
        "casing": points,
        "colored": [{"color": _ramp((b + 0.5) / COLOR_BUCKETS), "lines": lines} for b, lines in buckets.items()],
        "plain_color": COLORS["accent"],
        "highlights": [thin(h) for h in highlights if len(h) > 1],
        "selected": thin(selected) if len(selected) > 1 else [],
        "markers": [[lat, lon, text] for lat, lon, text in markers],
        "range": value_range,
        "colors": {"casing": COLORS["bg_dark"], "highlight": "#ffffff", "selected": COLORS["cyan"],
                   "marker": COLORS["fg"], "start": COLORS["green"], "end": COLORS["red"]},
    }


DRAW_TRACK_JS = """
(() => {
  const el = getElement(%(id)s); const map = el.map; const d = %(data)s;
  if (el._track) { el._track.remove(); map.off('zoomend', el._relayout); }
  const group = L.layerGroup().addTo(map); el._track = group;
  const line = (latlngs, color, weight, opacity) => L.polyline(latlngs, {color, weight, opacity, interactive: false}).addTo(group);
  if (d.selected.length) line(d.selected, d.colors.selected, 13, 0.6);
  for (const h of d.highlights) line(h, d.colors.highlight, 10, 0.6);
  line(d.casing, d.colors.casing, 7, 0.9);
  if (d.colored.length) { for (const c of d.colored) line(c.lines, c.color, 4, 1); }
  else line(d.casing, d.plain_color, 4, 1);
  const marks = L.layerGroup().addTo(group);
  // Lap markers that would overlap at the current zoom are skipped; zooming in reveals them.
  el._relayout = () => {
    marks.clearLayers(); let last = null;
    for (const [lat, lon, text] of d.markers) {
      const p = map.latLngToLayerPoint([lat, lon]);
      if (last && p.distanceTo(last) < 24) continue;
      last = p;
      L.circleMarker([lat, lon], {radius: 8, color: d.colors.casing, fillColor: d.colors.marker, fillOpacity: 1, weight: 1})
        .bindTooltip(text, {permanent: true, direction: 'center', className: 'lap-label'}).addTo(marks);
    }
  };
  map.on('zoomend', el._relayout);
  const first = d.casing[0], end = d.casing[d.casing.length - 1];
  for (const [p, color] of [[first, d.colors.start], [end, d.colors.end]])
    L.circleMarker(p, {radius: 7, color: '#ffffff', fillColor: color, fillOpacity: 1, weight: 2.5}).addTo(group);
  map.fitBounds(L.polyline(d.casing).getBounds(), {padding: [30, 30]});
  el._relayout();
})()
"""


async def draw_track(leaflet, payload: dict) -> None:
    """Draw a track payload (see track_payload) on a ui.leaflet in a single round trip —
    one message, instead of one per polyline/marker/tooltip."""
    await leaflet.initialized()
    ui.run_javascript(DRAW_TRACK_JS % {"id": leaflet.id, "data": json.dumps(payload)})


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
    with ui.tab_panels(tabs, value=laps_tab).classes("w-full").style("background: transparent"):
        with ui.tab_panel(laps_tab).classes("p-0 gap-2"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                split_input = ui.input(placeholder="400 = metres · 60s / 1m / 1:30 = time").classes("flex-grow").props("dense outlined")
                ui.button("Split", on_click=lambda: run_op(lambda: session.split(split_input.value or ""))).props("outline")
                ui.button("Split intervals (i)", on_click=lambda: split_intervals()).props("outline")
                filter_button = ui.button("Interval only (f)", on_click=lambda: toggle_filter()).props("outline")
                ui.button("Reset", on_click=lambda: run_op(session.reset)).props("outline")
            table = grid(session.columns, [], height="calc(100vh - 470px)", row_style=LAP_ROW_STYLE)
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

    def rebuild(keep_row: int | None = None) -> None:
        state["rows"] = session.rows()
        table.options["rowData"] = grid_rows([r.values for r in state["rows"]], [{"_kind": r.kind} for r in state["rows"]])
        table.options["columnDefs"][session.duration_column]["cellStyle"] = {"textAlign": "right"}
        table.update()
        select_row(table, keep_row)
        interval_area.clear()
        stats = display.interval_stats(session.current_laps, activity.sport)
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

        def build() -> dict | None:
            # FIT load (first time only, then cached) and per-lap slicing can take seconds on
            # a long activity — done in a worker thread so the server stays responsive.
            records = [r for r in session.ensure_records() if r.lat is not None and r.lon is not None]
            if len(records) < 2:
                return None

            def coords(lap):
                return [[r.lat, r.lon] for r in fit_parser.lap_record_slice(session.records, lap, session.utc_offset) if r.lat is not None]

            return track_payload(
                [[r.lat, r.lon] for r in records], [getattr(r, mode) for r in records],
                highlights=[coords(lap) for lap in session.marked_laps()],
                selected=coords(selected_lap) if selected_lap else [],
                markers=[(*c[0], str(lap.lap_index)) for lap in session.current_laps if (c := coords(lap))],
            )

        try:
            payload = await run.io_bound(build)
        except LapsError as e:
            notify(str(e), "warning")
            return
        if payload is None:
            map_legend.set_content("<span class='ta-status'>No GPS track for this activity</span>")
            return
        await draw_track(leaflet, payload)
        value_range = payload["range"]
        map_legend.set_content("")
        if value_range:
            fmt = {
                "speed_mps": lambda v: format_speed_or_pace(activity.sport, v),
                "heart_rate": lambda v: f"{v:.0f} bpm", "power": lambda v: f"{v:.0f} W", "altitude_m": lambda v: f"{v:.0f} m",
            }[mode]
            name = next(text for _, text, a in MAP_MODES if a == mode)
            map_legend.set_content(
                f"<div class='ta-tile' style='width:340px'><div class='cap'>{name}</div>"
                f"<div style='height:10px; border-radius:5px; background: linear-gradient(90deg, {', '.join(RAMP)})'></div>"
                f"<div style='display:flex; justify-content:space-between' class='hint'><span>{fmt(value_range[0])}</span><span>{fmt(value_range[1])}</span></div></div>"
            )

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
                    "xAxis": axis_style(type="category", data=[label for label, _ in rows]),
                    "yAxis": axis_style(type="value", minInterval=1, name="Phases"),
                    "series": [{"type": "bar", "data": [count for _, count in rows], "itemStyle": {"color": color, "borderRadius": [4, 4, 0, 0]}}],
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

    async def go_back() -> None:
        if tabs.value != laps_tab and tabs.value != "Laps":
            tabs.set_value(laps_tab)
        else:
            ui.navigate.back()

    frame.bind_back(go_back)
    frame.bind("m", "Mark interval", toggle_interval)
    frame.bind("z", "Zones", show_zones)
    frame.bind("c", "Compare intervals", draw_compare)
    frame.bind("p", "Physical laps", physical_laps)
    frame.bind("i", "Split intervals", split_intervals)
    frame.bind("f", "Interval-only", toggle_filter)
    frame.bind("h", "Phase time/pace/power", show_phases)
    frame.bind("g", "Map", draw_map)
    for key, text, attr in MAP_MODES:
        frame.keys[key] = lambda a=attr: draw_map(a) if tabs.value in (map_tab, "Map (g)") else None
    frame.bind("y", "Copy table", lambda: copy(tsv(session.columns, [r.values for r in state["rows"]]), f"{len(state['rows'])} laps"))
    rebuild(0)


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
        preview.set_content(f"<span style='color:{color}'>{_escape(text)}</span>")

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
            ui.html(_escape(prompt)).classes("ta-pre")
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


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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

    def load() -> None:
        view = services.load_training_week(state["week"])
        state["view"] = view
        today = date.today()
        week_title.set_text(display.week_title(state["week"]))
        tiles_area.clear()
        with tiles_area:
            stat_tiles(display.week_load_stats(view))
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
        for item in view.week.items:
            cells, color = display.week_item_cells(item, today)
            rows.append(cells)
            extras.append({"_color": COLORS.get(color, COLORS["fg"]) if color else COLORS["fg"],
                           "_sport_color": SPORT_COLORS.get(item.sport, COLORS["fg"]), "_today": item.day == today})
        table.options["rowData"] = grid_rows(rows, extras)
        table.options["columnDefs"][2][":cellStyle"] = "params => ({color: params.data._color})"
        table.options["columnDefs"][3][":cellStyle"] = "params => ({color: params.data._sport_color})"
        table.options[":getRowStyle"] = "params => params.data._today ? {fontWeight: 600} : {}"
        table.update()
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

    frame.bind_back()
    frame.bind("[", "Prev week", lambda: change(-7))
    frame.bind("]", "Next week", lambda: change(7))
    frame.bind("t", "This week", this_week)
    frame.bind("r", "Refresh from Garmin", refresh)
    frame.bind("y", "Copy table", lambda: copy(display.week_summary_text(state["view"]) + "\n\n" + tsv(
        display.WEEK_COLUMNS, [display.week_item_cells(i, date.today())[0] for i in state["view"].week.items]), "week"))
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
