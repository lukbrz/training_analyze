"""The laps view's state and operations, independent of any UI. The view is one list in
three sections:

- INTERVALS — every marked lap, whichever section it was marked in, in time order, each
  followed by its phase splits ("2.1", "2.2", ...);
- LAPS — the device's own laps, or a whole-activity split by distance/time when one is set;
- PHYSICAL — repeated course loops detected from GPS (in the background on open).

Interval marks on device laps are saved; marks on a split or on physical laps last for this
session only (neither is saved). The Textual LapsScreen, the Qt laps page and the web laps
page are all thin shells around a LapsSession, so they can't drift apart in behaviour."""

from dataclasses import dataclass
from datetime import timedelta

import display
import fit_parser
import storage
from models import ActivitySummary, Lap, format_split_value, parse_split_value

DEFAULT_INTERVAL_SPLIT_M = 300.0
SPLIT_HELP = "Split size: 400 (metres), 1.5km, or a time like 60s, 1m, 1m30s, 1:30"

# Where a lap comes from, and its prefix when listed among the intervals (device laps keep
# their plain number).
DEVICE, SPLIT, PHYSICAL = "device", "split", "physical"
INTERVAL_LABEL_PREFIX = {DEVICE: "", SPLIT: "S", PHYSICAL: "P"}


class LapsError(Exception):
    """Something the user should be told (bad input, nothing to act on) — not a bug."""


@dataclass
class LapRow:
    values: tuple[str, ...]
    # "header" (section title), "interval" (marked lap), "plain" (unmarked lap) or "phase"
    # (split of an interval)
    kind: str
    section: str  # "intervals", "laps" or "physical"
    lap: Lap | None = None  # the lap (or phase split) this row shows; None on headers
    source: str | None = None  # DEVICE / SPLIT / PHYSICAL list the lap belongs to


# Metrics a track can be coloured by: record attribute -> label.
MAP_METRICS = {"speed_mps": "Speed / pace", "heart_rate": "Heart rate", "power": "Power", "altitude_m": "Elevation"}

LatLon = tuple[float, float]


@dataclass
class MapLayers:
    """What a map of the current laps view shows, as plain data for any front end."""
    points: list[LatLon]
    values: list[float | None]  # the colouring metric per point (None = no data there)
    highlights: list[list[LatLon]]  # marked intervals
    selected: list[LatLon]  # the selected lap / phase
    markers: list[tuple[float, float, str]]  # (lat, lon, lap number) at each lap start


def _gps(records) -> list[LatLon]:
    return [(r.lat, r.lon) for r in records if r.lat is not None and r.lon is not None]


class LapsSession:
    def __init__(self, activity: ActivitySummary) -> None:
        self.activity = activity
        self.multisport = activity.sport == fit_parser.MULTISPORT
        self.device_laps: list[Lap] = storage.load_laps(activity.activity_id)
        self.split_laps: list[Lap] | None = None  # the LAPS section's split, when one is set
        self.split_name = ""
        self.physical_laps: list[Lap] | None = None
        # "pending" until detection has run, then "found", "none" or "no GPS"
        self.physical_status = "pending"
        # (source, lap_index) -> phase splits of that (marked) lap
        self.interval_splits: dict[tuple[str, int], list[Lap]] = {}
        self.show_intervals_only = False
        self.records: list[fit_parser.RecordPoint] | None = None
        self.utc_offset: timedelta | None = None
        # Row a front end should move its cursor to after the next rebuild (an operation
        # moved the row it acted on), or None to keep the cursor where it is.
        self.cursor_hint: int | None = None
        self._rows: list[LapRow] = []

    # --- what to display ---------------------------------------------------------

    @property
    def columns(self) -> tuple[str, ...]:
        return display.lap_columns(self.multisport)

    @property
    def right_aligned_columns(self) -> set[int]:
        return display.lap_right_aligned_columns(self.multisport)

    @property
    def current_laps(self) -> list[Lap]:
        """The LAPS section: the split when one is set, else the device's laps."""
        return self.split_laps if self.split_laps is not None else self.device_laps

    def _sources(self) -> list[tuple[str, list[Lap]]]:
        sources = [(DEVICE, self.device_laps)]
        if self.split_laps is not None:
            sources.append((SPLIT, self.split_laps))
        if self.physical_laps:
            sources.append((PHYSICAL, self.physical_laps))
        return sources

    def _marked(self) -> list[tuple[str, Lap]]:
        marked = [(source, lap) for source, laps in self._sources() for lap in laps if lap.is_interval]
        return sorted(marked, key=lambda m: m[1].start_time_local)

    def marked_laps(self) -> list[Lap]:
        return [lap for _, lap in self._marked()]

    def summary_text(self) -> str:
        device_laps = storage.load_laps(self.activity.activity_id) if self.multisport else []
        return display.activity_summary_text(self.activity, device_laps)

    def interval_summary(self) -> str:
        return display.interval_summary_text(self.marked_laps(), self.activity.sport)

    def _header(self, section: str, title: str, note: str) -> LapRow:
        values = (title, note) + ("",) * (len(self.columns) - 2)
        return LapRow(values, "header", section)

    def _lap_rows(self, section: str, laps: list[tuple[str, Lap]], label) -> list[LapRow]:
        """A section's lap rows; the Total column runs from the section's start (phase rows
        continue from their interval's start)."""
        rows = []
        total = 0.0
        for source, lap in laps:
            lap_start_total = total
            total += lap.duration_s
            rows.append(LapRow(
                display.lap_row(lap, label(source, lap), self.activity.sport, self.multisport, lap.is_interval, total),
                "interval" if lap.is_interval else "plain", section, lap, source,
            ))
            if section != "intervals":
                continue
            phase_total = lap_start_total
            for number, phase in enumerate(self.interval_splits.get((source, lap.lap_index), []), start=1):
                phase_total += phase.duration_s
                rows.append(LapRow(
                    display.lap_row(phase, f"  {label(source, lap)}.{number}", self.activity.sport, self.multisport, True, phase_total),
                    "phase", section, phase, source,
                ))
        return rows

    def rows(self) -> list[LapRow]:
        """The displayed rows: a header, then the laps, of each section. With the
        interval-only filter on, only the INTERVALS section is shown."""
        marked = self._marked()
        rows = [self._header("intervals", "INTERVALS", f"{len(marked)} marked" if marked else "none — m marks a lap")]
        rows += self._lap_rows("intervals", marked, _interval_label)
        if not self.show_intervals_only:
            source = SPLIT if self.split_laps is not None else DEVICE
            rows.append(self._header("laps", "LAPS", self.split_name or "device"))
            rows += self._lap_rows("laps", [(source, lap) for lap in self.current_laps], _plain_label)
            note = {
                "pending": "detecting...", "none": "none found", "no GPS": "no GPS track",
                "found": self._physical_note(),
            }[self.physical_status]
            rows.append(self._header("physical", "PHYSICAL", note))
            rows += self._lap_rows("physical", [(PHYSICAL, lap) for lap in self.physical_laps or []], _plain_label)
        self._rows = rows
        return rows

    def _physical_note(self) -> str:
        laps = self.physical_laps or []
        loops = sum(1 for lap in laps if (lap.label or "").isdigit())
        extras = "/".join(lap.label for lap in laps if lap.label in (fit_parser.LEAD_IN_LABEL, fit_parser.WAY_BACK_LABEL))
        return f"{loops} loops" + (f" + {extras}" if extras else "")

    def row_lap(self, row_index: int) -> Lap | None:
        """The lap (or phase split) shown in a displayed row; None for section headers."""
        return self._rows[row_index].lap if 0 <= row_index < len(self._rows) else None

    def take_cursor_hint(self) -> int | None:
        hint, self.cursor_hint = self.cursor_hint, None
        return hint

    # --- operations --------------------------------------------------------------

    def ensure_records(self) -> list[fit_parser.RecordPoint]:
        if self.records is None:
            path = storage.fit_path(self.activity.activity_id)
            if not path.exists():
                raise LapsError("FIT file not found for this activity")
            self.records = fit_parser.load_records(self.activity.activity_id)
            self.utc_offset = fit_parser.load_utc_offset(self.activity.activity_id, self.activity.start_time_local)
        if not self.records:
            raise LapsError("No record data in this FIT file")
        return self.records

    @staticmethod
    def parse_split(text: str, default: tuple[str, float] | None = None) -> tuple[str, float]:
        text = text.strip()
        if not text and default is not None:
            return default
        try:
            return parse_split_value(text)
        except ValueError:
            raise LapsError(SPLIT_HELP) from None

    def _split_records(self, records: list[fit_parser.RecordPoint], mode: str, value: float) -> list[Lap]:
        if mode == "distance":
            return fit_parser.split_by_distance(records, value, self.utc_offset)
        return fit_parser.split_by_time(records, value, self.utc_offset)

    def _drop_phases(self, source: str) -> None:
        self.interval_splits = {k: v for k, v in self.interval_splits.items() if k[0] != source}

    def split(self, text: str) -> None:
        """Show the whole activity split by the distance/time in `text` in the LAPS section."""
        parsed = self.parse_split(text)
        self.split_laps = self._split_records(self.ensure_records(), *parsed)
        self.split_name = f"split {format_split_value(*parsed)}"
        self._drop_phases(SPLIT)

    def reset(self) -> None:
        """Back to the device's laps in the LAPS section."""
        self.split_laps = None
        self.split_name = ""
        self._drop_phases(SPLIT)

    def toggle_interval(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._rows):
            return
        row = self._rows[row_index]
        if row.kind == "header":
            return
        if row.kind == "phase":
            raise LapsError("Mark the parent lap, not a phase split")
        lap = row.lap
        lap.is_interval = not lap.is_interval
        if not lap.is_interval:
            self.interval_splits.pop((row.source, lap.lap_index), None)
        if row.source == DEVICE:
            storage.save_laps(self.activity.activity_id, self.device_laps)
        # Marking adds a row to (unmarking removes one from) the INTERVALS section above, so
        # follow the lap; a lap unmarked in the INTERVALS section just leaves it.
        if row.section != "intervals":
            new_rows = self.rows()
            self.cursor_hint = next(
                (i for i, r in enumerate(new_rows) if r.section == row.section and r.lap is lap), row_index,
            )

    def split_intervals(self, text: str) -> str:
        """Split every marked lap into phases (default 300 m); returns a status message."""
        marked = self._marked()
        if not marked:
            raise LapsError("Mark at least one interval (m) before splitting it into phases")
        parsed = self.parse_split(text, default=("distance", DEFAULT_INTERVAL_SPLIT_M))
        records = self.ensure_records()
        self.interval_splits = {}
        for source, lap in marked:
            lap_records = fit_parser.lap_record_slice(records, lap, self.utc_offset)
            if lap_records:
                phases = self._split_records(lap_records, *parsed)
                for phase in phases:
                    phase.sport = lap.sport
                self.interval_splits[(source, lap.lap_index)] = phases
        return f"Split {len(self.interval_splits)} interval(s) into {format_split_value(*parsed)} phases"

    def toggle_filter(self) -> str:
        """Toggle interval-only; returns a warning when there's nothing to show."""
        self.show_intervals_only = not self.show_intervals_only
        self.cursor_hint = 0
        if self.show_intervals_only and not self.marked_laps():
            return "No intervals marked yet (press m on a lap to mark it)"
        return ""

    def physical_laps_radius(self, text: str) -> float:
        """Validate the loop radius for detect_physical_laps (an optional distance in the
        split input — the escape hatch when GPS drift hides real loops at the default)."""
        self.ensure_records()
        mode, radius = self.parse_split(text, default=("distance", fit_parser.DEFAULT_CLOSE_RADIUS_M))
        if mode != "distance":
            raise LapsError("Loop radius must be a distance in metres, e.g. 100")
        return radius

    def detect_physical_laps(self, radius: float = fit_parser.DEFAULT_CLOSE_RADIUS_M) -> list[Lap] | None:
        """Slow (scores every candidate loop anchor) — call it off the UI thread, then
        apply_physical_laps() on it. The laps view runs it once on open at the default
        radius; p re-runs it with the radius typed in the split input."""
        records = self.ensure_records()
        if not any(r.lat is not None for r in records):
            return None
        return fit_parser.detect_physical_laps(records, self.utc_offset, close_radius_m=radius)

    def apply_physical_laps(self, laps: list[Lap] | None, explicit: bool = True) -> str:
        """Show detected loops in the PHYSICAL section. `explicit` (the user asked, with p)
        reports the outcome; the automatic run on open stays quiet."""
        has_gps = self.records is not None and any(r.lat is not None for r in self.records)
        self.physical_laps = laps or None
        self.physical_status = "found" if laps else "none" if has_gps else "no GPS"
        self._drop_phases(PHYSICAL)
        if not explicit:
            return ""
        if not laps:
            raise LapsError("No repeated course laps detected" if has_gps else "No GPS track to find loops in")
        return f"Found {len(laps)} physical laps"

    def physical_laps_failed(self) -> None:
        """Automatic detection couldn't run (no FIT file or no records)."""
        self.physical_status = "no GPS"

    def histogram_phases(self) -> list[Lap]:
        """Interval phase-splits when present (the finer view), else the marked intervals,
        else the LAPS section."""
        phases = (
            [p for phases in self.interval_splits.values() for p in phases]
            or self.marked_laps()
            or list(self.current_laps)
        )
        if not phases:
            raise LapsError("No laps to show a histogram for")
        return phases

    def lap_coords(self, lap: Lap | None) -> list[LatLon]:
        """GPS points of one lap (or phase split); records must be loaded."""
        if lap is None or self.records is None:
            return []
        return _gps(fit_parser.lap_record_slice(self.records, lap, self.utc_offset))

    def map_layers(self, metric: str = "speed_mps", selected_lap: Lap | None = None) -> MapLayers | None:
        """Map data for the current view (loads records — slow the first time for a long
        activity, so call it off the UI thread). None when the activity has no GPS track.
        Lap-start markers are those of the LAPS section."""
        records = [r for r in self.ensure_records() if r.lat is not None and r.lon is not None]
        if len(records) < 2:
            return None
        values = [getattr(r, metric) for r in records]
        if metric == "speed_mps" and all(v is None for v in values):
            values = _speed_from_distance(records)
        markers = []
        for lap in self.current_laps:
            coords = self.lap_coords(lap)
            if coords:
                markers.append((*coords[0], str(lap.lap_index)))
        return MapLayers(
            points=_gps(records),
            values=values,
            highlights=[c for c in (self.lap_coords(lap) for lap in self.marked_laps()) if len(c) > 1],
            selected=self.lap_coords(selected_lap),
            markers=markers,
        )

    def compare_inputs(self) -> tuple[list[Lap], list[fit_parser.RecordPoint], timedelta]:
        marked = self.marked_laps()
        if len(marked) < 2:
            raise LapsError("Mark at least 2 intervals to compare")
        return marked, self.ensure_records(), self.utc_offset


def _plain_label(source: str, lap: Lap) -> str:
    return lap.label or str(lap.lap_index)


def _interval_label(source: str, lap: Lap) -> str:
    """A marked lap's label among the intervals: its source's prefix, then its own label
    ("P3", "S2"; "P:in" for a physical lap's lead-in)."""
    label = _plain_label(source, lap)
    prefix = INTERVAL_LABEL_PREFIX[source]
    return f"{prefix}:{label}" if prefix and not label.isdigit() else f"{prefix}{label}"


def _speed_from_distance(records) -> list[float | None]:
    """Speed from cumulative distance, for devices that record no speed channel."""
    values: list[float | None] = [None]
    for a, b in zip(records, records[1:]):
        seconds = (b.timestamp_utc - a.timestamp_utc).total_seconds()
        if seconds > 0 and a.distance_m is not None and b.distance_m is not None:
            values.append((b.distance_m - a.distance_m) / seconds)
        else:
            values.append(None)
    return values
