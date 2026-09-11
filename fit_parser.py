from datetime import datetime, timedelta

import fitparse

import routes
from models import ActivitySummary, Lap, RecordPoint

INTERVAL_INTENSITIES = {"interval", "active"}
SEMICIRCLE_TO_DEGREES = 180 / 2**31


def _resolve_speed(msg, distance_m: float | None, duration_s: float | None) -> float | None:
    speed = msg.get_value("avg_speed")
    if speed is not None:
        return speed
    speed = msg.get_value("enhanced_avg_speed")
    if speed is not None:
        return speed
    if distance_m and duration_s:
        return distance_m / duration_s
    return None


def _utc_offset(utc_start: datetime | None, local_start: datetime) -> timedelta:
    if utc_start is None:
        return timedelta(0)
    return local_start - utc_start.replace(tzinfo=None)


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

    local_start = datetime.fromisoformat(start_time_local)
    offset = _utc_offset(session.get_value("start_time"), local_start)

    duration_s = session.get_value("total_elapsed_time") or 0.0
    distance_m = session.get_value("total_distance") or 0.0

    summary = ActivitySummary(
        activity_id=activity_id,
        name=name,
        sport=session.get_value("sport") or "other",
        start_time_local=start_time_local,
        duration_s=duration_s,
        distance_m=distance_m,
        avg_speed_mps=_resolve_speed(session, distance_m, duration_s),
        avg_hr=session.get_value("avg_heart_rate"),
        avg_power=session.get_value("avg_power"),
        total_ascent_m=session.get_value("total_ascent"),
    )

    laps = []
    for lap_index, lap_msg in enumerate(fitfile.get_messages("lap"), start=1):
        lap_utc_start = lap_msg.get_value("start_time")
        lap_local_start = (
            (lap_utc_start.replace(tzinfo=None) + offset).isoformat() if lap_utc_start else ""
        )
        lap_duration_s = lap_msg.get_value("total_elapsed_time") or 0.0
        lap_distance_m = lap_msg.get_value("total_distance") or 0.0
        intensity = lap_msg.get_value("intensity")
        laps.append(
            Lap(
                lap_index=lap_index,
                start_time_local=lap_local_start,
                duration_s=lap_duration_s,
                distance_m=lap_distance_m,
                avg_speed_mps=_resolve_speed(lap_msg, lap_distance_m, lap_duration_s),
                avg_hr=lap_msg.get_value("avg_heart_rate"),
                avg_power=lap_msg.get_value("avg_power"),
                is_interval=intensity in INTERVAL_INTENSITIES,
            )
        )

    return summary, laps


def session_utc_offset(fit_bytes: bytes, start_time_local: str) -> timedelta:
    """UTC->local offset for this FIT file, for use with parse_records()/split_by_*()."""
    fitfile = fitparse.FitFile(fit_bytes)
    session = next(fitfile.get_messages("session"), None)
    utc_start = session.get_value("start_time") if session is not None else None
    return _utc_offset(utc_start, datetime.fromisoformat(start_time_local))


def parse_records(fit_bytes: bytes) -> list[RecordPoint]:
    """Extract the per-record (typically per-second) raw data stream from a FIT file."""
    fitfile = fitparse.FitFile(fit_bytes)
    records = []
    for rec in fitfile.get_messages("record"):
        timestamp = rec.get_value("timestamp")
        if timestamp is None:
            continue
        speed = rec.get_value("speed")
        if speed is None:
            speed = rec.get_value("enhanced_speed")
        lat_semicircles = rec.get_value("position_lat")
        lon_semicircles = rec.get_value("position_long")
        records.append(
            RecordPoint(
                timestamp_utc=timestamp.replace(tzinfo=None),
                distance_m=rec.get_value("distance"),
                heart_rate=rec.get_value("heart_rate"),
                power=rec.get_value("power"),
                speed_mps=speed,
                lat=lat_semicircles * SEMICIRCLE_TO_DEGREES if lat_semicircles is not None else None,
                lon=lon_semicircles * SEMICIRCLE_TO_DEGREES if lon_semicircles is not None else None,
            )
        )
    return records


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bucket_to_lap(bucket: list[RecordPoint], lap_index: int, offset: timedelta) -> Lap:
    start = bucket[0]
    end = bucket[-1]
    duration_s = (end.timestamp_utc - start.timestamp_utc).total_seconds()
    distance_m = (
        end.distance_m - start.distance_m
        if start.distance_m is not None and end.distance_m is not None
        else 0.0
    )
    hr_values = [r.heart_rate for r in bucket if r.heart_rate is not None]
    power_values = [r.power for r in bucket if r.power is not None]
    return Lap(
        lap_index=lap_index,
        start_time_local=(start.timestamp_utc + offset).isoformat(),
        duration_s=duration_s,
        distance_m=distance_m,
        avg_speed_mps=distance_m / duration_s if duration_s else None,
        avg_hr=round(_mean(hr_values)) if hr_values else None,
        avg_power=_mean(power_values),
        is_interval=False,
    )


def split_by_distance(records: list[RecordPoint], meters: float, offset: timedelta) -> list[Lap]:
    laps = []
    bucket: list[RecordPoint] = []
    bucket_start_distance = None
    for rec in records:
        if not bucket:
            bucket_start_distance = rec.distance_m
        bucket.append(rec)
        if (
            bucket_start_distance is not None
            and rec.distance_m is not None
            and rec.distance_m - bucket_start_distance >= meters
        ):
            laps.append(_bucket_to_lap(bucket, len(laps) + 1, offset))
            bucket = []
    if bucket:
        laps.append(_bucket_to_lap(bucket, len(laps) + 1, offset))
    return laps


def split_by_time(records: list[RecordPoint], seconds: float, offset: timedelta) -> list[Lap]:
    laps = []
    bucket: list[RecordPoint] = []
    bucket_start_time = None
    for rec in records:
        if not bucket:
            bucket_start_time = rec.timestamp_utc
        bucket.append(rec)
        if (rec.timestamp_utc - bucket_start_time).total_seconds() >= seconds:
            laps.append(_bucket_to_lap(bucket, len(laps) + 1, offset))
            bucket = []
    if bucket:
        laps.append(_bucket_to_lap(bucket, len(laps) + 1, offset))
    return laps


def detect_physical_laps(
    records: list[RecordPoint],
    offset: timedelta,
    close_radius_m: float = 30.0,
    min_lap_distance_m: float = 200.0,
) -> list[Lap] | None:
    """Detect repeated physical loops (e.g. laps around a park) from GPS position alone,
    independent of the device's own lap markers.

    The activity's first GPS-fixed record is the anchor. A lap boundary is marked each time
    the track comes back within `close_radius_m` of the anchor, provided at least
    `min_lap_distance_m` has been covered since the last boundary (only the first point of
    each such "visit" counts, so lingering near the start/finish doesn't create duplicate
    boundaries). Returns None if fewer than 2 segments result — e.g. an out-and-back or
    point-to-point course only ever nears its start once, at the very end.

    Known limitations: a poor GPS fix on the anchor record skews every subsequent check;
    loops shorter than `min_lap_distance_m` won't be detected; a course that happens to pass
    near the start mid-loop (not a real lap) can produce a false split. Acceptable for a
    personal tool, not worth extra heuristics.
    """
    fixed = [r for r in records if r.lat is not None and r.lon is not None]
    if len(fixed) < 20:
        return None

    anchor = fixed[0]
    boundaries = [0]
    last_boundary_distance = anchor.distance_m or 0.0
    inside_radius = False
    for i, rec in enumerate(records):
        if rec.lat is None or rec.lon is None:
            continue
        dist_to_anchor = routes.haversine_m(anchor.lat, anchor.lon, rec.lat, rec.lon)
        if dist_to_anchor <= close_radius_m:
            if not inside_radius:
                inside_radius = True
                covered = (rec.distance_m or last_boundary_distance) - last_boundary_distance
                if i != boundaries[-1] and covered >= min_lap_distance_m:
                    boundaries.append(i)
                    last_boundary_distance = rec.distance_m or last_boundary_distance
        else:
            inside_radius = False

    if len(boundaries) < 2:
        return None

    segments = []
    for start, end in zip(boundaries, boundaries[1:] + [len(records)]):
        segments.append(records[start:end])
    if len(segments) < 2:
        return None

    return [_bucket_to_lap(segment, i, offset) for i, segment in enumerate(segments, start=1)]


def lap_record_slice(records: list[RecordPoint], lap: Lap, offset: timedelta) -> list[RecordPoint]:
    """The subset of a full activity's record stream falling inside one lap's time window."""
    if not lap.start_time_local:
        return []
    utc_start = datetime.fromisoformat(lap.start_time_local) - offset
    utc_end = utc_start + timedelta(seconds=lap.duration_s)
    return [r for r in records if utc_start <= r.timestamp_utc <= utc_end]


def lap_x_values(lap_records: list[RecordPoint], lap: Lap, mode: str) -> list[float]:
    """X-axis values for one lap's records, aligned to a common origin per `mode`:
    "time" (seconds since the lap's first record), "distance" (meters since its
    first record's cumulative distance), or "percent" (time elapsed as a % of
    the lap's total duration)."""
    if not lap_records:
        return []
    if mode == "time":
        t0 = lap_records[0].timestamp_utc
        return [(r.timestamp_utc - t0).total_seconds() for r in lap_records]
    if mode == "distance":
        d0 = lap_records[0].distance_m or 0.0
        return [(r.distance_m or d0) - d0 for r in lap_records]
    if mode == "percent":
        t0 = lap_records[0].timestamp_utc
        total = lap.duration_s or 1.0
        return [(r.timestamp_utc - t0).total_seconds() / total * 100 for r in lap_records]
    raise ValueError(f"unknown x-axis mode: {mode}")


def metric_series(
    lap_records: list[RecordPoint], x_values: list[float], attr: str
) -> tuple[list[float], list[float]]:
    """(x, y) pairs for one metric attribute, dropping points where it's None."""
    xs, ys = [], []
    for x, rec in zip(x_values, lap_records):
        y = getattr(rec, attr)
        if y is not None:
            xs.append(x)
            ys.append(y)
    return xs, ys
