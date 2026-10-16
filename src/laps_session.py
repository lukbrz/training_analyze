"""The laps view's state and operations, independent of any UI: which laps are shown
(device laps, a whole-activity split, detected physical laps), interval marks, per-interval
phase splits, the interval-only filter, and the mapping from displayed rows back to laps.
The Textual LapsScreen and the Qt laps page are both thin shells around a LapsSession, so
they can't drift apart in behaviour."""

from dataclasses import dataclass
from datetime import timedelta

import display
import fit_parser
import storage
from models import ActivitySummary, Lap, format_split_value, parse_split_value

DEFAULT_INTERVAL_SPLIT_M = 300.0
SPLIT_HELP = "Split size: 400 (metres), 1.5km, or a time like 60s, 1m, 1m30s, 1:30"


class LapsError(Exception):
    """Something the user should be told (bad input, nothing to act on) — not a bug."""


@dataclass
class LapRow:
    values: tuple[str, ...]
    kind: str  # "interval" (marked lap), "plain" (unmarked lap) or "phase" (split of an interval)
    lap_position: int | None  # index into LapsSession.current_laps; None for phase rows
    lap: Lap  # the lap (or phase split) this row shows


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
        self.current_laps: list[Lap] = storage.load_laps(activity.activity_id)
        # Interval marks are saved only while the device's own laps are shown; marks made on
        # a custom split are for this session only (the split itself isn't saved).
        self.viewing_device_laps = True
        # lap_index -> phase splits of that (marked) lap, shown as rows "2.1", "2.2", ...
        self.interval_splits: dict[int, list[Lap]] = {}
        self.show_intervals_only = False
        self.records: list[fit_parser.RecordPoint] | None = None
        self.utc_offset: timedelta | None = None
        self._rows: list[LapRow] = []

    # --- what to display ---------------------------------------------------------

    @property
    def columns(self) -> tuple[str, ...]:
        return display.lap_columns(self.multisport)

    @property
    def right_aligned_columns(self) -> set[int]:
        return display.lap_right_aligned_columns(self.multisport)

    def summary_text(self) -> str:
        device_laps = storage.load_laps(self.activity.activity_id) if self.multisport else []
        return display.activity_summary_text(self.activity, device_laps)

    def interval_summary(self) -> str:
        return display.interval_summary_text(self.current_laps, self.activity.sport)

    def rows(self) -> list[LapRow]:
        """The displayed rows. The Total column is the running time at the end of each row —
        of all laps, or, with the interval-only filter on, of the marked intervals alone.
        Phase rows continue the running total from their interval's start."""
        rows = []
        total = 0.0
        for position, lap in enumerate(self.current_laps):
            if self.show_intervals_only and not lap.is_interval:
                continue
            lap_start_total = total
            total += lap.duration_s
            rows.append(LapRow(
                display.lap_row(lap, str(lap.lap_index), self.activity.sport, self.multisport, lap.is_interval, total),
                "interval" if lap.is_interval else "plain",
                position,
                lap,
            ))
            phase_total = lap_start_total
            for phase_number, phase in enumerate(self.interval_splits.get(lap.lap_index, []), start=1):
                phase_total += phase.duration_s
                rows.append(LapRow(
                    display.lap_row(phase, f"  {lap.lap_index}.{phase_number}", self.activity.sport, self.multisport, True, phase_total),
                    "phase",
                    None,
                    phase,
                ))
        self._rows = rows
        return rows

    def row_lap(self, row_index: int) -> Lap | None:
        """The lap (or phase split) shown in a displayed row."""
        return self._rows[row_index].lap if 0 <= row_index < len(self._rows) else None

    def marked_laps(self) -> list[Lap]:
        return [lap for lap in self.current_laps if lap.is_interval]

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

    def _show_laps(self, laps: list[Lap], device: bool) -> None:
        self.current_laps = laps
        self.viewing_device_laps = device
        self.interval_splits = {}

    def split(self, text: str) -> None:
        """Split the whole activity by the distance/time in `text`."""
        parsed = self.parse_split(text)
        self._show_laps(self._split_records(self.ensure_records(), *parsed), device=False)

    def reset(self) -> None:
        self._show_laps(storage.load_laps(self.activity.activity_id), device=True)

    def toggle_interval(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._rows):
            return
        position = self._rows[row_index].lap_position
        if position is None:
            raise LapsError("Mark the parent lap, not a phase split")
        lap = self.current_laps[position]
        lap.is_interval = not lap.is_interval
        if not lap.is_interval:
            self.interval_splits.pop(lap.lap_index, None)
        if self.viewing_device_laps:
            storage.save_laps(self.activity.activity_id, self.current_laps)

    def split_intervals(self, text: str) -> str:
        """Split every marked lap into phases (default 300 m); returns a status message."""
        marked = self.marked_laps()
        if not marked:
            raise LapsError("Mark at least one interval (m) before splitting it into phases")
        parsed = self.parse_split(text, default=("distance", DEFAULT_INTERVAL_SPLIT_M))
        records = self.ensure_records()
        self.interval_splits = {}
        for lap in marked:
            lap_records = fit_parser.lap_record_slice(records, lap, self.utc_offset)
            if lap_records:
                phases = self._split_records(lap_records, *parsed)
                for phase in phases:
                    phase.sport = lap.sport
                self.interval_splits[lap.lap_index] = phases
        return f"Split {len(self.interval_splits)} interval(s) into {format_split_value(*parsed)} phases"

    def toggle_filter(self) -> str:
        """Toggle interval-only; returns a warning when there's nothing to show."""
        self.show_intervals_only = not self.show_intervals_only
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

    def detect_physical_laps(self, radius: float) -> list[Lap] | None:
        """Slow (scores every candidate loop anchor) — call it off the UI thread, then
        apply_physical_laps() on it."""
        return fit_parser.detect_physical_laps(self.records, self.utc_offset, close_radius_m=radius)

    def apply_physical_laps(self, laps: list[Lap] | None) -> None:
        if not laps:
            raise LapsError("No repeated course laps detected")
        self._show_laps(laps, device=False)

    def histogram_phases(self) -> list[Lap]:
        """Interval phase-splits when present (the finer view), else whatever's shown."""
        phases = [p for phases in self.interval_splits.values() for p in phases] or list(self.current_laps)
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
        activity, so call it off the UI thread). None when the activity has no GPS track."""
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
