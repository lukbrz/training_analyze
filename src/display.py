"""Presentation helpers shared by every front end: they turn models into the exact strings
and values the screens show (table rows, summary lines, histogram bins), so the Textual TUI
and the GUIs render the same content. Plain Python — no UI toolkit imports here."""

import math
from collections import Counter
from dataclasses import dataclass
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

def tsv(columns, rows) -> str:
    """Rows as tab-separated text (header first), which pastes into a spreadsheet as cells.
    Used by every front end's "copy table"."""
    return "\n".join(["\t".join(columns), *("\t".join(str(v).strip() for v in row) for row in rows)])


# --- Activities -----------------------------------------------------------------

ACTIVITY_COLUMNS = (
    "Date", "Activity", "Sport", "Duration", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Route"
)
ACTIVITY_DURATION_COLUMN = 3


def activities_status(shown: int, listed: int, hidden: int) -> str:
    """The activity list's status line: shown under the sport filter, of those listed,
    plus how many hidden sports' activities settings leave out."""
    text = f"{shown} activities synced" if shown == listed else f"{shown} of {listed} activities"
    return text + (f" ({hidden} of hidden sports not listed)" if hidden else "")


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
    "#", "Start", "Duration", "Total", "Distance", "Pace/Speed", "Avg HR", "Avg Power", "Ascent", "Descent", "Int"
)


def lap_columns(multisport: bool) -> tuple[str, ...]:
    return LAP_COLUMNS[:1] + ("Sport",) + LAP_COLUMNS[1:] if multisport else LAP_COLUMNS


def lap_right_aligned_columns(multisport: bool) -> set[int]:
    columns = lap_columns(multisport)
    return {columns.index("Duration"), columns.index("Total")}


def lap_row(
    lap: Lap, index_label: str, activity_sport: str, multisport: bool, precise: bool, total_s: float
) -> tuple[str, ...]:
    """One laps-table row; `total_s` is the cumulative time at the end of this lap. Interval
    laps and their phase splits (`precise`) get tenths of a second — it's the only way a
    rep's phase durations visibly sum back to the rep's own duration (see
    fit_parser._interpolate_at_distance/_interpolate_at_time)."""
    sport = lap.sport or activity_sport
    fmt = format_duration_tenths if precise else format_duration
    duration, total = fmt(lap.duration_s), fmt(total_s)
    sport_cells = (LEG_NAMES.get(sport, sport),) if multisport else ()
    return (
        index_label,
        *sport_cells,
        lap.start_time_local[11:19] if lap.start_time_local else "–",
        duration,
        total,
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
# Below this many values, every value gets a regular bin (outlier fences need a real spread).
MIN_VALUES_FOR_OUTLIER_BINS = 8


@dataclass
class HistogramBar:
    label: str
    count: int
    outlier: bool = False  # an edge bar collecting values beyond the Tukey fences


def _tukey_fences(values: list[float]) -> tuple[float, float]:
    """The usual box-plot fences, Q1 - 1.5*IQR .. Q3 + 1.5*IQR, clamped to the data. Values
    beyond them — e.g. a 100 m split that includes an aid-station stop — are what stretch a
    histogram until every normal value lands in one or two bars."""
    ordered = sorted(values)
    if len(ordered) < MIN_VALUES_FOR_OUTLIER_BINS:
        return ordered[0], ordered[-1]

    def quantile(q: float) -> float:
        position = q * (len(ordered) - 1)
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    q1, q3 = quantile(0.25), quantile(0.75)
    iqr = q3 - q1
    return max(ordered[0], q1 - 1.5 * iqr), min(ordered[-1], q3 + 1.5 * iqr)


def robust_histogram(values: list[float], base_width: float, edge_fmt) -> tuple[float, list[HistogramBar]]:
    """(bin width, bars): up to MAX_HISTOGRAM_BINS regular bars spanning the bulk of the
    values (inside the Tukey fences), plus "<x" / "≥y" edge bars counting the outliers, so a
    few extreme values can't flatten the rest of the distribution. Edge bars are only used
    when the outliers would otherwise force wider bins."""
    if not values:
        return base_width, []
    low, high = _tukey_fences(values)
    width = adaptive_bin_width([low, high], base_width, MAX_HISTOGRAM_BINS)
    if adaptive_bin_width(values, base_width, MAX_HISTOGRAM_BINS) == width:
        # Every value already fits at this resolution: nothing to gain from edge bars, and
        # a genuinely slow rep shouldn't be lumped in with pauses.
        low, high = min(values), max(values)
    first, last = math.floor(low / width), math.floor(high / width)
    counts = Counter(math.floor(v / width) for v in values)
    below = sum(n for i, n in counts.items() if i < first)
    above = sum(n for i, n in counts.items() if i > last)
    bars = [HistogramBar(f"<{edge_fmt(first * width)}", below, True)] if below else []
    bars += [HistogramBar(bin_label(i * width, width, base_width, edge_fmt), counts.get(i, 0)) for i in range(first, last + 1)]
    if above:
        bars.append(HistogramBar(f"≥{edge_fmt((last + 1) * width)}", above, True))
    return width, bars


def histograms_tsv(histograms: dict[str, list[HistogramBar]]) -> str:
    """Histogram data as tab-separated text, for every front end's "copy"."""
    return tsv(("Chart", "Bin", "Phases"), [(title, bar.label, bar.count) for title, bars in histograms.items() for bar in bars])


def count_tick_step(top: int, max_ticks: int = 6) -> int:
    """A "nice" whole-number step (1, 2, 5, 10, 20, 25, 50, 100, ...) giving at most
    `max_ticks` intervals on a count axis: counts are integers, so ticks must be too."""
    magnitude = 1
    while True:
        for factor in (1, 2, 2.5, 5):
            step = factor * magnitude
            if step == int(step) and top / step <= max_ticks:
                return int(step)
        magnitude *= 10


def phase_histograms(phases: list[Lap], activity_sport: str) -> list[tuple[str, list[HistogramBar]]]:
    """The three phase histograms (duration, pace/speed, power) as (title, bars)."""
    sport = common_sport(phases, activity_sport)

    def build(values, base_width, edge_fmt, unit_suffix, title):
        width, bars = robust_histogram(values, base_width, edge_fmt)
        outliers = sum(b.count for b in bars if b.outlier)
        note = f", {outliers} outlier{'s' if outliers != 1 else ''} in edge bars" if outliers else ""
        return f"{title} ({int(width)}{unit_suffix} bins{note})", bars

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
        week_item_title(item),
        format_duration(item.duration_s) if item.duration_s else "–",
        format_lap_distance(item.sport, item.distance_m) if item.distance_m else "–",
        str(round_tss(item.tss)),
        tss_how(item),
    ), color


def week_item_title(item: training_load.WeekItem) -> str:
    """The Workout column; a row pairing a plan with an activity also names the plan when
    that adds something — the activity's name doesn't already contain it (Garmin names an
    activity after its workout, prefixed with the place), or it was planned for another day."""
    same_day = item.plan_day is None or item.plan_day == item.day
    if not item.plan_title or item.plan_title.casefold() in item.title.casefold() and same_day:
        return item.title
    day = f" ({item.plan_day:%a})" if item.plan_day and item.plan_day != item.day else ""
    return f"{item.title}  ← {item.plan_title}{day}"


def week_item_choice(item: training_load.WeekItem) -> str:
    """One line describing a week row, for pick-one dialogs (link to...)."""
    status = WEEK_STATUS_STYLES.get(item.status, (item.status, ""))[0]
    duration = f" · {format_duration(item.duration_s)}" if item.duration_s else ""
    return f"{item.day:%a %d.%m} · {status} · {item.sport} · {item.title}{duration}"


def week_day_choice(day: date, items: list[training_load.WeekItem]) -> str:
    """One move-to target: the day and what's already on it."""
    on_day = [i.title for i in items if i.day == day]
    return f"{day:%a %d.%m}" + (f"  —  {', '.join(on_day)}" if on_day else "")


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
        f"{week_metrics_text(view.load_metrics)}\n"
        f"{format_thresholds(view.thresholds)}"
    )


# --- Strength sets ----------------------------------------------------------------

SET_COLUMNS = ("#", "Exercise", "Reps", "Weight", "Volume", "Time", "Rest after")


def exercise_name(s: dict) -> str:
    name = s.get("exercise") or s.get("category") or "unknown"
    return name.replace("_", " ").capitalize()


def _kg(value: float | None) -> str:
    return f"{value:g} kg" if value else "–"


def strength_rows(sets: list[dict]) -> list[tuple[str, ...]]:
    """One row per active set, with the rest that follows it."""
    rows = []
    for i, s in enumerate(sets):
        if s.get("type") != "active":
            continue
        rest = sets[i + 1] if i + 1 < len(sets) and sets[i + 1].get("type") == "rest" else None
        reps, kg = s.get("reps"), s.get("weight_kg")
        rows.append((
            str(len(rows) + 1),
            exercise_name(s),
            str(reps) if reps else "–",
            _kg(kg),
            _kg(reps * kg) if reps and kg else "–",
            format_duration(s.get("duration_s") or 0),
            format_duration(rest["duration_s"]) if rest and rest.get("duration_s") else "–",
        ))
    return rows


def strength_summary(sets: list[dict]) -> str:
    """Totals and one line per exercise ("3 x 10 @ 16 kg"), in the order first done."""
    active = [s for s in sets if s.get("type") == "active"]
    if not active:
        return "No sets recorded for this activity"
    reps = sum(s.get("reps") or 0 for s in active)
    volume = sum((s.get("reps") or 0) * (s.get("weight_kg") or 0) for s in active)
    work = sum(s.get("duration_s") or 0 for s in active)
    rest = sum(s.get("duration_s") or 0 for s in sets if s.get("type") == "rest")
    lines = [
        f"Sets: {len(active)}  |  Reps: {reps}  |  Volume: {volume:g} kg  |  "
        f"Work: {format_duration(work)}  |  Rest: {format_duration(rest)}"
    ]
    by_exercise: dict[str, list[dict]] = {}
    for s in active:
        by_exercise.setdefault(exercise_name(s), []).append(s)
    for name, group in by_exercise.items():
        parts = [
            f"{s['reps']}" + (f" @ {s['weight_kg']:g} kg" if s.get("weight_kg") else "") if s.get("reps")
            else format_duration(s.get("duration_s") or 0)
            for s in group
        ]
        lines.append(f"  {name}: " + ", ".join(parts))
    return "\n".join(lines)


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


# --- Structured stats for the graphical front ends (stat tiles) -----------------


@dataclass
class Stat:
    label: str
    value: str
    unit: str = ""
    color: str = ""  # palette name: green, red, yellow, cyan, magenta, accent, orange ("" = default)
    hint: str = ""


def _split_unit(text: str) -> tuple[str, str]:
    """"5:21 /km" -> ("5:21", "/km"), "137" -> ("137", "")."""
    value, _, unit = text.partition(" ")
    return value, unit


def activity_stats(activity: ActivitySummary) -> list[Stat]:
    pace = _split_unit(format_speed_or_pace(activity.sport, activity.avg_speed_mps))
    distance = _split_unit(format_distance(activity.distance_m))
    stats = [
        Stat("Duration", format_duration(activity.duration_s)),
        Stat("Distance", *distance),
        Stat("Pace" if activity.sport in ("running", "walking", "hiking") or activity.sport in SWIM_SPORTS else "Speed", *pace),
        Stat("Avg HR", format_hr(activity.avg_hr), "bpm" if activity.avg_hr else "", "red" if activity.avg_hr else ""),
        Stat("Avg power", *_split_unit(format_power(activity.avg_power)), "magenta" if activity.avg_power else ""),
    ]
    if activity.total_ascent_m:
        stats.append(Stat("Ascent", f"{activity.total_ascent_m:.0f}", "m", "yellow"))
    return stats


def leg_stats(device_laps: list[Lap]) -> list[tuple[str, str, list[Stat]]]:
    """Per-leg stats of a multisport activity: [(leg name, sport, stats)]."""
    out = []
    for name, leg in legs(device_laps):
        distance = format_lap_distance(leg.sport, leg.distance_m) if leg.sport in SWIM_SPORTS else format_distance(leg.distance_m)
        stats = [
            Stat("Time", format_duration(leg.duration_s)),
            Stat("Distance", *_split_unit(distance)),
            Stat("Pace/Speed", *_split_unit(format_speed_or_pace(leg.sport, leg.avg_speed_mps))),
            Stat("HR", format_hr(leg.avg_hr), "bpm" if leg.avg_hr else "", "red" if leg.avg_hr else ""),
        ]
        if leg.avg_power:
            stats.append(Stat("Power", *_split_unit(format_power(leg.avg_power)), "magenta"))
        out.append((name, leg.sport, stats))
    return out


def interval_stats(laps: list[Lap], activity_sport: str) -> list[Stat]:
    marked = [lap for lap in laps if lap.is_interval]
    if not marked:
        return []
    total_distance = sum(lap.distance_m for lap in marked)
    total_duration = sum(lap.duration_s for lap in marked)
    sport = common_sport(marked, activity_sport)

    def weighted(attr: str) -> float | None:
        samples = [(getattr(lap, attr), lap.duration_s) for lap in marked if getattr(lap, attr) is not None]
        total = sum(d for _, d in samples)
        return sum(v * d for v, d in samples) / total if total else None

    hr, power = weighted("avg_hr"), weighted("avg_power")
    return [
        Stat("Intervals", str(len(marked)), "", "accent"),
        Stat("Distance", *_split_unit(format_lap_distance(sport, total_distance))),
        Stat("Time", format_duration(total_duration)),
        Stat("Avg", *_split_unit(format_speed_or_pace(sport, total_distance / total_duration if total_duration else None)), "green"),
        Stat("Avg HR", format_hr(round(hr) if hr is not None else None), "bpm" if hr else "", "red" if hr else ""),
        Stat("Avg power", *_split_unit(format_power(power)), "magenta" if power else ""),
    ]


def form_color(tsb: float) -> tuple[str, str]:
    """(colour, wording) for a TSB value, along the usual Performance Management Chart bands."""
    if tsb > 15:
        return "cyan", "very fresh"
    if tsb > 5:
        return "green", "fresh"
    if tsb >= -10:
        return "", "neutral"
    if tsb >= -30:
        return "yellow", "productive load"
    return "red", "overreaching risk"


def week_load_stats(view: services.TrainingWeekView) -> list[Stat]:
    """CTL / ATL / TSB tiles: today's value, with the week-end projection as a hint."""
    today, end = view.today_point, view.week_end_point
    projected = end is not None and end.day > date.today()
    end_label = "week end (plan)" if projected else "week end"

    def hint(value) -> str:
        return f"→ {round(value):+d} {end_label}" if value is not None else ""

    if today is None:
        return [Stat("Fitness (CTL)", "–"), Stat("Fatigue (ATL)", "–"), Stat("Form (TSB)", "–")]
    color, wording = form_color(today.tsb)
    return [
        Stat("Fitness (CTL)", str(round(today.ctl)), "", "accent", f"→ {round(end.ctl)} {end_label}" if end else ""),
        Stat("Fatigue (ATL)", str(round(today.atl)), "", "magenta", f"→ {round(end.atl)} {end_label}" if end else ""),
        Stat("Form (TSB)", f"{round(today.tsb):+d}", wording, color, hint(end.tsb if end else None)),
    ]


def _metric(value: float | None, fmt: str) -> str:
    return "–" if value is None else format(value, fmt)


def week_metric_stats(m) -> list[Stat]:
    """Ramp rate / ACWR / monotony / strain tiles (see metrics.WeekMetrics), coloured where
    the value is past the usual caution line."""
    acwr_color = "" if m.acwr is None else "red" if m.acwr > 1.5 else "yellow" if m.acwr > 1.3 else "green" if m.acwr >= 0.8 else ""
    return [
        Stat("Ramp rate", _metric(m.ramp_rate, "+.1f"), "CTL/week", "yellow" if (m.ramp_rate or 0) > 8 else "", "≤ 5-8 is sustainable"),
        Stat("ACWR", _metric(m.acwr, ".2f"), "ATL/CTL", acwr_color, "0.8-1.3 sweet spot"),
        Stat("Monotony", _metric(m.monotony, ".1f"), "", "yellow" if (m.monotony or 0) > 2 else "", "> 2: too little variation"),
        Stat("Strain", _metric(m.strain, ".0f"), "", "", "weekly TSS x monotony"),
    ]


def week_metrics_text(m) -> str:
    return "  ·  ".join(f"{s.label} {s.value}{(' ' + s.unit) if s.unit and s.value != '–' else ''}" for s in week_metric_stats(m))


def activity_metric_stats(m) -> list[Stat]:
    """Efficiency factor / aerobic decoupling tiles (see metrics.ActivityMetrics); empty when
    neither applies."""
    stats = []
    if m.efficiency_factor is not None:
        stats.append(Stat("Efficiency factor", f"{m.efficiency_factor:.2f}", m.ef_basis, "accent", "output per heartbeat"))
    if m.decoupling_pct is not None:
        color = "green" if m.decoupling_pct < 5 else "yellow" if m.decoupling_pct < 10 else "red"
        stats.append(Stat("Aerobic decoupling", f"{m.decoupling_pct:+.1f}", "%", color, "< 5% on steady sessions: good endurance"))
    return stats


def activity_metrics_text(m) -> str:
    return "  |  ".join(f"{s.label}: {s.value} {s.unit}".rstrip() for s in activity_metric_stats(m))


def week_strip_stats(view: services.TrainingWeekView) -> tuple[list[Stat], list[Stat], str]:
    """The week header's one-line strip: (fitness stats with a short "→ week end" hint
    each, load-metric stats whose hints are explanations, caption for the arrows)."""
    today, end = view.today_point, view.week_end_point
    projected = end is not None and end.day > date.today()
    load = week_load_stats(view)
    if today is not None and end is not None:
        load[0].hint = f"→ {round(end.ctl)}"
        load[1].hint = f"→ {round(end.atl)}"
        load[2].hint = f"→ {round(end.tsb):+d}"
    return load, week_metric_stats(view.load_metrics), "→ week end" + (" (plan)" if projected else "")


def week_tss_caption(view: services.TrainingWeekView) -> str:
    """One caption line for the TSS bar, with the thresholds the estimates use."""
    parts = week_tss_parts(view)
    missed = f" · {parts['missed']} missed" if parts["missed"] else ""
    return (
        f"Week TSS {parts['done']} done + {parts['remaining']} remaining = {parts['done'] + parts['remaining']}{missed}"
        f"   ·   {format_thresholds(view.thresholds)}"
    )


def week_tss_parts(view: services.TrainingWeekView) -> dict[str, int]:
    """Rounded TSS done / remaining / missed, for a stacked progress bar."""
    w = view.week
    return {"done": round_tss(w.done_tss), "remaining": round_tss(w.remaining_tss), "missed": round_tss(w.missed_tss)}


def metric_legend(metric: str, sport: str):
    """(label, value formatter) for a map colouring metric (see laps_session.MAP_METRICS)."""
    if metric == "speed_mps":
        pace = sport in ("running", "walking", "hiking") or sport in SWIM_SPORTS
        return ("Pace" if pace else "Speed"), (lambda v: format_speed_or_pace(sport, v))
    return {
        "heart_rate": ("Heart rate", lambda v: f"{v:.0f} bpm"),
        "power": ("Power", lambda v: f"{v:.0f} W"),
        "altitude_m": ("Elevation", lambda v: f"{v:.0f} m"),
    }[metric]
