import dataclasses
import json
from pathlib import Path

from models import ActivitySummary, Lap

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
INDEX_PATH = DATA_DIR / "activities_index.json"
LAPS_DIR = DATA_DIR / "laps"
FIT_DIR = DATA_DIR / "fit"
SESSION_DIR = BASE_DIR / ".garmin_session"


def load_index(path: Path = INDEX_PATH) -> tuple[str | None, list[ActivitySummary]]:
    if not path.exists():
        return None, []
    data = json.loads(path.read_text(encoding="utf-8"))
    activities = [ActivitySummary(**a) for a in data.get("activities", [])]
    return data.get("last_synced_date"), activities


def save_index(
    last_synced_date: str | None,
    activities: list[ActivitySummary],
    path: Path = INDEX_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "last_synced_date": last_synced_date,
        "activities": [dataclasses.asdict(a) for a in activities],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_laps(activity_id: str, laps_dir: Path = LAPS_DIR) -> list[Lap]:
    path = laps_dir / f"{activity_id}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Lap(**lap) for lap in data]


def save_laps(activity_id: str, laps: list[Lap], laps_dir: Path = LAPS_DIR) -> None:
    laps_dir.mkdir(parents=True, exist_ok=True)
    path = laps_dir / f"{activity_id}.json"
    path.write_text(
        json.dumps([dataclasses.asdict(lap) for lap in laps], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def fit_path(activity_id: str, fit_dir: Path = FIT_DIR) -> Path:
    return fit_dir / f"{activity_id}.fit"


def has_fit(activity_id: str, fit_dir: Path = FIT_DIR) -> bool:
    return fit_path(activity_id, fit_dir).exists()


def save_fit(activity_id: str, fit_bytes: bytes, fit_dir: Path = FIT_DIR) -> None:
    fit_dir.mkdir(parents=True, exist_ok=True)
    fit_path(activity_id, fit_dir).write_bytes(fit_bytes)
