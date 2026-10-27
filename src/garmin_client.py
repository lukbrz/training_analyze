import io
import zipfile
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from models import ZoneTime
from storage import SESSION_DIR

_ZONE_NUMBER_KEYS = ("zoneNumber", "zone_number", "zoneId", "zone")
_LOW_BOUNDARY_KEYS = ("zoneLowBoundary", "lowBoundary", "zoneMin", "minValue", "min")
_HIGH_BOUNDARY_KEYS = ("zoneHighBoundary", "highBoundary", "zoneMax", "maxValue", "max")
_SECONDS_KEYS = ("secsInZone", "secondsInZone", "timeInZone", "seconds")


class MfaRequired(Exception):
    """Raised by begin_login when the account needs an MFA code.

    Carries the in-progress `Garmin` client so the caller can hand it to
    complete_mfa() without starting a new login.
    """

    def __init__(self, garmin: Garmin) -> None:
        super().__init__("MFA code required")
        self.garmin = garmin


def try_cached_login(session_dir: Path = SESSION_DIR) -> Garmin | None:
    """Attempt to resume a previously cached session. Returns the
    authenticated client, or None if there's no usable cached session
    (missing, expired, or rejected by the API)."""
    if not session_dir.exists():
        return None
    garmin = Garmin()
    try:
        garmin.login(str(session_dir))
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        return None
    return garmin


def begin_login(email: str, password: str, session_dir: Path = SESSION_DIR) -> Garmin:
    """Start an interactive login. Returns an authenticated client on
    immediate success, or raises MfaRequired if a code is needed."""
    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    mfa_status, _ = garmin.login()
    if mfa_status == "needs_mfa":
        raise MfaRequired(garmin)
    _persist_session(garmin, session_dir)
    return garmin


def complete_mfa(garmin: Garmin, mfa_code: str, session_dir: Path = SESSION_DIR) -> Garmin:
    """Finish a login that raised MfaRequired, using the code the user entered."""
    garmin.resume_login(None, mfa_code)
    _persist_session(garmin, session_dir)
    return garmin


def _persist_session(garmin: Garmin, session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    garmin.client.dump(str(session_dir))


def list_activities(garmin: Garmin, start_date: str, end_date: str) -> list[dict]:
    """Activities between two YYYY-MM-DD dates (inclusive), oldest first."""
    return garmin.get_activities_by_date(start_date, end_date, sortorder="asc")


def download_fit(garmin: Garmin, activity_id: str) -> bytes:
    """Download and unzip the original FIT file for an activity."""
    zip_bytes = garmin.download_activity(
        activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        fit_names = [n for n in archive.namelist() if n.lower().endswith(".fit")]
        if not fit_names:
            raise GarminConnectInvalidFileFormatError(
                f"No .fit file found in downloaded archive for activity {activity_id}"
            )
        return archive.read(fit_names[0])


def _first(d: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _parse_zone_response(raw) -> list[ZoneTime]:
    """Normalize Garmin's hrTimeInZones/powerTimeInZones response into
    ZoneTime entries. The exact response shape is undocumented, so this
    tries several plausible key names and skips anything it can't read."""
    if isinstance(raw, dict):
        entries = next((v for v in raw.values() if isinstance(v, list)), [])
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = []

    zones = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        zone_number = _first(entry, _ZONE_NUMBER_KEYS)
        seconds = _first(entry, _SECONDS_KEYS)
        if zone_number is None or seconds is None:
            continue
        zones.append(
            ZoneTime(
                zone_number=int(zone_number),
                low_boundary=_first(entry, _LOW_BOUNDARY_KEYS),
                high_boundary=_first(entry, _HIGH_BOUNDARY_KEYS),
                seconds=float(seconds),
            )
        )
    return zones


def get_hr_zones(garmin: Garmin, activity_id: str) -> list[ZoneTime]:
    return _parse_zone_response(garmin.get_activity_hr_in_timezones(activity_id))


def get_power_zones(garmin: Garmin, activity_id: str) -> list[ZoneTime]:
    return _parse_zone_response(garmin.get_activity_power_in_timezones(activity_id))


STAT_FIELDS = ("trainingStressScore", "normPower", "intensityFactor", "avgPower", "averageHR")


def activity_stats(garmin: Garmin, start_date: str, end_date: str) -> dict[str, dict]:
    """Per-activity load fields from Garmin's activity list (normalized power, and Garmin's
    own TSS when it computed one) — values the local FIT summary doesn't carry. Keyed by
    activity id; one paginated list call, no per-activity requests."""
    return {
        str(raw["activityId"]): {key: raw.get(key) for key in STAT_FIELDS}
        for raw in garmin.get_activities_by_date(start_date, end_date, sortorder="asc")
    }


def get_thresholds(garmin: Garmin) -> dict:
    """The athlete's current thresholds as configured/detected in Garmin Connect. Fields that
    can't be fetched are simply left out (training_load falls back gracefully)."""
    out: dict = {}
    try:
        ftp = garmin.get_cycling_ftp()
        if isinstance(ftp, dict) and ftp.get("functionalThresholdPower"):
            out["ftp_cycling_w"] = float(ftp["functionalThresholdPower"])
    except Exception:  # optional data: a missing endpoint/field must not break the screen
        pass
    try:
        lt = garmin.get_lactate_threshold()
        hr_speed = lt.get("speed_and_heart_rate") or {}
        if hr_speed.get("heartRate"):
            out["lthr_running"] = float(hr_speed["heartRate"])
        if hr_speed.get("heartRateCycling"):
            out["lthr_cycling"] = float(hr_speed["heartRateCycling"])
        if hr_speed.get("speed"):
            # Garmin reports lactate-threshold speed in units of 10 m/s (0.3778 = 3.778 m/s,
            # i.e. 4:25/km — consistent with the athlete's threshold HR and race paces).
            out["threshold_speed_running_mps"] = float(hr_speed["speed"]) * 10
        power = lt.get("power") or {}
        if power.get("sport") == "RUNNING" and power.get("functionalThresholdPower"):
            out["ftp_running_w"] = float(power["functionalThresholdPower"])
    except Exception:
        pass
    try:
        for zones in garmin.get_heart_rate_zones():
            if zones.get("sport") == "DEFAULT" and zones.get("lactateThresholdHeartRateUsed"):
                out["lthr_default"] = float(zones["lactateThresholdHeartRateUsed"])
    except Exception:
        pass
    return out


def get_calendar_month(garmin: Garmin, year: int, month: int) -> list[dict]:
    """Calendar items (planned workouts, completed activities, ...) for one month (1-12)."""
    return garmin.get_scheduled_workouts(year, month).get("calendarItems", [])


def get_workout(garmin: Garmin, workout_id: int) -> dict:
    return garmin.get_workout_by_id(workout_id)


def upload_workout(garmin: Garmin, workout_dto: dict) -> int:
    """Create a workout in Garmin Connect from a DTO (see workouts.to_garmin); returns its id."""
    return int(garmin.upload_workout(workout_dto)["workoutId"])


def schedule_workout(garmin: Garmin, workout_id: int, date_str: str) -> None:
    garmin.schedule_workout(workout_id, date_str)


def exercise_sets(garmin: Garmin, activity_id: str) -> list[dict]:
    """A strength activity's sets, as recorded by the watch (and corrected in Garmin
    Connect): [{"type": "active"|"rest", "start", "duration_s", "reps", "weight_kg",
    "exercise", "category", "step"}]. Garmin reports weight in grams; exercise and category
    are its upper-case keys lower-cased (e.g. "chest_supported_dumbbell_row", "row"), the
    exercise None when only the category was recognised."""
    out = []
    for s in garmin.get_activity_exercise_sets(activity_id).get("exerciseSets") or []:
        exercise = (s.get("exercises") or [{}])[0]
        out.append({
            "type": (s.get("setType") or "").lower(),
            "start": s.get("startTime"),
            "duration_s": s.get("duration"),
            "reps": s.get("repetitionCount"),
            "weight_kg": s["weight"] / 1000 if s.get("weight") else None,
            "exercise": (exercise.get("name") or "").lower() or None,
            "category": (exercise.get("category") or "").lower() or None,
            "step": s.get("wktStepIndex"),
        })
    return out


def athlete_profile(garmin: Garmin) -> dict:
    """The athlete's current profile: gender, birth year (only the year — an age to the
    year is all anything here needs), height, and heart-rate and power zones per sport."""
    data = (garmin.get_user_profile() or {}).get("userData") or {}
    out = {
        "gender": (data.get("gender") or "").lower() or None,
        "birth_year": int(data["birthDate"][:4]) if data.get("birthDate") else None,
        "height_cm": data.get("height"),
    }
    zones = {}
    for z in garmin.get_heart_rate_zones() or []:
        floors = [z.get(f"zone{n}Floor") for n in range(1, 6)]
        zones[(z.get("sport") or "default").lower()] = {
            "zone_floors_bpm": floors if all(floors) else None,
            "max_hr_bpm": z.get("maxHeartRateUsed"),
            "resting_hr_bpm": z.get("restingHeartRateUsed"),
            "lthr_bpm": z.get("lactateThresholdHeartRateUsed"),
            "method": z.get("trainingMethod"),
        }
    out["hr_zones"] = zones
    power = {}
    try:
        for z in garmin.get_power_zones() or []:
            floors = [z.get(f"zone{n}Floor") for n in range(1, 8)]
            # Unused zones come back as 0 (running has 5): keep the rising floors only.
            used = [f for i, f in enumerate(floors) if f is not None and (i == 0 or f > floors[i - 1])]
            power[(z.get("sport") or "").lower()] = {"ftp_w": z.get("functionalThresholdPower"), "zone_floors_w": used}
    except Exception:  # optional: time in power zones is just left out
        pass
    out["power_zones"] = power
    return out


def athlete_history(garmin: Garmin, start: str, end: str) -> dict[str, dict[str, float]]:
    """Daily values of the athlete's parameters between two YYYY-MM-DD dates, as
    {series: {date: value}} — one request per series, whatever the range. A series Garmin
    can't serve is left out (each is optional)."""
    out: dict[str, dict[str, float]] = {}

    def series(name: str, fetch, parse) -> None:
        try:
            out[name] = {d: v for d, v in parse(fetch()) if d and v is not None}
        except Exception:
            pass

    bio = garmin.garmin_connect_biometric_stats_url
    range_query = "aggregation=daily&aggregationStrategy=LATEST"

    def biometric(metric: str, sport: str):
        return garmin.connectapi(f"{bio}/{metric}/range/{start}/{end}?sport={sport}&{range_query}")

    def values(entries, scale: float = 1.0):
        return [(e.get("from"), e["value"] * scale if e.get("value") is not None else None) for e in entries or []]

    series("weight_kg", lambda: garmin.get_weigh_ins(start, end), lambda r: [
        (s.get("summaryDate"), (s.get("latestWeight") or {}).get("weight", 0) / 1000 or None)
        for s in r.get("dailyWeightSummaries") or []
    ])
    series("resting_hr_bpm", lambda: garmin.connectapi(
        f"{garmin.garmin_connect_rhr_url}/{garmin.display_name}",
        params={"fromDate": start, "untilDate": end, "metricId": 60},
    ), lambda r: [
        (e.get("calendarDate"), e.get("value"))
        for e in ((r.get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE") or []
    ])
    try:  # one response holds both sports' VO2max
        vo2 = garmin.connectapi(f"{garmin.garmin_connect_metrics_url}/{start}/{end}") or []
    except Exception:
        vo2 = []
    for name, key in (("vo2max_running", "generic"), ("vo2max_cycling", "cycling")):
        series(name, lambda: vo2, lambda r, key=key: [
            ((e.get(key) or {}).get("calendarDate"), (e.get(key) or {}).get("vo2MaxPreciseValue")) for e in r
        ])
    series("ftp_cycling_w", lambda: biometric("functionalThresholdPower", "CYCLING"), values)
    series("threshold_power_running_w", lambda: biometric("functionalThresholdPower", "RUNNING"), values)
    series("lthr_running_bpm", lambda: biometric("lactateThresholdHeartRate", "RUNNING"), values)
    # Lactate-threshold speed comes in units of 10 m/s, as in get_thresholds.
    series("threshold_speed_running_mps", lambda: biometric("lactateThresholdSpeed", "RUNNING"), lambda r: values(r, 10.0))
    return out


def workout_exists(garmin: Garmin, workout_id: int) -> bool:
    """Whether a workout is still in the user's Garmin workout library (False once deleted)."""
    try:
        garmin.get_workout_by_id(workout_id)
    except GarminConnectNotFoundError:
        return False
    return True


def unschedule_workout(garmin: Garmin, schedule_id: int) -> None:
    """Remove one calendar entry (a calendar item's "id"); the workout itself is kept."""
    garmin.unschedule_workout(schedule_id)


__all__ = [
    "MfaRequired",
    "try_cached_login",
    "begin_login",
    "complete_mfa",
    "list_activities",
    "download_fit",
    "get_hr_zones",
    "get_power_zones",
    "upload_workout",
    "schedule_workout",
    "unschedule_workout",
    "workout_exists",
    "exercise_sets",
    "athlete_profile",
    "athlete_history",
    "activity_stats",
    "get_thresholds",
    "get_calendar_month",
    "get_workout",
    "GarminConnectAuthenticationError",
    "GarminConnectConnectionError",
    "GarminConnectInvalidFileFormatError",
    "GarminConnectNotFoundError",
    "GarminConnectTooManyRequestsError",
]
