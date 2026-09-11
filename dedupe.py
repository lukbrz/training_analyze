import dataclasses
from datetime import datetime

from models import ActivitySummary, RecordPoint

START_TIME_TOLERANCE_S = 180.0
DISTANCE_TOLERANCE = 0.2
DURATION_TOLERANCE = 0.2


def is_duplicate(a: ActivitySummary, b: ActivitySummary) -> bool:
    """Two activities are the same physical session recorded by separate devices if
    they're the same sport, start within START_TIME_TOLERANCE_S of each other, and their
    distance and duration each agree within tolerance (different devices commonly disagree
    slightly on GPS-derived distance and on exactly when recording started/stopped)."""
    if a.sport != b.sport:
        return False
    try:
        ta = datetime.fromisoformat(a.start_time_local)
        tb = datetime.fromisoformat(b.start_time_local)
    except ValueError:
        return False
    if abs((ta - tb).total_seconds()) > START_TIME_TOLERANCE_S:
        return False
    if not _within_tolerance(a.distance_m, b.distance_m, DISTANCE_TOLERANCE):
        return False
    if not _within_tolerance(a.duration_s, b.duration_s, DURATION_TOLERANCE):
        return False
    return True


def _within_tolerance(x: float, y: float, tolerance: float) -> bool:
    longer = max(x, y)
    return longer == 0 or abs(x - y) / longer <= tolerance


def find_duplicate_pairs(
    activities: list[ActivitySummary],
) -> list[tuple[ActivitySummary, ActivitySummary]]:
    """All pairs of activities that look like the same physical session recorded twice."""
    pairs = []
    for i, a in enumerate(activities):
        for b in activities[i + 1 :]:
            if is_duplicate(a, b):
                pairs.append((a, b))
    return pairs


def choose_primary(a: ActivitySummary, b: ActivitySummary) -> tuple[ActivitySummary, ActivitySummary]:
    """Pick which of two duplicate activities to treat as primary: the one with more
    optional fields populated wins (richer sensor data), tie-broken by longer duration."""

    def completeness(s: ActivitySummary) -> tuple[int, float]:
        fields = (s.avg_hr, s.avg_power, s.total_ascent_m, s.avg_speed_mps)
        return (sum(f is not None for f in fields), s.duration_s)

    return (a, b) if completeness(a) >= completeness(b) else (b, a)


def merge_summary(primary: ActivitySummary, secondaries: list[ActivitySummary]) -> ActivitySummary:
    """A copy of `primary` with any null optional field filled from the first secondary
    that has it. Distance/duration/sport/name/start_time stay primary's own — they're
    already a full, self-consistent session measurement from its own device."""
    merged = dataclasses.replace(primary)
    for field in ("avg_hr", "avg_power", "total_ascent_m", "avg_speed_mps"):
        if getattr(merged, field) is not None:
            continue
        for secondary in secondaries:
            value = getattr(secondary, field)
            if value is not None:
                setattr(merged, field, value)
                break
    return merged


def merge_records(streams: list[list[RecordPoint]]) -> list[RecordPoint]:
    """Combine multiple record streams for the same physical session into one, keyed by
    wall-clock second (FIT timestamps are UTC regardless of device, so streams line up
    without any offset conversion). `streams[0]` is primary: at each shared timestamp its
    own field values win, with any null field filled from a later stream that has data
    there; a timestamp missing from the primary stream entirely (a recording gap) is
    filled in wholesale from whichever other stream has it.

    Known limitation: `distance_m` is each device's own cumulative-distance measurement,
    so switching source at a gap can introduce a small jump rather than a perfectly smooth
    curve. Acceptable for a personal tool — the point is filling missing HR/power/GPS
    coverage, not producing a metrologically perfect distance trace.
    """
    if not streams:
        return []
    by_ts = [{r.timestamp_utc.replace(microsecond=0): r for r in stream} for stream in streams]
    all_ts = sorted(set().union(*by_ts))

    merged = []
    for ts in all_ts:
        candidates = [d[ts] for d in by_ts if ts in d]
        merged.append(
            RecordPoint(
                timestamp_utc=ts,
                distance_m=_first_non_null(candidates, "distance_m"),
                heart_rate=_first_non_null(candidates, "heart_rate"),
                power=_first_non_null(candidates, "power"),
                speed_mps=_first_non_null(candidates, "speed_mps"),
                lat=_first_non_null(candidates, "lat"),
                lon=_first_non_null(candidates, "lon"),
            )
        )
    return merged


def _first_non_null(records: list[RecordPoint], attr: str):
    for r in records:
        value = getattr(r, attr)
        if value is not None:
            return value
    return None
