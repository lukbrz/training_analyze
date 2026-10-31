"""Shared building blocks of the web GUI (web.py): page frame with a clickable key-binding
footer, AG Grid / ECharts helpers, stat tiles, progress for background jobs, and Leaflet track
drawing. Presentation only — mirrors gui_common.py for the Qt GUI."""

import inspect
import json
from collections.abc import Callable

from nicegui import run, ui

import display
from theme import COLORS

APP_TITLE = "Training Analyze"


class State:
    client = None  # logged-in Garmin client, shared by all browser tabs (one athlete per server)
    server = False  # --server: nightly sync + reprocess, and a sync when the app is opened
    background_message = ""  # what the background sync / nightly run is doing or last did
    data_version = 0  # bumped when a background job changed the data, so pages redraw


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


def stat_strip(groups: list[tuple[list, bool]], caption: str = ""):
    """Stats inline on one line for a dense header (see gui_common.StatStrip): a group's
    hints inline, or as each value's hover tooltip; `caption` follows the first group."""
    muted = COLORS["comment"]
    html_groups = []
    for stats, inline_hints in groups:
        items = []
        for s in stats:
            color = COLORS.get(s.color, COLORS["fg"]) if s.color else COLORS["fg"]
            unit = f"<span style='color:{muted}; font-size:0.8rem'> {escape(s.unit)}</span>" if s.unit else ""
            hint = f"<span style='color:{muted}'> {escape(s.hint)}</span>" if inline_hints and s.hint else ""
            title = f" title='{escape(s.hint)}'" if s.hint and not inline_hints else ""
            items.append(
                f"<span{title}><span style='color:{muted}'>{escape(s.label)}</span> "
                f"<b style='font-size:1.15rem; color:{color}'>{escape(s.value)}</b>{unit}{hint}</span>"
            )
        html_groups.append(" &nbsp;·&nbsp; ".join(items))
    if caption and html_groups:
        html_groups[0] += f" <span style='color:{muted}; font-size:0.75rem'>({escape(caption)})</span>"
    return ui.html(f" &nbsp;<span style='color:{COLORS['border2']}'>│</span>&nbsp; ".join(html_groups)).classes("w-full")


# Grid events are sent to the server with only these fields. Some AG Grid events (e.g.
# rowDoubleClicked) carry its internal, circular context object; sent whole, the browser
# fails to serialise them and the event never reaches the server.
ROW_EVENT_ARGS = ["rowIndex", "data"]


def row_context_menu(table, menu_for_row: Callable[[int], list], handlers: dict[str, Callable]) -> None:
    """Right-click on a grid row selects it and opens its menu (display.*_menu) at the
    cursor: every item a handler is given for, greyed out where it doesn't apply to that
    row, with its key — each runs the same function as the key."""
    # The grid renders no child elements, so the menu hangs off an invisible wrapper around
    # it (display: contents keeps the page layout as it was).
    parent = table.parent_slot.parent
    index = parent.default_slot.children.index(table)
    with parent:
        wrapper = ui.element("div").style("display: contents")
    wrapper.move(parent, target_index=index)
    table.move(wrapper)
    with wrapper:
        menu = ui.context_menu().props("touch-position")

    def fill(row: int | None) -> None:
        menu.clear()
        with menu:
            for item in menu_for_row(row) if row is not None else []:
                if item.action not in handlers:
                    continue
                with ui.menu_item(on_click=lambda fn=handlers[item.action]: _call(fn)).props("dense") as entry:
                    ui.label(item.label)
                    ui.space()
                    ui.label(item.key).classes("ta-status").style("margin-left: 24px")
                if not item.enabled:
                    entry.disable()

    def clicked(e) -> None:
        row = (e.args.get("data") or {}).get("_row", e.args.get("rowIndex"))
        select_row(table, row)
        fill(row)

    table.on("cellContextMenu", clicked, args=ROW_EVENT_ARGS)


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
             if e.args.get("rowIndex") is not None else None, args=ROW_EVENT_ARGS)
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

    table.on("rowSelected", changed, args=ROW_EVENT_ARGS)


def grid_rows(rows: list[tuple[str, ...]], extra: list[dict] | None = None) -> list[dict]:
    return [{**{f"c{i}": v for i, v in enumerate(values)}, "_row": n, **(extra[n] if extra else {})} for n, values in enumerate(rows)]


async def selected_row(table) -> int | None:
    row = await table.get_selected_row()
    return row["_row"] if row else None


def select_row(table, row: int | None) -> None:
    if row is not None:
        table.run_row_method(str(row), "setSelected", True)
        table.run_grid_method("ensureIndexVisible", row)


def copy(text: str, what: str) -> None:
    # navigator.clipboard only exists on https or localhost; over a VPN to http://<server>
    # fall back to the old select-and-copy way, which browsers still allow there.
    ui.run_javascript(f"""
        const text = {json.dumps(text)};
        if (window.isSecureContext && navigator.clipboard) {{
            navigator.clipboard.writeText(text);
        }} else {{
            const area = document.createElement('textarea');
            area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
            document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
        }}
    """)
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
    the event loop for long activities. (laps_session.MapLayers fits straight in: see
    layers_payload.)"""
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
  const selectedGroup = L.layerGroup().addTo(group);
  // Swapping the highlighted lap only touches this layer (see set_selected()).
  el._setSelected = (latlngs) => {
    selectedGroup.clearLayers();
    if (latlngs.length > 1) L.polyline(latlngs, {color: d.colors.selected, weight: 13, opacity: 0.6, interactive: false}).addTo(selectedGroup);
  };
  el._setSelected(d.selected);
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


def layers_payload(layers) -> dict:
    return track_payload(layers.points, layers.values, layers.highlights, layers.selected, layers.markers)


async def draw_track(leaflet, payload: dict, fit: bool = True) -> None:
    """Draw a track payload (see track_payload) on a ui.leaflet in a single round trip —
    one message, instead of one per polyline/marker/tooltip. Uses the element's own client,
    so it also works from background tasks."""
    await leaflet.initialized()
    js = DRAW_TRACK_JS % {"id": leaflet.id, "data": json.dumps(payload)}
    if not fit:
        js = js.replace("map.fitBounds(", "if (!el._fitted) map.fitBounds(")
    leaflet.client.run_javascript(js + f"; getElement({leaflet.id})._fitted = true;")


def set_selected(leaflet, points) -> None:
    """Highlight one lap on a map already drawn with draw_track."""
    leaflet.client.run_javascript(
        f"(() => {{ const el = getElement({leaflet.id}); if (el._setSelected) el._setSelected({json.dumps([list(p) for p in points])}); }})()"
    )


def legend_html(metric: str, sport: str, value_range) -> str:
    if not value_range:
        return ""
    name, fmt = display.metric_legend(metric, sport)
    return (
        f"<div class='ta-tile' style='width:340px'><div class='cap'>{name}</div>"
        f"<div style='height:10px; border-radius:5px; background: linear-gradient(90deg, {', '.join(RAMP)})'></div>"
        f"<div style='display:flex; justify-content:space-between' class='hint'><span>{fmt(value_range[0])}</span>"
        f"<span>{fmt(value_range[1])}</span></div></div>"
    )


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
