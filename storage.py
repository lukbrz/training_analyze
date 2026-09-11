import dataclasses
import json
from datetime import datetime
from pathlib import Path

from models import ActivitySummary, Lap, RecordPoint, Route, RouteSignature, ZoneTime

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
INDEX_PATH = DATA_DIR / "activities_index.json"
LAPS_DIR = DATA_DIR / "laps"
FIT_DIR = DATA_DIR / "fit"
ZONES_DIR = DATA_DIR / "zones"
ROUTES_PATH = DATA_DIR / "routes.json"
MERGED_RECORDS_DIR = DATA_DIR / "merged_records"
MERGED_LEDGER_PATH = DATA_DIR / "merged_activities.json"
SESSION_DIR = BASE_DIR / ".garmin_session"


def _filtered(cls, raw: dict) -> dict:
    """Drop any keys not in `cls`'s fields, so old JSON with a removed/renamed
    field doesn't blow up dataclass construction."""
    known = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in raw.items() if k in known}


def load_index(path: Path = INDEX_PATH) -> tuple[str | None, list[ActivitySummary]]:
    if not path.exists():
        return None, []
    data = json.loads(path.read_text(encoding="utf-8"))
    activities = [ActivitySummary(**_filtered(ActivitySummary, a)) for a in data.get("activities", [])]
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
    return [Lap(**_filtered(Lap, lap)) for lap in data]


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


def load_routes(path: Path = ROUTES_PATH) -> list[Route]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    routes = []
    for raw in data:
        signature = RouteSignature(**_filtered(RouteSignature, raw["signature"]))
        fields = _filtered(Route, raw)
        fields["signature"] = signature
        routes.append(Route(**fields))
    return routes


def save_routes(routes: list[Route], path: Path = ROUTES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([dataclasses.asdict(r) for r in routes], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_merged_records(
    activity_id: str, records: list[RecordPoint], records_dir: Path = MERGED_RECORDS_DIR
) -> None:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{activity_id}.json"
    data = [{**dataclasses.asdict(r), "timestamp_utc": r.timestamp_utc.isoformat()} for r in records]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_merged_records(
    activity_id: str, records_dir: Path = MERGED_RECORDS_DIR
) -> list[RecordPoint] | None:
    path = records_dir / f"{activity_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for raw in data:
        fields = _filtered(RecordPoint, raw)
        fields["timestamp_utc"] = datetime.fromisoformat(fields["timestamp_utc"])
        records.append(RecordPoint(**fields))
    return records


def load_merge_ledger(path: Path = MERGED_LEDGER_PATH) -> dict[str, str]:
    """Maps a duplicate ("secondary") activity_id to the primary activity_id it was
    merged into. A secondary's own .fit/laps/zones files are never deleted — only its
    entry is dropped from the visible activities index — so the merge stays fully
    re-derivable (and safe to recompute on every sync/reprocess) from this ledger plus
    both raw .fit files."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_merge_ledger(ledger: dict[str, str], path: Path = MERGED_LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def load_zones(
    activity_id: str, zones_dir: Path = ZONES_DIR
) -> tuple[list[ZoneTime], list[ZoneTime]] | None:
    path = zones_dir / f"{activity_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    hr_zones = [ZoneTime(**_filtered(ZoneTime, z)) for z in data.get("hr_zones", [])]
    power_zones = [ZoneTime(**_filtered(ZoneTime, z)) for z in data.get("power_zones", [])]
    return hr_zones, power_zones


def save_zones(
    activity_id: str,
    hr_zones: list[ZoneTime],
    power_zones: list[ZoneTime],
    zones_dir: Path = ZONES_DIR,
) -> None:
    zones_dir.mkdir(parents=True, exist_ok=True)
    path = zones_dir / f"{activity_id}.json"
    data = {
        "hr_zones": [dataclasses.asdict(z) for z in hr_zones],
        "power_zones": [dataclasses.asdict(z) for z in power_zones],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
