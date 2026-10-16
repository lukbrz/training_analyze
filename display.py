"""Presentation helpers shared by every front end: they turn models into the exact strings
and values the screens show (table rows, summary lines, histogram bins), so the Textual TUI
and the GUIs render the same content. Plain Python — no UI toolkit imports here."""

import math
from collections import Counter
from datetime import date, timedelta

import fit_parser
import services
import training_load
from models import (
    SWIM_SPORTS,
    ActivitySummary,
    Lap,
    format_distance,
    format_duration,
    format_duration_tenths,
    format_elevation,
    format_hr,
    format_lap_distance,
    format_power,
    format_speed_or_pace,
)

# --- Activities -----------------------------------------------------------------

ACTIVITY_COLUMNS = (
    "Date", "Activity", "Sport", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Route"
)
ACTIVITY_DURATION_COLUMN = 3


def activity_row(activity: ActivitySummary, route_name: str) -> tuple[str, ...]:
    return (
        activity.start_time_local.replace("T", " ")[:16],
        activity.name,
        activity.sport,
        format_duration(activity.duration_s),
        format_distance(activity.distance_m),
        format_speed_or_pace(activity.sport, activity.avg_speed_mps),
        format_hr(activity.avg_hr),
        format_power(activity.avg_power),
        route_name,
    )


LEG_NAMES = {"swimming": "Swim", "cycling": "Bike", "running": "Run", "transition": "Trans"}


def common_sport(laps: list[Lap], fallback: str) -> str:
    """The sport all `laps` share (multisport laps carry their own), else `fallback`."""
    sports = {lap.sport for lap in laps}
    return sports.pop() if len(sports) == 1 and None not in sports else fallback


def legs(laps: list[Lap]) -> list[tuple[str, Lap]]:
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

    result = []
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
        result.append((name, Lap(
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
    return result


def activity_summary_text(activity: ActivitySummary, device_laps: list[Lap]) -> str:
    """The laps screen's header: totals, plus one line per leg for multisport."""
    a = activity
    text = (
        f"{a.name}  |  {a.start_time_local}  |  {a.sport}\n"
        f"Duration: {format_duration(a.duration_s)}   "
        f"Distance: {format_distance(a.distance_m)}   "
        f"Avg: {format_speed_or_pace(a.sport, a.avg_speed_mps)}   "
        f"Avg HR: {format_hr(a.avg_hr)}   Avg Power: {format_power(a.avg_power)}"
    )
    if a.sport == fit_parser.MULTISPORT:
        for name, leg in legs(device_laps):
            distance = (
                format_lap_distance(leg.sport, leg.distance_m) if leg.sport in SWIM_SPORTS
                else format_distance(leg.distance_m)
            )
            text += (
                f"\n  {name:<5} {format_duration(leg.duration_s):>8}  {distance:>9}  "
                f"{format_speed_or_pace(leg.sport, leg.avg_speed_mps):>12}  "
                f"HR {format_hr(leg.avg_hr):>3}  {format_power(leg.avg_power):>6}  "
                f"↑ {format_elevation(leg.total_ascent_m)}"
            )
    return text


# --- Laps -----------------------------------------------------------------------

LAP_COLUMNS = (
    "#", "Start", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Ascent", "Descent", "Int"
)


def lap_columns(multisport: bool) -> tuple[str, ...]:
    return LAP_COLUMNS[:1] + ("Sport",) + LAP_COLUMNS[1:] if multisport else LAP_COLUMNS


def lap_duration_column(multisport: bool) -> int:
    return lap_columns(multisport).index("Duration")


def lap_row(lap: Lap, index_label: str, activity_sport: str, multisport: bool, precise: bool) -> tuple[str, ...]:
    """One laps-table row. Interval laps and their phase splits (`precise`) get tenths of a
    second — it's the only way a rep's phase durations visibly sum back to the rep's own
    duration (see fit_parser._interpolate_at_distance/_interpolate_at_time)."""
    sport = lap.sport or activity_sport
    duration = format_duration_tenths(lap.duration_s) if precise else format_duration(lap.duration_s)
    sport_cells = (LEG_NAMES.get(sport, sport),) if multisport else ()
    return (
        index_label,
        *sport_cells,
        lap.start_time_local[11:19] if lap.start_time_local else "–",
        duration,
        format_lap_distance(sport, lap.distance_m),
        format_speed_or_pace(sport, lap.avg_speed_mps),
        format_hr(lap.avg_hr),
        format_power(lap.avg_power),
        format_elevation(lap.total_ascent_m),
        format_elevation(lap.total_descent_m),
        "✓" if lap.is_interval else "",
    )


def interval_summary_text(laps: list[Lap], activity_sport: str) -> str:
    marked = [lap for lap in laps if lap.is_interval]
    if not marked:
        return "No intervals marked (press m on a lap to mark it)"
    total_distance = sum(lap.distance_m for lap in marked)
    total_duration = sum(lap.duration_s for lap in marked)
    avg_speed = total_distance / total_duration if total_duration else None
    sport = common_sport(marked, activity_sport)

    def weighted(attr: str) -> float | None:
        samples = [(getattr(lap, attr), lap.duration_s) for lap in marked if getattr(lap, attr) is not None]
        total = sum(d for _, d in samples)
        return sum(v * d for v, d in samples) / total if total else None

    avg_hr = weighted("avg_hr")
    return (
        f"Intervals: {len(marked)}  |  Distance: {format_lap_distance(sport, total_distance)}  |  "
        f"Duration: {format_duration(total_duration)}  |  "
        f"Avg: {format_speed_or_pace(sport, avg_speed)}  |  "
        f"Avg HR: {format_hr(round(avg_hr) if avg_hr is not None else None)}  |  "
        f"Avg Power: {format_power(weighted('avg_power'))}"
    )


# --- Histograms -----------------------------------------------------------------


def histogram_bins(values: list[float], bin_width: float) -> list[tuple[float, int]]:
    """(bin_start, count) pairs covering every bin from the lowest to the highest occupied
    one, zero-count bins included — so the chart reads as a continuous distribution instead
    of skipping the gaps between whichever bins happen to have a phase in them."""
    if not values:
        return []
    counts = Counter(math.floor(v / bin_width) for v in values)
    lo, hi = min(counts), max(counts)
    return [(i * bin_width, counts.get(i, 0)) for i in range(lo, hi + 1)]


def adaptive_bin_width(values: list[float], base_width: float, max_bins: int) -> float:
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


def bin_label(start: float, width: float, base_width: float, edge_fmt) -> str:
    """A bin's x-axis label: just its (formatted) start when the bin is its original,
    unwidened size, or a "start-end" range once `adaptive_bin_width` has widened it —
    a single edge value would otherwise misread as the whole bin's worth of phases having
    that exact value."""
    if width <= base_width:
        return edge_fmt(start)
    return f"{edge_fmt(start)}-{edge_fmt(start + width)}"


MAX_HISTOGRAM_BINS = 12


def phase_histograms(phases: list[Lap], activity_sport: str) -> list[tuple[str, list[tuple[str, int]]]]:
    """The three phase histograms (duration, pace/speed, power) as (title, [(label, count)])."""
    sport = common_sport(phases, activity_sport)

    def build(values, base_width, edge_fmt, unit_suffix, title):
        width = adaptive_bin_width(values, base_width, MAX_HISTOGRAM_BINS)
        bins = histogram_bins(values, width)
        return f"{title} ({int(width)}{unit_suffix} bins)", [
            (bin_label(start, width, base_width, edge_fmt), count) for start, count in bins
        ]

    out = [build([p.duration_s for p in phases], 1.0, lambda v: f"{int(round(v))}s", "s", "Duration histogram")]
    if sport in SWIM_SPORTS:
        out.append(build([100 / p.avg_speed_mps for p in phases if p.avg_speed_mps], 5.0, format_duration, "s/100m", "Pace histogram"))
    elif sport in ("running", "walking", "hiking"):
        out.append(build([1000 / p.avg_speed_mps for p in phases if p.avg_speed_mps], 5.0, format_duration, "s/km", "Pace histogram"))
    else:
        out.append(build([p.avg_speed_mps * 3.6 for p in phases if p.avg_speed_mps], 1.0, lambda v: f"{v:.0f} km/h", " km/h", "Speed histogram"))
    out.append(build([p.avg_power for p in phases if p.avg_power is not None], 5.0, lambda v: f"{int(round(v))}W", "W", "Power histogram"))
    return out


# --- Training week --------------------------------------------------------------


# TSS values are estimates, so they're shown rounded to this step; CTL/ATL/TSB move by about
# a point a day, so they're shown as whole numbers (coarser rounding would hide the trend).
TSS_ROUNDING = 5


def round_tss(value: float) -> int:
    return int(round(value / TSS_ROUNDING) * TSS_ROUNDING)


def format_pmc_point(label: str, point) -> str:
    if point is None:
        return f"{label}: –"
    return f"{label}: CTL {round(point.ctl)}  ATL {round(point.atl)}  TSB {round(point.tsb):+d}"


def tss_how(item: training_load.WeekItem) -> str:
    """The "How estimated" column: the method for completed activities, the plan's estimate
    for planned workouts."""
    parts = [item.method] if item.method else []
    if item.planned_tss is not None:
        plan = f"≈ plan {round_tss(item.planned_tss)}"
        if item.plan_assumed:
            plan += " (lap-button steps assumed)"
        parts.append(plan)
    return "; ".join(parts)


def format_thresholds(th: training_load.Thresholds) -> str:
    parts = []
    if th.ftp_cycling_w:
        parts.append(f"FTP {th.ftp_cycling_w:.0f} W")
    if th.ftp_running_w:
        parts.append(f"run threshold {th.ftp_running_w:.0f} W")
    if th.lthr_running or th.lthr_cycling:
        parts.append(
            f"LTHR run {format_hr(th.lthr_running and round(th.lthr_running))} / "
            f"bike {format_hr(th.lthr_cycling and round(th.lthr_cycling))}"
        )
    if th.threshold_speed_running_mps:
        parts.append(f"threshold pace {format_speed_or_pace('running', th.threshold_speed_running_mps)}")
    return "Thresholds: " + (" · ".join(parts) if parts else "none yet (press r)")


# status -> (label shown, colour name understood by both Rich and the GUIs' palettes)
WEEK_STATUS_STYLES = {
    "done (planned)": ("✓ done", "green"),
    "unplanned": ("✓ extra", "cyan"),
    "planned": ("planned", ""),
    "missed": ("✗ missed", "red"),
}
WEEK_COLUMNS = ("Day", "Date", "Status", "Sport", "Workout", "Duration", "Distance", "TSS", "How estimated")


def week_item_cells(item: training_load.WeekItem, today: date) -> tuple[tuple[str, ...], str]:
    """One week-view row as display values, plus its colour ("" for default) — today's rows
    are additionally bold in the TUI."""
    status, color = WEEK_STATUS_STYLES.get(item.status, (item.status, ""))
    return (
        item.day.strftime("%a"),
        item.day.strftime("%d.%m"),
        status,
        item.sport,
        item.title,
        format_duration(item.duration_s) if item.duration_s else "–",
        format_lap_distance(item.sport, item.distance_m) if item.distance_m else "–",
        str(round_tss(item.tss)),
        tss_how(item),
    ), color


def week_title(start: date) -> str:
    end = start + timedelta(days=6)
    return f"Week {start.isocalendar().week}: {start:%d.%m} – {end:%d.%m.%Y}"


def week_summary_text(view: services.TrainingWeekView) -> str:
    w = view.week
    projected = view.week_end_point and view.week_end_point.day > date.today()
    done, remaining = round_tss(w.done_tss), round_tss(w.remaining_tss)
    return (
        f"TSS  done {done}  ·  remaining {remaining}  ·  missed {round_tss(w.missed_tss)}  ·  "
        f"week total {done + remaining}\n"
        f"{format_pmc_point('Today', view.today_point)}\n"
        f"{format_pmc_point('Week end (projected)' if projected else 'Week end', view.week_end_point)}\n"
        f"{format_thresholds(view.thresholds)}"
    )


# --- Zones, compare charts, routes ------------------------------------------------

ZONE_COLUMNS = ("#", "Range", "Time", "%")


def zone_rows(zones) -> list[tuple[str, ...]]:
    if not zones:
        return [("–", "Couldn't parse zone data", "–", "–")]
    total = sum(zone.seconds for zone in zones) or 1.0
    rows = []
    for zone in sorted(zones, key=lambda z: z.zone_number):
        if zone.low_boundary is not None and zone.high_boundary is not None:
            zone_range = f"{zone.low_boundary:.0f}–{zone.high_boundary:.0f}"
        elif zone.low_boundary is not None:
            zone_range = f"{zone.low_boundary:.0f}+"
        else:
            zone_range = "–"
        rows.append((str(zone.zone_number), zone_range, format_duration(zone.seconds), f"{zone.seconds / total * 100:.0f}%"))
    return rows


# (record attribute, chart title, scale applied to values)
COMPARE_METRICS = (
    ("power", "Power (W)", 1.0),
    ("heart_rate", "Heart Rate (bpm)", 1.0),
    ("speed_mps", "Speed (km/h)", 3.6),
)
COMPARE_X_AXIS_LABELS = {"time": "Elapsed seconds", "distance": "Elapsed meters", "percent": "% of interval"}


def compare_series(marked_laps: list[Lap], records, utc_offset, attr: str, x_mode: str, scale: float = 1.0):
    """[(label, xs, ys)] — one line per marked interval, aligned to a common origin."""
    series = []
    for lap in marked_laps:
        lap_records = fit_parser.lap_record_slice(records, lap, utc_offset)
        x_values = fit_parser.lap_x_values(lap_records, lap, x_mode)
        xs, ys = fit_parser.metric_series(lap_records, x_values, attr)
        if xs:
            series.append((f"Lap {lap.lap_index}", xs, [y * scale for y in ys]))
    return series


ROUTE_COLUMNS = ("Name", "Sport", "Place", "Activities", "Distance")


def grouped_routes(routes, activities: list[ActivitySummary]) -> list[tuple]:
    """[(route, activity count)] for routes with 2+ activities (a route done once isn't worth
    tracking yet), most-used first."""
    counts: dict[str, int] = {}
    for activity in activities:
        if activity.route_id:
            counts[activity.route_id] = counts.get(activity.route_id, 0) + 1
    grouped = [r for r in routes if counts.get(r.route_id, 0) >= 2]
    return [(r, counts[r.route_id]) for r in sorted(grouped, key=lambda r: (-counts[r.route_id], r.name))]


def route_row(route, count: int) -> tuple[str, ...]:
    return (route.name, route.sport, route.place or "–", str(count), format_distance(route.signature.distance_m))
