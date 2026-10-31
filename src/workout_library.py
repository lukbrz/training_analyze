"""The workouts folder as the Workouts screens show it, independent of any UI: loose workout
files plus week plans (zips unpacked into folders), their validation state and upload state,
previews, and the "upload a whole week" plan. Shared by the Textual screen and the Qt page.

Rows are keyed by path relative to the workouts folder ("week/file.json" — also the key in
the uploaded-workouts ledger); week header rows are "week:<folder>".

Past plans are archived (see settings): a week whose workouts' dates have all passed (its
folder and zip), or a loose file whose date has passed, moves into workouts/archive/, its
ledger keys gaining the "archive/" prefix. Archived workouts are only listed when the
show-archived setting is on, as "archive/<week>" weeks and "archive/<file>" rows.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import shutil
from pathlib import Path

import services
import settings
import storage
import workouts

WEEK_KEY_PREFIX = "week:"
ARCHIVE = "archive"
COLUMNS = ("File", "Date", "Name", "Sport", "Status", "Uploaded")
EMPTY_HINT = (
    "Put workout .json files, or a week's plan as a .zip of them, in the workouts folder "
    "(in the web app: the upload box below) — see workout_schema.json — then press r."
)


class LibraryError(Exception):
    def __init__(self, message: str, severity: str = "warning") -> None:
        super().__init__(message)
        self.severity = severity


@dataclass
class LibraryRow:
    key: str
    values: tuple[str, ...]
    kind: str  # "week" (group header), "workout" or "invalid"


def _load_entry(path: Path) -> dict | str:
    try:
        return workouts.load(path)
    except workouts.WorkoutError as e:
        return str(e)


class WorkoutLibrary:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or storage.WORKOUTS_DIR
        # relative path -> validated workout dict, or the validation error text
        self.entries: dict[str, dict | str] = {}
        # week folder name -> relative paths of its workouts, in date order
        self.week_dirs: dict[str, list[str]] = {}
        self.all_rows: list[LibraryRow] = []
        # Weeks whose workouts are hidden under their header row. Each week gets a default
        # the first time it's seen (collapsed once it's entirely in the past); after that the
        # user's toggles are kept across refreshes.
        self.collapsed: set[str] = set()
        self._seen_weeks: set[str] = set()

    @property
    def rows(self) -> list[LibraryRow]:
        """The rows to display: everything except the workouts of collapsed weeks, with each
        week header showing ▸ (collapsed) or ▾ (expanded)."""
        rows = []
        for row in self.all_rows:
            week = self.week_of(row.key)
            if row.kind == "week":
                arrow = "▸" if week in self.collapsed else "▾"
                rows.append(LibraryRow(row.key, (f"{arrow} {row.values[0]}", *row.values[1:]), row.kind))
            elif week not in self.collapsed:
                rows.append(row)
        return rows

    def toggle_week(self, key: str | None) -> bool:
        """Collapse/expand the week a row belongs to; False if the row isn't in a week."""
        week = self.week_of(key)
        if week not in self.week_dirs:
            return False
        self.collapsed ^= {week}
        return True

    def refresh(self, today: date | None = None) -> list[str]:
        """Unpack new/updated week zips, archive past plans (if enabled), rescan the folder;
        returns messages for the user."""
        today = (today or date.today()).isoformat()
        self.root.mkdir(parents=True, exist_ok=True)
        messages = workouts.extract_new_archives(self.root)
        prefs = settings.load()
        if prefs["auto_archive_workouts"]:
            messages += self.archive_past(today)
        uploaded = storage.load_uploaded_workouts()
        self.entries, self.week_dirs, self.all_rows = {}, {}, []

        folders = [(self.root, "")]
        archive_root = self.root / ARCHIVE
        if prefs["show_archived_workouts"] and archive_root.is_dir():
            folders.append((archive_root, f"{ARCHIVE}/"))
        for folder, prefix in folders:
            self._scan(folder, prefix, uploaded, today)
        return messages

    def _scan(self, folder: Path, prefix: str, uploaded: dict[str, int], today: str) -> None:
        for path in sorted(folder.glob("*.json")):
            key = f"{prefix}{path.name}"
            self.entries[key] = _load_entry(path)
            self.all_rows.append(self._entry_row(key, key, uploaded))

        for week_dir in sorted(d for d in folder.iterdir() if d.is_dir() and not (d.name == ARCHIVE and not prefix)):
            week = f"{prefix}{week_dir.name}"
            keys = []
            for path in week_dir.glob("*.json"):
                key = f"{week}/{path.name}"
                self.entries[key] = _load_entry(path)
                keys.append(key)
            if not keys:
                continue
            keys.sort(key=lambda k: (self.date_of(k), k))
            self.week_dirs[week] = keys
            dates = [d for d in (self.date_of(k) for k in keys) if d]
            invalid = sum(not isinstance(self.entries[k], dict) for k in keys)
            done = sum(k in uploaded for k in keys)
            if week not in self._seen_weeks:
                self._seen_weeks.add(week)
                if dates and dates[-1] < today:
                    self.collapsed.add(week)
            self.all_rows.append(LibraryRow(
                f"{WEEK_KEY_PREFIX}{week}",
                (
                    week,
                    f"{dates[0]} – {dates[-1]}" if dates else "",
                    f"{len(keys)} workouts",
                    "",
                    f"{invalid} invalid" if invalid else "OK",
                    f"{done}/{len(keys)}",
                ),
                "week",
            ))
            for key in keys:
                self.all_rows.append(self._entry_row(key, f"  {key.rsplit('/', 1)[1]}", uploaded))

    def archive_past(self, today: str) -> list[str]:
        """Move plans whose dates have all passed into workouts/archive/ (see the module
        docstring); returns one message per move. Undated files are never archived."""
        archive_root = self.root / ARCHIVE
        moves: list[tuple[Path, Path, str, str]] = []  # (source, target, old key prefix, new)
        for path in sorted(self.root.glob("*.json")):
            entry = _load_entry(path)
            if isinstance(entry, dict) and entry.get("schedule_date") and entry["schedule_date"] < today:
                moves.append((path, archive_root / path.name, path.name, f"{ARCHIVE}/{path.name}"))
        for week_dir in sorted(d for d in self.root.iterdir() if d.is_dir() and d.name != ARCHIVE):
            entries = [_load_entry(p) for p in week_dir.glob("*.json")]
            dates = [e.get("schedule_date") for e in entries if isinstance(e, dict)]
            if dates and all(d and d < today for d in dates):
                moves.append((week_dir, archive_root / week_dir.name, f"{week_dir.name}/", f"{ARCHIVE}/{week_dir.name}/"))
                zip_path = week_dir.parent / f"{week_dir.name}.zip"  # not with_suffix: "12-18.10" has a dot
                if zip_path.exists():
                    moves.append((zip_path, archive_root / zip_path.name, "", ""))
        if not moves:
            return []

        archive_root.mkdir(exist_ok=True)
        ledger = storage.load_uploaded_workouts()
        messages = []
        for source, target, old, new in moves:
            if target.exists():  # a re-delivered plan for an archived week replaces it
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            source.rename(target)
            if old:
                ledger = {
                    (new + k[len(old):] if (k.startswith(old) if old.endswith("/") else k == old) else k): v
                    for k, v in ledger.items()
                }
                messages.append(f"Archived {source.name} (all its dates have passed)")
        storage.save_uploaded_workouts(ledger)
        return messages

    def _entry_row(self, key: str, label: str, uploaded: dict[str, int]) -> LibraryRow:
        entry = self.entries[key]
        uploaded_id = str(uploaded[key]) if key in uploaded else ""
        if isinstance(entry, dict):
            return LibraryRow(key, (label, entry.get("schedule_date", ""), entry["name"], entry["sport"], "OK", uploaded_id), "workout")
        return LibraryRow(key, (label, "", "–", "–", "invalid", uploaded_id), "invalid")

    @property
    def status_text(self) -> str:
        return f"{len(self.entries)} workout(s), {len(self.week_dirs)} week plan(s) in {self.root}"

    def date_of(self, key: str) -> str:
        entry = self.entries.get(key)
        return entry.get("schedule_date", "") if isinstance(entry, dict) else ""

    @staticmethod
    def week_of(key: str | None) -> str | None:
        """The week folder a row belongs to (its header row or one of its workouts):
        "week" for "week/file.json", "archive/week" for "archive/week/file.json"."""
        if key is None:
            return None
        if key.startswith(WEEK_KEY_PREFIX):
            return key[len(WEEK_KEY_PREFIX):]
        folder = key.rsplit("/", 1)[0] if "/" in key else None
        return None if folder == ARCHIVE else folder

    def preview(self, key: str | None) -> tuple[str, bool]:
        """(preview text, is_error) for the selected row."""
        if key is not None and key.startswith(WEEK_KEY_PREFIX):
            uploaded = storage.load_uploaded_workouts()
            week = self.week_of(key)
            lines = [f"Week plan {week} — Enter/x to expand or collapse, U to upload the whole week", ""]
            for member in self.week_dirs.get(week, []):
                entry = self.entries[member]
                mark = "✓ uploaded" if member in uploaded else ""
                if isinstance(entry, dict):
                    lines.append(f"{entry.get('schedule_date', '          ')}  {workouts.describe(entry)[0]}  {mark}")
                else:
                    lines.append(f"{member}: INVALID")
            return "\n".join(lines), False
        entry = self.entries.get(key) if key else None
        if entry is None:
            return EMPTY_HINT, False
        if isinstance(entry, str):
            return f"Validation errors:\n{entry}", True
        return "\n".join(workouts.describe(entry)), False

    def single_upload(self, key: str | None) -> tuple[list[tuple[str, dict]], str]:
        """(items to upload, warning) for uploading the selected workout."""
        if key is not None and key.startswith(WEEK_KEY_PREFIX):
            raise LibraryError("This is a week — press U to upload all of it, or select one workout")
        entry = self.entries.get(key) if key else None
        if not isinstance(entry, dict):
            raise LibraryError("Select a valid workout to upload")
        warning = ""
        if key in storage.load_uploaded_workouts():
            warning = "Already uploaded — it's put back on the calendar if missing there (no second copy)"
        return [(key, entry)], warning

    def week_upload(self, key: str | None) -> tuple[list[tuple[str, dict]], str]:
        """(items to upload, confirmation prompt) for the selected row's week."""
        week = self.week_of(key)
        if week is None or week not in self.week_dirs:
            raise LibraryError("Select a week plan (or one of its workouts) to upload the week")
        keys = self.week_dirs[week]
        invalid = [k for k in keys if not isinstance(self.entries[k], dict)]
        if invalid:
            raise LibraryError(
                f"{len(invalid)} workout(s) in {week} are invalid — fix or remove them first: "
                + ", ".join(k.rsplit("/", 1)[1] for k in invalid),
                severity="error",
            )
        # Already-uploaded workouts are included too: upload_many checks each against Garmin
        # and only puts it back on the calendar if its entry there was removed.
        uploaded = storage.load_uploaded_workouts()
        new = [k for k in keys if k not in uploaded]
        today = date.today().isoformat()
        if new:
            lines = [f"Upload {len(new)} new workout(s) from {week} to Garmin Connect and schedule them?"]
        else:
            lines = [f"Every workout of {week} is already in Garmin Connect. Check the calendar and schedule each one that's missing from it?"]
        if new and len(new) < len(keys):
            lines.append(
                f"The other {len(keys) - len(new)} are already in Garmin: each one missing from "
                "the calendar is scheduled again (no second copy is made)."
            )
        lines.append("")
        for key in keys:
            entry = self.entries[key]
            when = entry.get("schedule_date") or "not scheduled"
            note = "  (already uploaded)" if key in uploaded else ""
            if entry.get("schedule_date") and entry["schedule_date"] < today:
                note += "  ⚠ date is in the past"
            lines.append(f"  {when:<13} {entry['name']} [{entry['sport']}]{note}")
        return [(k, self.entries[k]) for k in keys], "\n".join(lines)


def upload_many(
    client, items: list[tuple[str, dict]], progress: Callable[[str], None] = lambda _: None
) -> list[tuple[str, str]]:
    """Upload (and schedule) each workout, carrying on past failures. Returns
    (severity, message) notifications to show the user."""
    notes = []
    counts = {"uploaded": 0, "rescheduled": 0, "unchanged": 0}
    months: dict = {}  # Garmin calendar months fetched during this run
    for i, (key, entry) in enumerate(items, start=1):
        progress(f"Uploading {i}/{len(items)}: {entry['name']}...")
        try:
            outcome, warning = services.upload_and_record(client, key, entry, months)
        except Exception as e:  # surface any API rejection (e.g. a malformed DTO) and carry on
            notes.append(("error", f"Upload failed — {entry['name']}: {e}"))
            continue
        counts[outcome] += 1
        if warning:
            notes.append(("warning", warning))
    parts = [
        f"{counts['uploaded']} uploaded" if counts["uploaded"] else "",
        f"{counts['rescheduled']} put back on the calendar" if counts["rescheduled"] else "",
        f"{counts['unchanged']} already on the calendar" if counts["unchanged"] else "",
    ]
    if any(parts):
        notes.append(("information", "Garmin Connect: " + ", ".join(p for p in parts if p)))
    return notes
