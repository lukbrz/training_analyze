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


__all__ = [
    "MfaRequired",
    "try_cached_login",
    "begin_login",
    "complete_mfa",
    "list_activities",
    "download_fit",
    "get_hr_zones",
    "get_power_zones",
    "GarminConnectAuthenticationError",
    "GarminConnectConnectionError",
    "GarminConnectInvalidFileFormatError",
    "GarminConnectNotFoundError",
    "GarminConnectTooManyRequestsError",
]
