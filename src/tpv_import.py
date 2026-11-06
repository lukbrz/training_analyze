"""Importing TrainingPeaks Virtual recordings from files, for rides whose TPV recording never
reached this copy of the app through Garmin Connect (deleted there as a duplicate before
this copy synced it), so they're left with the bike computer's trainer distance.

TPV saves every ride as a FIT file: on the PC it runs on, and in TrainingPeaks' "export
workout files" zip (<device>_<yyyymmdd>_<hhmmss>_s.fit.gz beside the other devices' files).
The import reads the .zip, .fit and .fit.gz files in the import folder (storage.IMPORT_DIR),
keeps TPV's recordings, and adds each to the ride recorded beside it by a bike computer or
watch (started within MATCH_WINDOW of it, sharing at least MIN_OVERLAP of the shorter
recording), merged just like a synced one: its distance, speed, position and elevation
replace the trainer's. A recording no ride matches becomes an activity of its own (nothing
else recorded that ride, or its other recording was deleted too). Running it again only
adds what's still missing.

A TPV FIT file doesn't name its route (Garmin's title for it did). The name comes from the
route its GPS track matches when an earlier ride there was named, else from TPV's catalogue
when exactly one route has the recording's lap distance, else it stays unnamed ("Route N",
renamed on the Routes screen like any other).
"""

import gzip
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitparse

import fit_parser
import services
import storage
import tpv
from models import ActivitySummary, Route

ID_PREFIX = "tpv-"  # imported recordings' activity ids (Garmin's are numbers)
FIT_SUFFIXES = (".fit", ".fit.gz")
MAX_UTC_OFFSET = timedelta(hours=14)
MATCH_WINDOW = timedelta(minutes=30)  # how far apart TPV and the ride's other recording may start
MIN_OVERLAP = 0.5  # ...and how much of the shorter one they must share


@dataclass
class ImportReport:
    files: int = 0  # FIT files read
    recordings: int = 0  # of them TPV's
    imported: list[str] = field(default_factory=list)  # one line per ride that got its recording
    added: list[str] = field(default_factory=list)  # one line per recording added as an activity
    present: int = 0  # recordings the ride already has
    averages: int = 0  # TPV-only activities given the average HR / power TPV leaves out
    unreadable: list[str] = field(default_factory=list)

    def message(self) -> str:
        if not self.files and not self.unreadable:
            averages = f"; average HR/power worked out for {_count(self.averages, 'TPV ride')}" if self.averages else ""
            return f"No .zip, .fit or .fit.gz files in {storage.IMPORT_DIR}{averages}"
        parts = [f"TPV import: TPV recording added to {_count(len(self.imported), 'ride')}"]
        if self.added:
            parts.append(f"{_count(len(self.added), 'new activity', 'new activities')} added (no other recording)")
        parts.append(f"{self.present} already there")
        if self.averages:
            parts.append(f"average HR/power worked out for {_count(self.averages, 'TPV ride')}")
        if self.unreadable:
            parts.append(f"{_count(len(self.unreadable), 'file')} unreadable")
        parts.append(f"{_count(self.recordings, 'TPV recording')} in {_count(self.files, 'FIT file')}")
        return ", ".join(parts)

    def details(self) -> str:
        return "\n".join([self.message(), *self.imported, *self.added])


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def import_files(progress: services.Progress = lambda _: None, paths: list[Path] | None = None) -> ImportReport | str:
    """Import the TPV recordings in `paths` (default: every file in the import folder). One
    job at a time (services.exclusive_job); returns a message instead when another runs."""
    try:
        with services.exclusive_job("A TPV import"):
            return _import(_default_paths() if paths is None else paths, progress)
    except services.Busy as e:
        return str(e)


def import_message(progress: services.Progress = lambda _: None) -> str:
    """import_files() as the summary text the front ends show."""
    report = import_files(progress)
    return report if isinstance(report, str) else report.message()


def _default_paths() -> list[Path]:
    if not storage.IMPORT_DIR.is_dir():
        return []
    return sorted(p for p in storage.IMPORT_DIR.rglob("*") if p.is_file())


def fit_files(paths: list[Path], report: ImportReport) -> Iterator[tuple[str, bytes]]:
    """(name, FIT bytes) of every FIT file in `paths` — zips are opened, .gz unpacked."""
    for path in paths:
        lower = path.name.lower()
        try:
            if lower.endswith(".zip"):
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        if member.lower().endswith(FIT_SUFFIXES):
                            yield member, _unpacked(member, archive.read(member))
            elif lower.endswith(FIT_SUFFIXES):
                yield path.name, _unpacked(lower, path.read_bytes())
        except (OSError, EOFError, zipfile.BadZipFile, gzip.BadGzipFile):
            report.unreadable.append(path.name)


def _unpacked(name: str, data: bytes) -> bytes:
    return gzip.decompress(data) if name.lower().endswith(".gz") else data


@dataclass
class Header:
    start_utc: datetime  # first record
    offset: timedelta | None  # UTC -> local time, from TPV's activity message


def tpv_header(fit: bytes) -> Header | None:
    """Start time of a TPV recording; None for another device's file. Reads only the start
    of the file (TPV writes its activity message before the records)."""
    offset = None
    for message in fitparse.FitFile(fit, check_crc=False).get_messages():
        if message.name == "file_id":
            maker, product = message.get_value("manufacturer"), message.get_value("product_name")
            if maker != "peaksware" and tpv.TPV_NAME not in str(product or ""):
                return None
        elif message.name == "activity":
            utc, local = message.get_value("timestamp"), message.get_value("local_timestamp")
            if utc and local:
                offset = local - utc.replace(tzinfo=None)
        elif message.name == "record":
            start = message.get_value("timestamp")
            return Header(start.replace(tzinfo=None), offset) if start else None
    return None


def _utc_start(activity: ActivitySummary) -> datetime | None:
    if not storage.has_fit(activity.activity_id):
        return None
    try:
        local = datetime.fromisoformat(activity.start_time_local)
        return local - fit_parser.load_utc_offset(activity.activity_id, activity.start_time_local)
    except (ValueError, OSError):
        return None


def _is_tpv_recording(activity_id: str, names: dict[str, str]) -> bool:
    """Whether a recording is a virtual-riding program's (not a bike computer that flags a
    trainer ride virtual, as an Edge paired with TPV does)."""
    if activity_id.startswith(ID_PREFIX) or tpv.is_tpv_title(names.get(activity_id)):
        return True
    try:
        fit = storage.fit_path(activity_id).read_bytes()
        virtual = fit_parser.sub_sport(fit) == "virtual_activity" or "virtual" in fit_parser.recording_name(fit).lower()
        return virtual and fit_parser.virtual_software(fit)
    except Exception:  # missing or unreadable
        return False


def has_tpv_recording(activity: ActivitySummary, ledger: dict[str, str], names: dict[str, str]) -> bool:
    """Whether an activity is a virtual-riding program's recording or has one merged in."""
    return (activity.virtual and _is_tpv_recording(activity.activity_id, names)) or any(
        primary == activity.activity_id and _is_tpv_recording(secondary, names) for secondary, primary in ledger.items()
    )


def route_name_for(route: Route | None, laps, activities: list[ActivitySummary], catalogue: list[dict]) -> str | None:
    """The TPV route an imported recording rode (see the module docstring)."""
    if route is not None:
        for activity in activities:
            recording = services.tpv_recording(activity) if activity.route_id == route.route_id else None
            name = tpv.route_name(recording[1]) if recording else None
            if name:
                return name
        named = re.fullmatch(r"(.+) \(TPV\)", route.name)
        if named:
            return named.group(1)
    found = tpv.route_by_laps(laps, catalogue)
    return found["name"] if found else None


def _overlap_s(a_start: datetime, a_s: float, b_start: datetime, b_s: float) -> float:
    return max(0.0, (min(a_start + timedelta(seconds=a_s), b_start + timedelta(seconds=b_s)) - max(a_start, b_start)).total_seconds())


def _import(paths: list[Path], progress: services.Progress) -> ImportReport:
    report = ImportReport()
    last_synced_date, activities = storage.load_index()
    routes_list = storage.load_routes()
    ledger = storage.load_merge_ledger()
    names = storage.load_recording_names()
    catalogue = services.refresh_tpv_catalogue(progress)
    rides = [a for a in activities if a.sport == "cycling"]
    utc_starts: dict[str, datetime | None] = {}
    merged: list[tuple[str, str, float]] = []  # (recording id, ride id, its distance before)
    added: list[str] = []  # recordings added as activities of their own

    for name, fit in fit_files(paths, report):
        report.files += 1
        progress(f"Reading {name} ({report.files})...")
        try:
            header = tpv_header(fit)
        except Exception:  # not a FIT file fitparse can read
            report.unreadable.append(name)
            continue
        if header is None:
            continue
        report.recordings += 1
        start = header.start_utc
        recording_id = f"{ID_PREFIX}{start:%Y%m%d-%H%M%S}"
        if recording_id in ledger or any(a.activity_id == recording_id for a in activities):
            report.present += 1
            continue

        # Rides started near it (TPV and the bike computer or watch are started by hand).
        nearby = []
        for ride in rides:
            try:  # cheap pre-filter on local time before loading the ride's UTC offset
                local = datetime.fromisoformat(ride.start_time_local)
            except ValueError:
                continue
            if abs(local - start) > MAX_UTC_OFFSET + MATCH_WINDOW:
                continue
            if ride.activity_id not in utc_starts:
                utc_starts[ride.activity_id] = _utc_start(ride)
            utc = utc_starts[ride.activity_id]
            if utc is not None and abs(utc - start) <= MATCH_WINDOW:
                nearby.append(ride)
        if any(has_tpv_recording(ride, ledger, names) for ride in nearby):
            report.present += 1
            continue

        offset = header.offset
        if nearby:
            offset = datetime.fromisoformat(nearby[0].start_time_local) - utc_starts[nearby[0].activity_id]
        if offset is None:  # this computer's time zone
            offset = start.replace(tzinfo=timezone.utc).astimezone().utcoffset() or timedelta(0)
        progress(f"Importing the TPV recording of {start + offset:%Y-%m-%d %H:%M}...")
        try:
            summary, laps = fit_parser.parse(fit, recording_id, tpv.TPV_NAME, f"{start + offset:%Y-%m-%d %H:%M:%S}")
        except ValueError:
            report.unreadable.append(name)
            continue
        # The ride recorded alongside: the one it overlaps most, by at least half the shorter.
        ride, best = None, 0.0
        for candidate in nearby:
            overlap = _overlap_s(start, summary.duration_s, utc_starts[candidate.activity_id], candidate.duration_s)
            if overlap >= MIN_OVERLAP * min(summary.duration_s, candidate.duration_s) and overlap > best:
                ride, best = candidate, overlap

        storage.save_fit(recording_id, fit)
        storage.save_laps(recording_id, laps)
        summary.route_id = services.assign_route(recording_id, fit, summary.sport, routes_list)
        route = next((r for r in routes_list if r.route_id == summary.route_id), None)
        summary.name = tpv.title_for(route_name_for(route, laps, activities, catalogue))
        names[recording_id] = summary.name
        storage.save_recording_names(names)
        if ride is not None:
            ledger[recording_id] = ride.activity_id
            storage.save_merge_ledger(ledger)
            merged.append((recording_id, ride.activity_id, ride.distance_m))
        else:  # a ride nothing else recorded (or whose other recording is gone): its own activity
            activities.append(summary)
            added.append(recording_id)

    if merged or added:
        activities, _ = services.merge_duplicates(
            activities, recompute_existing=False, routes_list=routes_list,
            rebuild={ride_id for _, ride_id, _ in merged}, progress=progress,
        )
        services.name_tpv_routes(activities, routes_list)
        storage.save_index(last_synced_date, activities)
        _forget_regions([ride_id for _, ride_id, _ in merged])
        by_id = {a.activity_id: a for a in activities}
        routes_by_id = {r.route_id: r for r in routes_list}

        def line(activity: ActivitySummary, distance: str) -> str:
            route = routes_by_id.get(activity.route_id)
            return f"{activity.start_time_local[:16]} {activity.name}: {distance}" + (f", route {route.name}" if route else "")

        for _, ride_id, before in merged:
            ride = by_id[ride_id]
            report.imported.append(line(ride, f"{before / 1000:.1f} → {ride.distance_m / 1000:.1f} km"))
        for recording_id in added:
            activity = by_id.get(recording_id)
            if activity is not None:  # (a later sync's duplicate could have taken its place)
                report.added.append(line(activity, f"{activity.distance_m / 1000:.1f} km, added as a new activity"))
    report.averages = _fill_averages(activities, progress)
    if report.averages:
        storage.save_index(last_synced_date, activities)
    return report


def _fill_averages(activities: list[ActivitySummary], progress: services.Progress) -> int:
    """Average HR / power for TPV-only activities saved before the parser worked them out
    from the records (TPV's FIT files carry neither) — a full Reprocess would too, but takes
    long. Returns how many activities changed."""
    changed = 0
    for activity in activities:
        if not (activity.activity_id.startswith(ID_PREFIX) or tpv.is_tpv_title(activity.name)):
            continue
        if (activity.avg_hr is not None and activity.avg_power is not None) or not storage.has_fit(activity.activity_id):
            continue
        progress(f"Working out average HR and power of {activity.name} ({activity.start_time_local[:10]})...")
        try:
            summary, laps = fit_parser.parse(
                storage.fit_path(activity.activity_id).read_bytes(), activity.activity_id, activity.name, activity.start_time_local,
            )
        except ValueError:
            continue
        avg_hr = activity.avg_hr if activity.avg_hr is not None else summary.avg_hr
        avg_power = activity.avg_power if activity.avg_power is not None else summary.avg_power
        if (avg_hr, avg_power) == (activity.avg_hr, activity.avg_power):
            continue  # the recording has neither
        activity.avg_hr, activity.avg_power = avg_hr, avg_power
        stored = storage.load_laps(activity.activity_id)
        if len(stored) == len(laps):  # (not after a custom split)
            for old, new in zip(stored, laps):
                old.avg_hr = old.avg_hr if old.avg_hr is not None else new.avg_hr
                old.avg_power = old.avg_power if old.avg_power is not None else new.avg_power
            storage.save_laps(activity.activity_id, stored)
        changed += 1
    return changed


def _forget_regions(activity_ids: list[str]) -> None:
    """Drop the cached platform/world of rides that just got a TPV recording, so the Routes
    screen classifies them again (as TPV rather than an indoor ride)."""
    cache = storage.load_activity_regions()
    known = cache.get("regions") or {}
    if [known.pop(i) for i in activity_ids if i in known]:
        storage.save_activity_regions(cache)
