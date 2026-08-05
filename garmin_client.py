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

from storage import SESSION_DIR


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


__all__ = [
    "MfaRequired",
    "try_cached_login",
    "begin_login",
    "complete_mfa",
    "list_activities",
    "download_fit",
    "GarminConnectAuthenticationError",
    "GarminConnectConnectionError",
    "GarminConnectInvalidFileFormatError",
    "GarminConnectNotFoundError",
    "GarminConnectTooManyRequestsError",
]
