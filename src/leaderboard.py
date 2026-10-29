"""Personal leaderboards: the athlete's own loops of a route, ranked by time.

A route is one of the app's GPS routes (routes.py) — virtual courses included, their
virtual-world coordinates group like real ones. The loops of each activity on it are the
TPV route's loops for TrainingPeaks Virtual rides (services.tpv_loops: TPV's laps at the
route's lap line) and loops found on the GPS track otherwise (fit_parser.
detect_physical_laps, cached per activity — it takes seconds on a long ride). Each loop's
time, speed, power and HR are over its moving time (pauses left out), and its W/kg uses
the athlete's weight on the day. Each activity is also a whole pass of the route. effort itself. Loops and whole passes are ranked on separate boards, each counting only
"clean" efforts — within tolerance of the usual loop / route distance (no lead-in or detour
inside, no cut-short ride): tight for TPV's exact virtual distances, looser for GPS ones.
"""

from dataclasses import dataclass

import fit_parser
import metrics
import services
import storage
from models import ActivitySummary, Lap

CLEAN_TOLERANCE_TPV = 0.015
CLEAN_TOLERANCE_GPS = 0.03
PHYSICAL_LAPS_VERSION = "2"  # bump when fit_parser.detect_physical_laps changes


@dataclass
class Effort:
    activity_id: str
    date: str
    title: str
    loop: str  # the loop's label in its activity ("3" = third loop)
    duration_s: float  # moving time of the loop
    distance_m: float
    speed_mps: float | None
    power_w: float | None
    hr: float | None
    w_per_kg: float | None
    paused_s: float = 0.0
    exact: bool = False  # distance from TPV's virtual course (exact), not GPS


def _gps_loops(activity: ActivitySummary, records: list) -> list[Lap]:
    """GPS-detected loops of an activity, from the per-activity cache when it's current."""
    key = f"{PHYSICAL_LAPS_VERSION}|{fit_parser.records_cache_key(activity.activity_id)}"
    cached = storage.load_physical_laps(activity.activity_id, key)
    if cached is not None:
        return cached
    offset = fit_parser.load_utc_offset(activity.activity_id, activity.start_time_local)
    laps = fit_parser.detect_physical_laps(records, offset) or []
    storage.save_physical_laps(activity.activity_id, key, laps)
    return laps


def _weight_on(day: str, history: dict[str, float]) -> float | None:
    dates = [d for d in history if d <= day]
    return history[max(dates)] if dates else None


def _effort(activity: ActivitySummary, records: list, offset, lap: Lap, weight: float | None, exact: bool) -> Effort:
    stretch = fit_parser.lap_record_slice(records, lap, offset)
    stats = metrics.segment_stats(stretch, foot=False) if len(stretch) > 1 else {}
    moving = stats.get("moving_s") or lap.duration_s
    power = stats.get("avg_power_w")
    return Effort(
        activity.activity_id, activity.start_time_local[:10], activity.name, lap.label, moving, lap.distance_m,
        lap.distance_m / moving if moving else None, power, stats.get("avg_hr"),
        power / weight if power and weight else None, stats.get("paused_s") or 0.0, exact,
    )


def activity_efforts(activity: ActivitySummary, weights: dict[str, float]) -> tuple[list[Effort], float | None, Effort | None]:
    """(the activity's loops as efforts, its loop distance, the whole activity as an
    effort) — clean or not."""
    if not storage.has_fit(activity.activity_id):
        return [], None, None
    records = fit_parser.load_records(activity.activity_id)
    if len(records) < 2:
        return [], None, None
    tpv = services.tpv_loops(activity)
    laps = tpv.laps if tpv else _gps_loops(activity, records)
    loops = [lap for lap in laps if (lap.label or "").isdigit()]
    offset = fit_parser.load_utc_offset(activity.activity_id, activity.start_time_local)
    weight = _weight_on(activity.start_time_local[:10], weights)
    loop_m = (tpv.loop_m if tpv else sorted(lap.distance_m for lap in loops)[len(loops) // 2]) if loops else None
    whole = fit_parser.records_to_lap(records, 1, offset)
    whole.label = "whole"
    exact = tpv is not None
    return (
        [_effort(activity, records, offset, lap, weight, exact) for lap in loops],
        loop_m,
        _effort(activity, records, offset, whole, weight, exact),
    )


@dataclass
class Board:
    """A route's two leaderboards: single loops, and complete passes of the whole route."""
    loops: list[Effort]
    loop_m: float | None
    routes: list[Effort]
    route_m: float | None


def _clean(efforts: list[Effort], length: float) -> list[Effort]:
    """Efforts within tolerance of `length`, fastest first."""
    return sorted(
        (e for e in efforts if abs(e.distance_m - length) <= length * (CLEAN_TOLERANCE_TPV if e.exact else CLEAN_TOLERANCE_GPS)),
        key=lambda e: e.duration_s,
    )


def _median(values: list[float]) -> float | None:
    return sorted(values)[len(values) // 2] if values else None


def route_leaderboard(route_id: str, progress=lambda _: None) -> Board:
    """The route's loops (each activity's loops of it) and whole passes (each activity
    from start to finish), each ranked separately. Slow the first time for activities
    whose loops come from GPS detection."""
    _, activities = storage.load_index()
    on_route = sorted((a for a in activities if a.route_id == route_id), key=lambda a: a.start_time_local)
    weights = (storage.load_athlete().get("history") or {}).get("weight_kg", {})
    loops, loop_lengths, wholes = [], [], []
    for n, activity in enumerate(on_route, start=1):
        progress(f"Finding loops {n}/{len(on_route)}: {activity.name}")
        activity_loops, loop_m, whole = activity_efforts(activity, weights)
        loops += activity_loops
        if loop_m:
            loop_lengths.append(loop_m)
        if whole:
            wholes.append(whole)
    loop_m, route_m = _median(loop_lengths), _median([w.distance_m for w in wholes])
    return Board(_clean(loops, loop_m) if loop_m else [], loop_m, _clean(wholes, route_m) if route_m else [], route_m)


def best_per_activity(efforts: list[Effort]) -> list[Effort]:
    """Each activity's fastest loop only (efforts already fastest first)."""
    seen, out = set(), []
    for effort in efforts:
        if effort.activity_id not in seen:
            seen.add(effort.activity_id)
            out.append(effort)
    return out
