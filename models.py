from dataclasses import dataclass

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
    max_hr: int | None = None
    total_ascent_m: float | None = None


@dataclass
class Lap:
    lap_index: int
    start_time_local: str
    duration_s: float
    distance_m: float
    avg_speed_mps: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_distance(meters: float) -> str:
    return f"{meters / 1000:.2f} km"


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
