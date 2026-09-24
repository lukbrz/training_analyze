from dataclasses import dataclass
from datetime import datetime

PACE_SPORTS = {"running", "walking", "hiking"}


@dataclass
class ActivitySummary:
    activity_id: str
    name: str
    sport: str
    start_time_local: str
    duration_s: float
    distance_m: float
    avg_speed_mps: float | None = None
    avg_hr: int | None = None
    avg_power: float | None = None
    total_ascent_m: float | None = None
    route_id: str | None = None


@dataclass
class Lap:
    lap_index: int
    start_time_local: str
    duration_s: float
    distance_m: float
    avg_speed_mps: float | None = None
    avg_hr: int | None = None
    avg_power: float | None = None
    is_interval: bool = False


@dataclass
class RecordPoint:
    timestamp_utc: datetime
    distance_m: float | None
    heart_rate: int | None = None
    power: float | None = None
    speed_mps: float | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass
class ZoneTime:
    zone_number: int
    low_boundary: float | None
    high_boundary: float | None
    seconds: float


@dataclass
class RouteSignature:
    points: list[list[float]]  # resampled [lat, lon] pairs (lists, not tuples — JSON round-trip)
    distance_m: float


@dataclass
class Route:
    route_id: str
    name: str
    sport: str
    signature: RouteSignature
    place: str | None = None


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_duration_tenths(seconds: float) -> str:
    """Like format_duration, but keeping one decimal place — for interval laps and their
    phase splits, where FIT's native sub-second precision (and, for phase splits, distance
    interpolation — see fit_parser._interpolate_at_distance) actually means something: it's
    what lets a rep's phase durations visibly sum back to the rep's own duration instead of
    silently rounding away a second or two."""
    total_tenths = int(round(seconds * 10))
    whole_secs, tenths = divmod(total_tenths, 10)
    hours, rem = divmod(whole_secs, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{tenths}"
    return f"{minutes}:{secs:02d}.{tenths}"


def format_distance(meters: float) -> str:
    return f"{meters / 1000:.2f} km"


def format_distance_m(meters: float) -> str:
    return f"{meters:.0f} m"


def format_lap_distance(sport: str, meters: float) -> str:
    return format_distance_m(meters) if sport in PACE_SPORTS else format_distance(meters)


def format_speed_or_pace(sport: str, avg_speed_mps: float | None) -> str:
    if not avg_speed_mps:
        return "–"
    if sport in PACE_SPORTS:
        pace_s_per_km = 1000 / avg_speed_mps
        minutes, secs = divmod(int(round(pace_s_per_km)), 60)
        return f"{minutes}:{secs:02d} /km"
    return f"{avg_speed_mps * 3.6:.1f} km/h"


def format_hr(hr: int | None) -> str:
    return str(hr) if hr else "–"


def format_power(watts: float | None) -> str:
    return f"{watts:.0f} W" if watts else "–"
