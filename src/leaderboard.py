"""Personal leaderboards: the athlete's own loops of a route, ranked by time.

A route is one of the app's GPS routes (routes.py) — virtual courses included, their
virtual-world coordinates group like real ones. The loops of each activity on it are the
TPV route's loops for TrainingPeaks Virtual rides (services.tpv_loops: TPV's laps at the
route's lap line) and loops found on the GPS track otherwise (fit_parser.
detect_physical_laps, cached per activity — it takes seconds on a long ride). Each loop's
time, speed, power and HR are over its moving time (pauses left out), and its W/kg uses
the athlete's weight on the day. An activity with no loops (one pass of a route) is a single
effort itself. Only "clean" efforts — within tolerance of the route's usual effort distance
(no lead-in or detour inside) — are ranked: tight for TPV's exact virtual distances, looser
for GPS ones.
"""

from dataclasses import dataclass

import fit_parser
import metrics
import services
import storage
from models import ActivitySummary, Lap

CLEAN_TOLERANCE_TPV = 0.015
CLEAN_TOLERANCE_GPS = 0.03
PHYSICAL_LAPS_VERSION = "1"


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


def activity_efforts(activity: ActivitySummary, weights: dict[str, float]) -> tuple[list[Effort], float | None]:
    """(the activity's loops as efforts, its loop distance) — clean or not."""
    if not storage.has_fit(activity.activity_id):
        return [], None
    records = fit_parser.load_records(activity.activity_id)
    tpv = services.tpv_loops(activity)
    laps = tpv.laps if tpv else _gps_loops(activity, records)
    loops = [lap for lap in laps if (lap.label or "").isdigit()]
    offset = fit_parser.load_utc_offset(activity.activity_id, activity.start_time_local)
    if not loops:  # one pass of the route: the whole activity is the effort
        if len(records) < 2:
            return [], None
        whole = fit_parser.records_to_lap(records, 1, offset)
        whole.label = "whole"
        loops = [whole]
    loop_m = tpv.loop_m if tpv else sorted(lap.distance_m for lap in loops)[len(loops) // 2]
    weight = _weight_on(activity.start_time_local[:10], weights)
    efforts = []
    for lap in loops:
        stretch = fit_parser.lap_record_slice(records, lap, offset)
        stats = metrics.segment_stats(stretch, foot=False) if len(stretch) > 1 else {}
        moving = stats.get("moving_s") or lap.duration_s
        power = stats.get("avg_power_w")
        efforts.append(Effort(
            activity.activity_id, activity.start_time_local[:10], activity.name, lap.label, moving, lap.distance_m,
            lap.distance_m / moving if moving else None, power, stats.get("avg_hr"),
            power / weight if power and weight else None, stats.get("paused_s") or 0.0, tpv is not None,
        ))
    return efforts, loop_m


def route_leaderboard(route_id: str, progress=lambda _: None) -> tuple[list[Effort], float | None]:
    """(clean loops of every activity on the route, fastest first; the route's loop
    distance). Slow the first time for activities whose loops come from GPS detection."""
    _, activities = storage.load_index()
    on_route = sorted((a for a in activities if a.route_id == route_id), key=lambda a: a.start_time_local)
    weights = (storage.load_athlete().get("history") or {}).get("weight_kg", {})
    all_efforts, loop_lengths = [], []
    for n, activity in enumerate(on_route, start=1):
        progress(f"Finding loops {n}/{len(on_route)}: {activity.name}")
        efforts, loop_m = activity_efforts(activity, weights)
        all_efforts += efforts
        if loop_m:
            loop_lengths.append(loop_m)
    if not loop_lengths:
        return [], None
    loop_m = sorted(loop_lengths)[len(loop_lengths) // 2]
    clean = [
        e for e in all_efforts
        if abs(e.distance_m - loop_m) <= loop_m * (CLEAN_TOLERANCE_TPV if e.exact else CLEAN_TOLERANCE_GPS)
    ]
    return sorted(clean, key=lambda e: e.duration_s), loop_m


def best_per_activity(efforts: list[Effort]) -> list[Effort]:
    """Each activity's fastest loop only (efforts already fastest first)."""
    seen, out = set(), []
    for effort in efforts:
        if effort.activity_id not in seen:
            seen.add(effort.activity_id)
            out.append(effort)
    return out
