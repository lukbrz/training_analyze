"""Comparison charts, described once for the TUI, Qt and web front ends.

Two comparisons:
- marked intervals of one activity against each other (laps view, `c`): power, HR and
  speed per interval, raw or smoothed, either as values or as the difference to the best
  interval (the fastest one) at the same point of the interval;
- efforts of a route's leaderboard (`m` marks, `c` compares): the time gap to the fastest
  effort along the distance, and the time lost or gained on each stage of it.

The front ends only draw the Chart objects built here.
"""

from bisect import bisect_left
from dataclasses import dataclass, field

import fit_parser
from models import Lap

# --- Charts as data ----------------------------------------------------------------------


@dataclass
class Series:
    label: str
    xs: list[float]
    ys: list[float]


@dataclass
class Chart:
    title: str
    series: list[Series]
    x_label: str = ""
    y_label: str = ""
    kind: str = "line"  # "line", or "bar": xs are indexes into `categories`
    categories: list[str] = field(default_factory=list)
    zero_line: bool = False  # draw y = 0 (difference charts)

    @property
    def empty(self) -> bool:
        return not any(s.xs for s in self.series)


# --- Intervals of one activity --------------------------------------------------------------

# (record attribute, name, unit, scale applied to values)
METRICS = (
    ("power", "Power", "W", 1.0),
    ("heart_rate", "Heart rate", "bpm", 1.0),
    ("speed_mps", "Speed", "km/h", 3.6),
)
X_MODES = (("time", "Time"), ("distance", "Distance"), ("percent", "%"))
X_AXIS_LABELS = {"time": "Elapsed seconds", "distance": "Elapsed meters", "percent": "% of interval"}
VALUE_MODES = (("absolute", "Values"), ("relative", "vs best"))
SMOOTHING_CHOICES = ((0, "Raw"), (5, "5 s"), (15, "15 s"), (30, "30 s"), (60, "60 s"))
DEFAULT_SMOOTHING_S = 15


def smooth(times: list[float], values: list[float], window_s: float) -> list[float]:
    """Centred moving average over `window_s` seconds of time (not of samples, so a
    recording gap doesn't stretch it); `times` must be in order."""
    if window_s <= 0 or len(values) < 3:
        return list(values)
    half = window_s / 2
    out, lo, hi, total = [], 0, 0, 0.0
    for t in times:
        while hi < len(values) and times[hi] <= t + half:
            total += values[hi]
            hi += 1
        while times[lo] < t - half:
            total -= values[lo]
            lo += 1
        out.append(total / (hi - lo))
    return out


def best_interval(laps: list[Lap]) -> int:
    """Index of the best of the marked intervals: the fastest, or the most powerful when
    there's no speed."""
    def key(lap: Lap) -> tuple[float, float]:
        return (lap.avg_speed_mps or 0.0, lap.avg_power or 0.0)
    return max(range(len(laps)), key=lambda i: key(laps[i])) if laps else 0


def _interval_series(lap: Lap, label: str, records, utc_offset, attr: str, x_mode: str, scale: float, smoothing_s: float) -> Series:
    stretch = fit_parser.lap_record_slice(records, lap, utc_offset)
    if not stretch:
        return Series(label, [], [])
    x_values = fit_parser.lap_x_values(stretch, lap, x_mode)
    if x_mode == "distance":  # a sample without distance would read as 0 m: hold the last one
        x_values = _running_max(x_values)
    t0 = stretch[0].timestamp_utc
    xs, ts, ys = [], [], []
    for x, record in zip(x_values, stretch):
        value = getattr(record, attr)
        if value is not None:
            xs.append(x)
            ts.append((record.timestamp_utc - t0).total_seconds())
            ys.append(value * scale)
    return Series(label, xs, smooth(ts, ys, smoothing_s))


def _running_max(values: list[float]) -> list[float]:
    out, top = [], float("-inf")
    for v in values:
        top = max(top, v)
        out.append(top)
    return out


def _interp(xs: list[float], ys: list[float], x: float) -> float:
    """Linear interpolation on ascending `xs` (x within their range)."""
    i = bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    return ys[i] if x1 == x0 else ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def _minus_reference(series: Series, reference: Series) -> Series:
    """`series` minus `reference` at the same x, where both have data."""
    if not reference.xs:
        return Series(series.label, [], [])
    lo, hi = reference.xs[0], reference.xs[-1]
    points = [(x, y - _interp(reference.xs, reference.ys, x)) for x, y in zip(series.xs, series.ys) if lo <= x <= hi]
    return Series(series.label, [p[0] for p in points], [p[1] for p in points])


def interval_charts(
    laps: list[Lap], labels: list[str], records, utc_offset, x_mode: str = "time", value_mode: str = "absolute",
    smoothing_s: float = DEFAULT_SMOOTHING_S, speed_note: str = "",
) -> list[Chart]:
    """One chart per metric (power, HR, speed), one line per marked interval. "relative"
    draws each interval minus the best one (best_interval) at the same x."""
    best = best_interval(laps)
    charts = []
    for attr, name, unit, scale in METRICS:
        series = [_interval_series(lap, label, records, utc_offset, attr, x_mode, scale, smoothing_s) for lap, label in zip(laps, labels)]
        title = f"{name} ({unit})"
        if value_mode == "relative":
            series = [_minus_reference(s, series[best]) for s in series]
            title = f"{name} difference to {labels[best]}, the best ({unit})"
        notes = [speed_note] if attr == "speed_mps" and speed_note else []
        notes += [f"{smoothing_s:g} s average"] if smoothing_s else []
        title = " · ".join([title] + notes)
        charts.append(Chart(title, [s for s in series if s.xs], X_AXIS_LABELS[x_mode], unit, zero_line=value_mode == "relative"))
    return charts


def interval_labels(laps: list[Lap]) -> list[str]:
    return [f"Lap {lap.lap_index}" for lap in laps]


# --- Leaderboard efforts along the distance ---------------------------------------------------

CURVE_POINTS = 400  # an effort's distance/time curve is kept at about this many points
GAP_POINTS = 300
STAGE_STEPS_M = (100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000)
MAX_STAGES = 20


def distance_time_curve(records) -> list[tuple[float, float]]:
    """(metres from the start, moving seconds from the start) along a stretch of records —
    pauses (fit_parser.pause_mask) don't add time, so the end is the effort's moving time.
    Thinned to about CURVE_POINTS points."""
    if len(records) < 2:
        return []
    d0 = next((r.distance_m for r in records if r.distance_m is not None), None)
    if d0 is None:
        return []
    mask = fit_parser.pause_mask(records)
    curve, t, d = [], 0.0, 0.0
    for i, record in enumerate(records):
        if i and not mask[i]:
            t += (record.timestamp_utc - records[i - 1].timestamp_utc).total_seconds()
        if record.distance_m is not None:
            d = max(d, record.distance_m - d0)
        curve.append((round(d, 1), round(t, 2)))
    step = max(1, len(curve) // CURVE_POINTS)
    thinned = curve[::step]
    if thinned[-1] != curve[-1]:
        thinned.append(curve[-1])
    return thinned


def _time_at(curve: list[tuple[float, float]], distances: list[float], x: float) -> float:
    i = bisect_left(distances, x)
    if i <= 0:
        return curve[0][1]
    if i >= len(curve):
        return curve[-1][1]
    (d0, t0), (d1, t1) = curve[i - 1], curve[i]
    return t1 if d1 == d0 else t0 + (t1 - t0) * (x - d0) / (d1 - d0)


class _Timeline:
    """An effort's moving time at any point of the route, its own distance stretched to the
    route's length (GPS loops of one route differ by a few per cent; stretching lines up
    start and finish, so the gap at the finish is the difference in leaderboard time)."""

    def __init__(self, curve: list[tuple[float, float]], length_m: float) -> None:
        self.curve = curve
        self.distances = [d for d, _ in curve]
        self.scale = curve[-1][0] / length_m

    def at(self, x: float) -> float:
        return _time_at(self.curve, self.distances, x * self.scale)


def stage_step(length_m: float) -> int:
    return next((s for s in STAGE_STEPS_M if length_m / s <= MAX_STAGES), STAGE_STEPS_M[-1])


def _distance_text(metres: float, in_km: bool) -> str:
    return f"{metres / 1000:g} km" if in_km else f"{metres:.0f} m"


def effort_label(rank: int, effort) -> str:
    loop = "" if effort.loop == "whole" else f" loop {effort.loop}"
    return f"#{rank} {effort.date}{loop}"


def effort_charts(reference, reference_rank: int, others: list[tuple[int, object]], length_m: float) -> list[Chart]:
    """Gap to the reference (the board's fastest) along the route, and the time each
    effort lost (+) or gained (−) on each stage of it. `others`: (rank, leaderboard.Effort)."""
    usable = [(rank, e) for rank, e in others if e.curve and e.curve[-1][0] > 0]
    if not reference.curve or reference.curve[-1][0] <= 0 or not usable:
        return []
    in_km = length_m >= 3000
    unit = 1000 if in_km else 1
    ref = _Timeline(reference.curve, length_m)
    grid = [length_m * i / GAP_POINTS for i in range(GAP_POINTS + 1)]
    ref_times = [ref.at(x) for x in grid]
    ref_label = effort_label(reference_rank, reference)

    gap_series = [Series(f"{ref_label} (reference)", [x / unit for x in grid], [0.0] * len(grid))]
    timelines = []
    for rank, effort in usable:
        timeline = _Timeline(effort.curve, length_m)
        timelines.append((effort_label(rank, effort), timeline))
        gap_series.append(Series(timelines[-1][0], [x / unit for x in grid], [timeline.at(x) - t for x, t in zip(grid, ref_times)]))

    step = stage_step(length_m)
    bounds = [float(d) for d in range(0, int(length_m), step)] + [length_m]
    if len(bounds) > 2 and bounds[-1] - bounds[-2] < step * 0.25:  # fold a sliver of a last stage
        del bounds[-2]
    categories = [_distance_text(end, in_km) for end in bounds[1:]]
    stage_series = []
    for label, timeline in timelines:
        lost = []
        for start, end in zip(bounds, bounds[1:]):
            lost.append((timeline.at(end) - timeline.at(start)) - (ref.at(end) - ref.at(start)))
        stage_series.append(Series(label, list(range(len(categories))), lost))

    x_label = "Distance (km)" if in_km else "Distance (m)"
    return [
        Chart(f"Gap to {ref_label} — seconds behind (+) / ahead (−)", gap_series, x_label, "s", zero_line=True),
        Chart(f"Time lost (+) / gained (−) per {_distance_text(step, step >= 1000)} stage vs {ref_label}", stage_series,
              "Stage ending at", "s", kind="bar", categories=categories, zero_line=True),
    ]
