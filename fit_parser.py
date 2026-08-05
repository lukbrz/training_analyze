from datetime import datetime, timedelta

import fitparse

from models import ActivitySummary, Lap


def parse(
    fit_bytes: bytes, activity_id: str, name: str, start_time_local: str
) -> tuple[ActivitySummary, list[Lap]]:
    """Parse a downloaded FIT file into an activity summary and its laps.

    `start_time_local` is the activity's local start time as reported by
    Garmin Connect's activity list (already timezone-correct); it's used
    both as the summary's start time and to translate the FIT file's UTC
    lap timestamps into local time via a fixed offset.
    """
    fitfile = fitparse.FitFile(fit_bytes)
    session = next(fitfile.get_messages("session"), None)
    if session is None:
        raise ValueError(f"FIT file for activity {activity_id} has no session message")

    utc_start = session.get_value("start_time")
    local_start = datetime.fromisoformat(start_time_local)
    offset = local_start - utc_start.replace(tzinfo=None) if utc_start else timedelta(0)

    summary = ActivitySummary(
        activity_id=activity_id,
        name=name,
        sport=session.get_value("sport") or "other",
        start_time_local=start_time_local,
        duration_s=session.get_value("total_elapsed_time") or 0.0,
        distance_m=session.get_value("total_distance") or 0.0,
        avg_speed_mps=session.get_value("avg_speed"),
        avg_hr=session.get_value("avg_heart_rate"),
        max_hr=session.get_value("max_heart_rate"),
        total_ascent_m=session.get_value("total_ascent"),
    )

    laps = []
    for lap_index, lap_msg in enumerate(fitfile.get_messages("lap"), start=1):
        lap_utc_start = lap_msg.get_value("start_time")
        lap_local_start = (
            (lap_utc_start.replace(tzinfo=None) + offset).isoformat() if lap_utc_start else ""
        )
        laps.append(
            Lap(
                lap_index=lap_index,
                start_time_local=lap_local_start,
                duration_s=lap_msg.get_value("total_elapsed_time") or 0.0,
                distance_m=lap_msg.get_value("total_distance") or 0.0,
                avg_speed_mps=lap_msg.get_value("avg_speed"),
                avg_hr=lap_msg.get_value("avg_heart_rate"),
                max_hr=lap_msg.get_value("max_heart_rate"),
            )
        )

    return summary, laps
