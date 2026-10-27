"""User settings (data/settings.json), edited on the settings screen of every front end.
The fields are described here once, as plain data, so the TUI, Qt and web screens can each
render the same form generically."""

from dataclasses import dataclass

import storage
import training_load as tl

# What a "sport" means for hiding: groups of FIT / Garmin sport names (see
# training_load.SPORT_FAMILIES), so hiding "strength" hides both a planned
# strength_training workout and a recorded 'training' activity.
SPORT_GROUPS = ("running", "cycling", "swimming", "strength", "hiking", "multisport", "other")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # "bool" or "sports" (a subset of SPORT_GROUPS)
    default: object
    help: str = ""


FIELDS = (
    Field(
        "hidden_sports", "Hidden sports", "sports", [],
        "Not listed among activities nor as calendar rows. Their training load still counts.",
    ),
    Field(
        "auto_archive_workouts", "Archive past workouts", "bool", True,
        "Move week plans and workout files whose dates have all passed into workouts/archive/.",
    ),
    Field(
        "show_archived_workouts", "Show archived workouts", "bool", False,
        "List workouts/archive/ on the Workouts screen too.",
    ),
    Field(
        "auto_detect_physical_laps", "Find physical laps automatically", "bool", True,
        "Look for repeated loops when an activity opens (takes a second or two on long rides); p always works.",
    ),
)
_BY_KEY = {f.key: f for f in FIELDS}


def _valid(field: Field, value) -> bool:
    if field.kind == "bool":
        return isinstance(value, bool)
    return isinstance(value, list) and all(v in SPORT_GROUPS for v in value)


def load() -> dict:
    """Every setting: the saved value when valid, else its default."""
    saved = storage.load_settings()
    return {
        f.key: saved[f.key] if f.key in saved and _valid(f, saved[f.key]) else f.default
        for f in FIELDS
    }


def save(values: dict) -> None:
    unknown = [k for k in values if k not in _BY_KEY]
    invalid = [k for k, v in values.items() if k in _BY_KEY and not _valid(_BY_KEY[k], v)]
    if unknown or invalid:
        raise ValueError(f"bad settings: {unknown + invalid}")
    storage.save_settings({**load(), **values})


def get(key: str):
    return load()[key]


def sport_group(sport: str | None) -> str:
    family = tl.sport_family(sport)
    return family if family in SPORT_GROUPS else "other"


def is_hidden(sport: str | None, values: dict | None = None) -> bool:
    return sport_group(sport) in (values or load())["hidden_sports"]
