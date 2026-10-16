import math
from array import array
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
    sessions = list(fitfile.get_messages("session"))
    if not sessions:
        raise ValueError(f"FIT file for activity {activity_id} has no session message")
    session = sessions[0]
    session_sport = session.get_value("sport") or "other"

    local_start = datetime.fromisoformat(start_time_local)
    offset = _utc_offset(session.get_value("start_time"), local_start)

    if len(sessions) > 1:
        summary = _multisport_summary(sessions, activity_id, name, start_time_local)
    else:
        duration_s = session.get_value("total_elapsed_time") or 0.0
        distance_m = session.get_value("total_distance") or 0.0
        summary = ActivitySummary(
            activity_id=activity_id,
            name=name,
            sport=session_sport,
            start_time_local=start_time_local,
            duration_s=duration_s,
            distance_m=distance_m,
            avg_speed_mps=_resolve_speed(session, distance_m, duration_s),
            avg_hr=session.get_value("avg_heart_rate"),
            avg_power=session.get_value("avg_power"),
            total_ascent_m=session.get_value("total_ascent"),
        )

    raw_laps = []
    for lap_msg in fitfile.get_messages("lap"):
        lap_utc_start = lap_msg.get_value("start_time")
        lap_local_start = (
            (lap_utc_start.replace(tzinfo=None) + offset).isoformat() if lap_utc_start else ""
        )
        lap_duration_s = lap_msg.get_value("total_elapsed_time") or 0.0
        lap_distance_m = lap_msg.get_value("total_distance") or 0.0
        raw_laps.append(
            {
                "start_time_local": lap_local_start,
                "duration_s": lap_duration_s,
                "distance_m": lap_distance_m,
                "avg_speed_mps": _resolve_speed(lap_msg, lap_distance_m, lap_duration_s),
                "avg_hr": lap_msg.get_value("avg_heart_rate"),
                "avg_power": lap_msg.get_value("avg_power"),
                "total_ascent_m": lap_msg.get_value("total_ascent"),
                "total_descent_m": lap_msg.get_value("total_descent"),
                "intensity": lap_msg.get_value("intensity"),
                "sport": lap_msg.get_value("sport") or session_sport,
            }
        )

    raw_laps = _merge_autolap_splits(raw_laps)
    # With no structured workout the watch stamps every lap 'active' (plain runs, races,
    # multisport legs) — that says nothing about which laps were efforts, so only trust the
    # intensity as an interval marker when it actually varies across the activity.
    structured = len({raw["intensity"] for raw in raw_laps}) > 1

    laps = [
        Lap(
            lap_index=lap_index,
            start_time_local=raw["start_time_local"],
            duration_s=raw["duration_s"],
            distance_m=raw["distance_m"],
            avg_speed_mps=raw["avg_speed_mps"],
            avg_hr=raw["avg_hr"],
            avg_power=raw["avg_power"],
            total_ascent_m=raw["total_ascent_m"],
            total_descent_m=raw["total_descent_m"],
            is_interval=structured and raw["intensity"] in INTERVAL_INTENSITIES,
            sport=raw["sport"],
        )
        for lap_index, raw in enumerate(raw_laps, start=1)
    ]

    return summary, laps


MULTISPORT = "multisport"


def _multisport_summary(sessions: list, activity_id: str, name: str, start_time_local: str) -> ActivitySummary:
    """One summary for a multi-session FIT file (triathlon etc.: swim, T1, bike, T2, run each
    recorded as its own session). Duration/distance/ascent are summed and HR is
    time-weighted; average speed and power are left empty since a single figure mixing swim,
    bike and run means nothing — the per-leg values live on the laps (see Lap.sport)."""
    duration_s = sum(s.get_value("total_elapsed_time") or 0.0 for s in sessions)
    distance_m = sum(s.get_value("total_distance") or 0.0 for s in sessions)
    hr_weighted = [
        (s.get_value("avg_heart_rate"), s.get_value("total_elapsed_time") or 0.0)
        for s in sessions
        if s.get_value("avg_heart_rate") is not None
    ]
    hr_time = sum(t for _, t in hr_weighted)
    ascents = [s.get_value("total_ascent") for s in sessions if s.get_value("total_ascent") is not None]
    return ActivitySummary(
        activity_id=activity_id,
        name=name,
        sport=MULTISPORT,
        start_time_local=start_time_local,
        duration_s=duration_s,
        distance_m=distance_m,
        avg_hr=round(sum(hr * t for hr, t in hr_weighted) / hr_time) if hr_time else None,
        total_ascent_m=sum(ascents) if ascents else None,
    )


def _weighted_avg(
    a_value: float | None, a_weight: float, b_value: float | None, b_weight: float
) -> float | None:
    if a_value is None:
        return b_value
    if b_value is None:
        return a_value
    total_weight = a_weight + b_weight
    if not total_weight:
        return (a_value + b_value) / 2
    return (a_value * a_weight + b_value * b_weight) / total_weight


def _round_optional(value: float | None) -> int | None:
    return None if value is None else round(value)


def _sum_optional(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def _merge_autolap_splits(raw_laps: list[dict]) -> list[dict]:
    """Merges consecutive raw device laps that share the same intensity — but only for
    activities where intensity actually varies (a structured workout was followed), never for
    a plain run/ride where every lap is uniformly 'active' (or uniformly anything else): there,
    each lap boundary is a deliberate Auto Lap-by-distance split the user wants to keep (e.g.
    per-km splits on a marathon), and merging them would collapse the whole activity into one
    lap.

    Garmin's distance-based Auto Lap (most commonly "every 1 km", a very common device default)
    fires purely on elapsed distance, independent of a workout's own structure — so within a
    structured workout it can trigger mid-effort and chop what the device itself still
    considers one continuous phase (e.g. a planned 1800m rep) into multiple separate lap
    entries with no change in intensity between them, unlike a genuinely distinct phase change
    (warmup -> active, active -> rest, ...), which always does show up as an intensity change
    in this device's lap stream. That gives a reliable signal — merge consecutive same-intensity
    laps — without needing to guess the device's exact Auto Lap distance setting (1km, 1 mile,
    ... it varies by user/device configuration). Verified against real recorded activities:
    every legitimate distinct rep in a structured workout is separated by an intensity change;
    only Auto Lap-interrupted phases ever show two consecutive laps of the identical intensity.
    """
    if len({raw["intensity"] for raw in raw_laps}) <= 1:
        return raw_laps

    merged: list[dict] = []
    for raw in raw_laps:
        if merged and merged[-1]["intensity"] == raw["intensity"] and merged[-1]["sport"] == raw["sport"]:
            prev = merged[-1]
            duration_s = prev["duration_s"] + raw["duration_s"]
            distance_m = prev["distance_m"] + raw["distance_m"]
            merged[-1] = {
                "start_time_local": prev["start_time_local"],
                "duration_s": duration_s,
                "distance_m": distance_m,
                "avg_speed_mps": distance_m / duration_s if duration_s else None,
                "avg_hr": _round_optional(
                    _weighted_avg(prev["avg_hr"], prev["duration_s"], raw["avg_hr"], raw["duration_s"])
                ),
                "avg_power": _weighted_avg(
                    prev["avg_power"], prev["duration_s"], raw["avg_power"], raw["duration_s"]
                ),
                "total_ascent_m": _sum_optional(prev["total_ascent_m"], raw["total_ascent_m"]),
                "total_descent_m": _sum_optional(prev["total_descent_m"], raw["total_descent_m"]),
                "intensity": raw["intensity"],
                "sport": raw["sport"],
            }
        else:
            merged.append(dict(raw))
    return merged


def session_utc_offset(fit_bytes: bytes, start_time_local: str) -> timedelta:
    """UTC->local offset for this FIT file, for use with parse_records()/split_by_*()."""
    fitfile = fitparse.FitFile(fit_bytes)
    session = next(fitfile.get_messages("session"), None)
    utc_start = session.get_value("start_time") if session is not None else None
    return _utc_offset(utc_start, datetime.fromisoformat(start_time_local))


def _pool_length_distance_points(fitfile) -> list[tuple[datetime, float]]:
    """(utc_time, cumulative_distance_m) breakpoints built from a pool swim's `length`
    messages — the only place a pool swim stores distance (its `record` messages have none,
    since there's no GPS). Each active length adds one pool length between its start and end;
    idle lengths (rest at the wall) add nothing."""
    session = next(fitfile.get_messages("session"), None)
    pool_length = session.get_value("pool_length") if session is not None else None
    if not pool_length:
        return []
    points: list[tuple[datetime, float]] = []
    distance = 0.0
    for length in fitfile.get_messages("length"):
        start = length.get_value("start_time")
        elapsed = length.get_value("total_elapsed_time")
        if start is None or elapsed is None:
            continue
        start = start.replace(tzinfo=None)
        points.append((start, distance))
        if length.get_value("length_type") == "active":
            distance += pool_length
        points.append((start + timedelta(seconds=elapsed), distance))
    return points


def _distance_at(points: list[tuple[datetime, float]], t: datetime) -> float:
    """Cumulative distance at time `t`, linear within a length (constant speed assumed)."""
    if t <= points[0][0]:
        return points[0][1]
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if t <= t1:
            span = (t1 - t0).total_seconds()
            return d1 if span <= 0 else d0 + (d1 - d0) * (t - t0).total_seconds() / span
    return points[-1][1]


def parse_records(fit_bytes: bytes) -> list[RecordPoint]:
    """Extract the per-record (typically per-second) raw data stream from a FIT file.

    Pool swims get a synthesized `distance_m` from their per-length data, so splitting by
    distance and interval phase-splitting work on them like on any other activity.
    """
    fitfile = fitparse.FitFile(fit_bytes)
    records = []
    for rec in fitfile.get_messages("record"):
        timestamp = rec.get_value("timestamp")
        if timestamp is None:
            continue
        speed = rec.get_value("speed")
        if speed is None:
            speed = rec.get_value("enhanced_speed")
        altitude = rec.get_value("enhanced_altitude")
        if altitude is None:
            altitude = rec.get_value("altitude")
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
                altitude_m=altitude,
            )
        )

    if records and all(r.distance_m is None for r in records):
        points = _pool_length_distance_points(fitfile)
        if points:
            for rec in records:
                rec.distance_m = _distance_at(points, rec.timestamp_utc)
    return records


# Bump whenever parse_records() output changes, so stale caches are rebuilt rather than reused.
RECORDS_CACHE_VERSION = 1
_CACHED_FIELDS = ("distance_m", "heart_rate", "power", "speed_mps", "lat", "lon", "altitude_m")
_EPOCH = datetime(1970, 1, 1)


def _records_cache_key(activity_id: str) -> str:
    """Identifies the exact inputs a cached stream was built from: the FIT file (size and
    modification time), the merged-duplicates stream if there is one, and the cache format."""
    fit = storage.fit_path(activity_id)
    merged = storage.MERGED_RECORDS_DIR / f"{activity_id}.json"
    parts = [str(RECORDS_CACHE_VERSION)]
    for path in (fit, merged):
        stat = path.stat() if path.exists() else None
        parts.append(f"{stat.st_size}:{stat.st_mtime_ns}" if stat else "-")
    return "|".join(parts)


def _encode_stream(records: list[RecordPoint], session_start_utc: datetime | None) -> dict:
    """Column-wise arrays of doubles (NaN = no value): ~10x smaller and far faster to load
    than per-record objects."""
    nan = float("nan")
    columns = {"timestamp": array("d", ((r.timestamp_utc - _EPOCH).total_seconds() for r in records)).tobytes()}
    for name in _CACHED_FIELDS:
        columns[name] = array("d", (nan if (v := getattr(r, name)) is None else float(v) for r in records)).tobytes()
    return {"session_start_utc": session_start_utc, "columns": columns}


def _decode_stream(payload: dict) -> list[RecordPoint]:
    columns = {}
    for name, raw in payload["columns"].items():
        values = array("d")
        values.frombytes(raw)
        columns[name] = values
    heart_rates = [None if math.isnan(v) else int(v) for v in columns["heart_rate"]]
    other = {name: [None if math.isnan(v) else v for v in columns[name]] for name in _CACHED_FIELDS if name != "heart_rate"}
    return [
        RecordPoint(
            timestamp_utc=_EPOCH + timedelta(seconds=ts),
            heart_rate=heart_rates[i],
            **{name: other[name][i] for name in other},
        )
        for i, ts in enumerate(columns["timestamp"])
    ]


def _session_start(fit_bytes: bytes) -> datetime | None:
    session = next(fitparse.FitFile(fit_bytes).get_messages("session"), None)
    start = session.get_value("start_time") if session is not None else None
    return start.replace(tzinfo=None) if start else None


def _load_stream(activity_id: str, fit_bytes: bytes | None = None) -> tuple[list[RecordPoint], datetime | None]:
    key = _records_cache_key(activity_id)
    cached = storage.load_records_cache(activity_id)
    if cached is not None and cached.get("key") == key:
        return _decode_stream(cached["stream"]), cached["stream"]["session_start_utc"]
    if fit_bytes is None:
        fit_bytes = storage.fit_path(activity_id).read_bytes()
    merged = storage.load_merged_records(activity_id)
    records = merged if merged is not None else parse_records(fit_bytes)
    session_start = _session_start(fit_bytes)
    storage.save_records_cache(activity_id, {"key": key, "stream": _encode_stream(records, session_start)})
    return records, session_start


def load_records(activity_id: str, fit_bytes: bytes | None = None) -> list[RecordPoint]:
    """The best available record stream for an activity: a persisted merged stream (see
    dedupe.py — combining a duplicate-device recording of the same session) if one exists,
    else parsed from its own FIT file. Parsing a long activity takes seconds (fitparse is
    pure Python), so the result is cached on disk (storage.RECORDS_CACHE_DIR) and reused until
    the FIT file, the merged stream or the parser changes — sync and reprocess fill the cache
    as a side effect (route matching loads every activity's records)."""
    return _load_stream(activity_id, fit_bytes)[0]


def load_utc_offset(activity_id: str, start_time_local: str) -> timedelta:
    """Same as session_utc_offset(), but from the records cache instead of a full re-parse."""
    start = _load_stream(activity_id)[1]
    return _utc_offset(start, datetime.fromisoformat(start_time_local))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


ELEVATION_HYSTERESIS_M = 1.0


def _ascent_descent(bucket: list[RecordPoint]) -> tuple[float | None, float | None]:
    """Total climb and drop over a record bucket. Raw per-second altitude jitters by tens of
    centimetres, so summing every change roughly triples the true climb; a change only
    counts once altitude has moved ELEVATION_HYSTERESIS_M from the last counted point. 1 m
    was calibrated against the watch's own per-lap totals on real activities (it lands within
    a few metres of them, e.g. 38 vs 36 m and 159 vs 147 m over a marathon)."""
    up = down = 0.0
    ref = None
    for rec in bucket:
        if rec.altitude_m is None:
            continue
        if ref is None:
            ref = rec.altitude_m
            continue
        change = rec.altitude_m - ref
        if abs(change) >= ELEVATION_HYSTERESIS_M:
            if change > 0:
                up += change
            else:
                down -= change
            ref = rec.altitude_m
    if ref is None:
        return None, None
    return up, down


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
    ascent, descent = _ascent_descent(bucket)
    return Lap(
        lap_index=lap_index,
        start_time_local=(start.timestamp_utc + offset).isoformat(),
        duration_s=duration_s,
        distance_m=distance_m,
        avg_speed_mps=distance_m / duration_s if duration_s else None,
        avg_hr=round(_mean(hr_values)) if hr_values else None,
        avg_power=_mean(power_values),
        is_interval=False,
        total_ascent_m=ascent,
        total_descent_m=descent,
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
        altitude_m=lerp(prev.altitude_m, nxt.altitude_m),
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


def _fold_negligible_tail(
    segments: list[list[RecordPoint]], min_tail: float, by: str = "distance"
) -> None:
    """Mutates `segments` in place, folding a final segment shorter than `min_tail` (metres,
    or seconds when `by="time"`) into the one before it.

    Without this, a segmenting pass's very last segment can end up a near-zero sliver with no
    real signal in it — e.g. `split_by_distance` interpolates a boundary at exactly the target
    distance, and when a lap's total distance happens to be an (near-)exact multiple of the
    split size, that boundary lands right on the lap's last real record, leaving a trailing
    "segment" that's just [boundary, that same record] a fraction of a meter/second apart.
    """
    if len(segments) < 2:
        return
    tail = segments[-1]
    if by == "time":
        tail_size = (tail[-1].timestamp_utc - tail[0].timestamp_utc).total_seconds()
    else:
        tail_size = (tail[-1].distance_m or 0.0) - (tail[0].distance_m or 0.0)
    if tail_size < min_tail:
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
    _fold_negligible_tail(segments, min_tail=max(1.0, meters * 0.05))

    return [_bucket_to_lap(segment, i, offset) for i, segment in enumerate(segments, start=1)]


def split_by_time(records: list[RecordPoint], seconds: float, offset: timedelta) -> list[Lap]:
    segments: list[list[RecordPoint]] = []
    bucket: list[RecordPoint] = []
    bucket_start_time = None
    for rec in records:
        if not bucket:
            bucket_start_time = rec.timestamp_utc
        elif (rec.timestamp_utc - bucket_start_time).total_seconds() >= seconds:
            # Same shared interpolated boundary as split_by_distance, so time splits of an
            # interval also sum exactly to the interval's own duration.
            boundary = _interpolate_at_time(bucket[-1], rec, bucket_start_time + timedelta(seconds=seconds))
            bucket.append(boundary)
            segments.append(bucket)
            bucket = [boundary]
            bucket_start_time = boundary.timestamp_utc
        bucket.append(rec)
    if len(bucket) > 1:
        segments.append(bucket)

    _fold_negligible_tail(segments, min_tail=max(1.0, seconds * 0.05), by="time")

    return [_bucket_to_lap(segment, i, offset) for i, segment in enumerate(segments, start=1)]


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
    _fold_negligible_tail(segments, min_tail=min_lap_distance_m)

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
