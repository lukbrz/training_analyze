"""The workouts folder as the Workouts screens show it, independent of any UI: loose workout
files plus week plans (zips unpacked into folders), their validation state and upload state,
previews, and the "upload a whole week" plan. Shared by the Textual screen and the Qt page.

Rows are keyed by path relative to the workouts folder ("week/file.json" — also the key in
the uploaded-workouts ledger); week header rows are "week:<folder>".
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import services
import storage
import workouts

WEEK_KEY_PREFIX = "week:"
COLUMNS = ("File", "Date", "Name", "Sport", "Status", "Uploaded")
EMPTY_HINT = (
    "Put workout .json files, or a week's plan as a .zip of them, in the folders above "
    "(see workout_schema.json), then press r."
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

    def refresh(self) -> list[str]:
        """Unpack new/updated week zips, rescan the folder; returns unpack messages."""
        self.root.mkdir(parents=True, exist_ok=True)
        messages = workouts.extract_new_archives(self.root)
        uploaded = storage.load_uploaded_workouts()
        self.entries, self.week_dirs, self.all_rows = {}, {}, []
        today = date.today().isoformat()

        for path in sorted(self.root.glob("*.json")):
            self.entries[path.name] = _load_entry(path)
            self.all_rows.append(self._entry_row(path.name, path.name, uploaded))

        for week_dir in sorted(d for d in self.root.iterdir() if d.is_dir()):
            keys = []
            for path in week_dir.glob("*.json"):
                key = f"{week_dir.name}/{path.name}"
                self.entries[key] = _load_entry(path)
                keys.append(key)
            if not keys:
                continue
            keys.sort(key=lambda k: (self.date_of(k), k))
            self.week_dirs[week_dir.name] = keys
            dates = [d for d in (self.date_of(k) for k in keys) if d]
            invalid = sum(not isinstance(self.entries[k], dict) for k in keys)
            done = sum(k in uploaded for k in keys)
            if week_dir.name not in self._seen_weeks:
                self._seen_weeks.add(week_dir.name)
                if dates and dates[-1] < today:
                    self.collapsed.add(week_dir.name)
            self.all_rows.append(LibraryRow(
                f"{WEEK_KEY_PREFIX}{week_dir.name}",
                (
                    week_dir.name,
                    f"{dates[0]} – {dates[-1]}" if dates else "",
                    f"{len(keys)} workouts",
                    "",
                    f"{invalid} invalid" if invalid else "OK",
                    f"{done}/{len(keys)}",
                ),
                "week",
            ))
            for key in keys:
                self.all_rows.append(self._entry_row(key, f"  {key.split('/', 1)[1]}", uploaded))
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
        """The week folder a row belongs to (its header row or one of its workouts)."""
        if key is None:
            return None
        if key.startswith(WEEK_KEY_PREFIX):
            return key[len(WEEK_KEY_PREFIX):]
        return key.split("/", 1)[0] if "/" in key else None

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
            warning = "Already uploaded — uploading again creates a second copy in Garmin"
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
                + ", ".join(k.split("/", 1)[1] for k in invalid),
                severity="error",
            )
        uploaded = storage.load_uploaded_workouts()
        pending = [k for k in keys if k not in uploaded]
        if not pending:
            raise LibraryError(f"Everything in {week} is already uploaded", severity="information")

        today = date.today().isoformat()
        lines = [f"Upload {len(pending)} workout(s) from {week} to Garmin Connect and schedule them?", ""]
        for key in keys:
            entry = self.entries[key]
            when = entry.get("schedule_date") or "not scheduled"
            note = "  (already uploaded — skipped)" if key in uploaded else ""
            if key not in uploaded and entry.get("schedule_date") and entry["schedule_date"] < today:
                note = "  ⚠ date is in the past"
            lines.append(f"  {when:<13} {entry['name']} [{entry['sport']}]{note}")
        return [(k, self.entries[k]) for k in pending], "\n".join(lines)


def upload_many(
    client, items: list[tuple[str, dict]], progress: Callable[[str], None] = lambda _: None
) -> list[tuple[str, str]]:
    """Upload (and schedule) each workout, carrying on past failures. Returns
    (severity, message) notifications to show the user."""
    notes, done = [], 0
    for i, (key, entry) in enumerate(items, start=1):
        progress(f"Uploading {i}/{len(items)}: {entry['name']}...")
        try:
            warning = services.upload_and_record(client, key, entry)
        except Exception as e:  # surface any API rejection (e.g. a malformed DTO) and carry on
            notes.append(("error", f"Upload failed — {entry['name']}: {e}"))
            continue
        done += 1
        if warning:
            notes.append(("warning", warning))
    if done:
        notes.append(("information", f"Uploaded {done} of {len(items)} workout(s) to Garmin Connect"))
    return notes
