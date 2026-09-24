from datetime import datetime, timedelta

import fitparse

import routes
import storage
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


def load_records(activity_id: str, fit_bytes: bytes) -> list[RecordPoint]:
    """The best available record stream for an activity: a persisted merged stream (see
    dedupe.py — combining a duplicate-device recording of the same session) if one
    exists, else freshly parsed from its own raw FIT bytes."""
    merged = storage.load_merged_records(activity_id)
    if merged is not None:
        return merged
    return parse_records(fit_bytes)


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


def _lerp_record(prev: RecordPoint, nxt: RecordPoint, frac: float) -> RecordPoint:
    """A synthetic record linearly interpolated between two real samples, `frac` of the way
    (by time) from `prev` to `nxt`. Every field — not just the one a caller cares about — is
    interpolated so the point behaves like a real sample anywhere it's used (bucketed into a
    lap average, sliced into a chart series, etc)."""
    frac = min(max(frac, 0.0), 1.0)

    def lerp(a: float | None, b: float | None) -> float | None:
        return a + frac * (b - a) if a is not None and b is not None else None

    return RecordPoint(
        timestamp_utc=prev.timestamp_utc + (nxt.timestamp_utc - prev.timestamp_utc) * frac,
        distance_m=lerp(prev.distance_m, nxt.distance_m),
        heart_rate=lerp(prev.heart_rate, nxt.heart_rate),
        power=lerp(prev.power, nxt.power),
        speed_mps=lerp(prev.speed_mps, nxt.speed_mps),
        lat=lerp(prev.lat, nxt.lat),
        lon=lerp(prev.lon, nxt.lon),
    )


def _interpolate_at_distance(prev: RecordPoint, nxt: RecordPoint, target_distance: float) -> RecordPoint:
    """A synthetic record at exactly `target_distance`, interpolated between the two real
    samples straddling it.

    Records are only ever a handful of meters and a second or more apart (more under Smart
    Recording's multi-second gaps), so snapping a distance-based split boundary to whichever
    real sample first reaches the target — as a naive bucketing pass would — silently drops up
    to one full sample interval of duration at every split point: the record that closes one
    bucket can't also open the next. That's small per split but adds up, and it means several
    splits of one lap never sum back to that lap's own (sub-second-precision, per the FIT spec)
    duration — which is exactly the "2 seconds missing" a user will notice comparing a
    300m-split interval against its marked parent lap. Interpolating a shared boundary point
    that belongs to both buckets fixes that: it's still an estimate (pace is assumed constant
    between the two straddling samples), but it's the closest available estimate of when the
    target distance was actually reached.
    """
    prev_distance, next_distance = prev.distance_m, nxt.distance_m
    if prev_distance is None or next_distance is None or next_distance <= prev_distance:
        frac = 0.0
    else:
        frac = (target_distance - prev_distance) / (next_distance - prev_distance)
    point = _lerp_record(prev, nxt, frac)
    point.distance_m = target_distance  # exact, rather than whatever the frac lerp rounds to
    return point


def _interpolate_at_time(prev: RecordPoint, nxt: RecordPoint, target_time: datetime) -> RecordPoint:
    """A synthetic record at exactly `target_time`, interpolated between the two real samples
    straddling it — the time-axis counterpart of `_interpolate_at_distance`, used by
    `lap_record_slice` so a lap's record slice reconstructs its full FIT-reported duration
    instead of silently truncating up to one sample interval at each edge."""
    total = (nxt.timestamp_utc - prev.timestamp_utc).total_seconds()
    frac = (target_time - prev.timestamp_utc).total_seconds() / total if total else 0.0
    point = _lerp_record(prev, nxt, frac)
    point.timestamp_utc = target_time
    return point


def _fold_negligible_tail(segments: list[list[RecordPoint]], min_tail_distance: float) -> None:
    """Mutates `segments` in place, folding a final segment shorter than `min_tail_distance`
    into the one before it.

    Without this, a segmenting pass's very last segment can end up a near-zero sliver with no
    real signal in it — e.g. `split_by_distance` interpolates a boundary at exactly the target
    distance, and when a lap's total distance happens to be an (near-)exact multiple of the
    split size, that boundary lands right on the lap's last real record, leaving a trailing
    "segment" that's just [boundary, that same record] a fraction of a meter/second apart.
    """
    if len(segments) < 2:
        return
    tail = segments[-1]
    tail_distance = (tail[-1].distance_m or 0.0) - (tail[0].distance_m or 0.0)
    if tail_distance < min_tail_distance:
        segments[-2] = segments[-2] + tail
        segments.pop()


def split_by_distance(records: list[RecordPoint], meters: float, offset: timedelta) -> list[Lap]:
    segments: list[list[RecordPoint]] = []
    bucket: list[RecordPoint] = []
    bucket_start_distance = None
    for rec in records:
        if not bucket:
            bucket_start_distance = rec.distance_m
        elif (
            bucket_start_distance is not None
            and rec.distance_m is not None
            and rec.distance_m - bucket_start_distance >= meters
        ):
            boundary = _interpolate_at_distance(bucket[-1], rec, bucket_start_distance + meters)
            bucket.append(boundary)
            segments.append(bucket)
            bucket = [boundary]
            bucket_start_distance = boundary.distance_m
        bucket.append(rec)
    if len(bucket) > 1:
        segments.append(bucket)

    # A genuine final partial split (the lap's distance doesn't divide evenly by `meters`) is
    # usually close to a full split and should stand on its own — only fold away a sliver that
    # couldn't be a meaningful phase on its own.
    _fold_negligible_tail(segments, min_tail_distance=max(1.0, meters * 0.05))

    return [_bucket_to_lap(segment, i, offset) for i, segment in enumerate(segments, start=1)]


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


DEFAULT_CLOSE_RADIUS_M = 75.0
DEFAULT_MIN_LAP_DISTANCE_M = 150.0
MAX_ANCHOR_CANDIDATES = 400


def _boundaries_for_anchor(
    records: list[RecordPoint], anchor_lat: float, anchor_lon: float,
    close_radius_m: float, min_lap_distance_m: float,
) -> list[int]:
    """Record indices where `records` first comes within `close_radius_m` of (anchor_lat,
    anchor_lon), each at least `min_lap_distance_m` (by cumulative distance) after the last —
    i.e. how many times the track "closes a loop" back to this one point. Index 0 always
    counts as the first boundary even though it isn't itself a return to the anchor."""
    boundaries = [0]
    last_boundary_distance = records[0].distance_m or 0.0
    inside_radius = False
    for i, rec in enumerate(records):
        if rec.lat is None or rec.lon is None:
            continue
        dist_to_anchor = routes.haversine_m(anchor_lat, anchor_lon, rec.lat, rec.lon)
        if dist_to_anchor <= close_radius_m:
            if not inside_radius:
                inside_radius = True
                covered = (rec.distance_m or last_boundary_distance) - last_boundary_distance
                if i != boundaries[-1] and covered >= min_lap_distance_m:
                    boundaries.append(i)
                    last_boundary_distance = rec.distance_m or last_boundary_distance
        else:
            inside_radius = False
    return boundaries


def detect_physical_laps(
    records: list[RecordPoint],
    offset: timedelta,
    close_radius_m: float = DEFAULT_CLOSE_RADIUS_M,
    min_lap_distance_m: float = DEFAULT_MIN_LAP_DISTANCE_M,
) -> list[Lap] | None:
    """Detect repeated physical loops (e.g. laps around a park) from GPS position alone,
    independent of the device's own lap markers.

    The loop's anchor point is searched for across the *whole* track, not assumed to be the
    recording's start: a real workout commonly has a non-looping warmup/cooldown leg (e.g.
    running from home to a park and back), so a loop that only exists partway through the
    activity would never be found by anchoring on the first few GPS fixes. Every candidate
    anchor (subsampled across the track — `MAX_ANCHOR_CANDIDATES` — since testing every single
    point is unnecessary and slow) is scored by how many times the track returns within
    `close_radius_m` of it, each such return at least `min_lap_distance_m` (by cumulative
    distance) after the last (only the first point of each "visit" counts, so lingering near
    a point doesn't create duplicate boundaries); the best-scoring anchor's boundaries become
    the lap splits. Returns None if the best anchor still yields fewer than 2 segments — e.g.
    an out-and-back or point-to-point course that never repeats any single point.

    `close_radius_m` defaults generously (75m) because consumer GPS accuracy commonly runs
    20-50m under tree cover or in urban canyons (e.g. park loops) — too tight a radius misses
    real loops entirely, which is the failure mode that matters most (silently detecting
    nothing beats a wrong split, but detecting nothing is worse than a caller-supplied
    override). Both parameters are also exposed to the UI (`LapsScreen`'s split-value input)
    so a miss on a real activity can be retried with a wider radius without a code change.
    Known limitations: loops shorter than `min_lap_distance_m` won't be detected; a course
    that happens to pass near some point mid-loop (not a real lap boundary) can produce a
    false split, and the anchor search only ever picks one loop location per activity, so an
    activity mixing two different loop courses only finds whichever recurs more. Acceptable
    for a personal tool, not worth extra heuristics.
    """
    fixed = [r for r in records if r.lat is not None and r.lon is not None]
    if len(fixed) < 20:
        return None

    step = max(1, len(records) // MAX_ANCHOR_CANDIDATES)
    best_boundaries = [0]
    for i in range(0, len(records), step):
        rec = records[i]
        if rec.lat is None or rec.lon is None:
            continue
        boundaries = _boundaries_for_anchor(records, rec.lat, rec.lon, close_radius_m, min_lap_distance_m)
        if len(boundaries) > len(best_boundaries):
            best_boundaries = boundaries

    if len(best_boundaries) < 2:
        return None

    segments = []
    for start, end in zip(best_boundaries, best_boundaries[1:] + [len(records)]):
        segments.append(records[start:end])

    # Every segment except the last is guaranteed >= min_lap_distance_m by construction
    # (a boundary only gets added once that much has been covered). The last segment runs
    # to the end of the recording with no such guarantee — without this, a run that simply
    # *finishes* near where it started (extremely common) would masquerade as an extra lap.
    _fold_negligible_tail(segments, min_tail_distance=min_lap_distance_m)

    if len(segments) < 2:
        return None

    return [_bucket_to_lap(segment, i, offset) for i, segment in enumerate(segments, start=1)]


def lap_record_slice(records: list[RecordPoint], lap: Lap, offset: timedelta) -> list[RecordPoint]:
    """The subset of a full activity's record stream falling inside one lap's time window,
    with the exact start/end instants included as interpolated records (see
    `_interpolate_at_time`) when they fall between two real samples rather than on one."""
    if not lap.start_time_local:
        return []
    utc_start = datetime.fromisoformat(lap.start_time_local) - offset
    utc_end = utc_start + timedelta(seconds=lap.duration_s)

    start_idx = end_idx = None
    for i, rec in enumerate(records):
        if start_idx is None and rec.timestamp_utc >= utc_start:
            start_idx = i
        if rec.timestamp_utc <= utc_end:
            end_idx = i
        elif start_idx is not None:
            break
    if start_idx is None or end_idx is None or end_idx < start_idx:
        return []

    result = list(records[start_idx : end_idx + 1])
    if start_idx > 0 and result[0].timestamp_utc > utc_start:
        result.insert(0, _interpolate_at_time(records[start_idx - 1], result[0], utc_start))
    if end_idx + 1 < len(records) and result[-1].timestamp_utc < utc_end:
        result.append(_interpolate_at_time(result[-1], records[end_idx + 1], utc_end))
    return result


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
