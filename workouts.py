"""Load agent-written workout JSON (see workout_schema.json) and convert it to the DTO shape
Garmin Connect's workout service accepts.

The DTO field names, type IDs and target-value conventions below were taken from real
workouts read back from Garmin Connect and from its /workout-service/workout/types
catalogue — not guessed — except the swim pool-length unit, which no existing workout on the
account had (see POOL_LENGTH_UNIT).
"""

import json
import zipfile
from pathlib import Path, PurePosixPath

import jsonschema

from models import format_duration

SCHEMA_PATH = Path(__file__).parent / "workout_schema.json"

SPORT_TYPES = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swimming": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 4},
}
STEP_TYPES = {
    "warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6, "other": 7,
}
END_CONDITIONS = {
    "lap.button": {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True},
    "time": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
    "distance": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
    "iterations": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
}
TARGET_TYPES = {
    "no.target": 1, "power.zone": 2, "cadence": 3, "heart.rate.zone": 4, "pace.zone": 6,
}
STROKE_TYPES = {
    "any_stroke": 1, "backstroke": 2, "breaststroke": 3, "drill": 4, "fly": 5, "free": 6,
    "individual_medley": 7, "mixed": 8,
}
EQUIPMENT_TYPES = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4, "snorkel": 5}
# Unverified: Garmin unit dicts elsewhere look like {"unitId", "unitKey", "factor"}, and metre
# is unit 1 (factor 100, i.e. stored in cm). If a swim upload is rejected, check this first.
POOL_LENGTH_UNIT = {"unitId": 1, "unitKey": "meter", "factor": 100.0}


class WorkoutError(ValueError):
    pass


# A week's plan is a handful of small JSON files; anything far beyond that is not a plan.
MAX_ARCHIVE_MEMBERS = 100
MAX_MEMBER_BYTES = 1_000_000


def extract_archive(zip_path: Path, dest_dir: Path) -> list[str]:
    """Extract the workout JSON files from a week's zip into `dest_dir`; returns the names of
    members that were ignored (non-JSON files, macOS metadata).

    The archive comes from another agent, so extraction is deliberately narrow: only `.json`
    members, written under their bare file name (any folders inside the zip are flattened,
    which also makes "../" path escapes impossible), with count and size limits checked
    against the bytes actually read rather than the sizes the zip headers claim. The whole
    archive is read and checked before anything is written, so a bad archive leaves the
    previous extraction untouched. Re-extracting replaces only the folder's .json files.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise WorkoutError(f"{zip_path.name}: not a valid zip archive ({e})") from e

    files: dict[str, bytes] = {}
    ignored: list[str] = []
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise WorkoutError(f"{zip_path.name}: {len(members)} files, more than the {MAX_ARCHIVE_MEMBERS} allowed")
        for info in members:
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if "__MACOSX" in info.filename or name.startswith(".") or not name.lower().endswith(".json"):
                ignored.append(info.filename)
                continue
            with archive.open(info) as member:
                data = member.read(MAX_MEMBER_BYTES + 1)
            if len(data) > MAX_MEMBER_BYTES:
                raise WorkoutError(f"{zip_path.name}: {info.filename} is larger than {MAX_MEMBER_BYTES} bytes")
            if name in files:
                raise WorkoutError(f"{zip_path.name}: two files are named {name}")
            files[name] = data

    if not files:
        raise WorkoutError(f"{zip_path.name}: no .json workout files inside")
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in dest_dir.glob("*.json"):
        stale.unlink()
    for name, data in files.items():
        (dest_dir / name).write_bytes(data)
    return ignored


def extract_new_archives(workouts_dir: Path) -> list[str]:
    """Extract every `<week>.zip` in `workouts_dir` into `workouts_dir/<week>/`, but only when
    that folder doesn't exist yet or the zip is newer than it (a re-delivered plan). Returns
    one message per extraction or failure, for showing to the user."""
    messages = []
    for zip_path in sorted(workouts_dir.glob("*.zip")):
        dest_dir = workouts_dir / zip_path.stem
        if dest_dir.exists() and dest_dir.stat().st_mtime >= zip_path.stat().st_mtime:
            continue
        try:
            ignored = extract_archive(zip_path, dest_dir)
        except WorkoutError as e:
            messages.append(str(e))
            continue
        note = f" (ignored: {', '.join(ignored)})" if ignored else ""
        messages.append(f"Unpacked {zip_path.name} into {dest_dir.name}/{note}")
    return messages


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _pace_to_seconds(pace: str) -> int:
    minutes, secs = pace.split(":")
    return int(minutes) * 60 + int(secs)


def _iter_steps(steps: list[dict]):
    for step in steps:
        if "repeat" in step:
            yield from _iter_steps(step["steps"])
        else:
            yield step


def _most_specific(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    """A oneOf failure's own message is just "not valid under any of the given schemas".
    The deepest error among its branches points at the field that's actually wrong (e.g.
    steps/0/duration rather than "steps/0 isn't a repeat either")."""
    if not error.context:
        return error
    by_branch: dict = {}
    for sub in error.context:
        by_branch.setdefault(sub.relative_schema_path[0], []).append(sub)
    # Branches that failed on their `type` const (e.g. the "power" target branch when the
    # step says "pace") are the wrong alternative entirely, not a hint about what's wrong.
    candidates = [
        sub
        for subs in by_branch.values()
        if not any(s.validator == "const" and list(s.relative_path) == ["type"] for s in subs)
        for sub in subs
    ] or list(error.context)
    leaves = [_most_specific(sub) for sub in candidates]
    return max(leaves, key=lambda e: len(e.absolute_path))


def validate(workout: dict) -> None:
    """Raise WorkoutError describing every problem: schema violations first (with the JSON
    path, so an agent can fix its own output), then the cross-field rules a schema can't
    express cleanly."""
    validator = jsonschema.Draft202012Validator(_load_schema())
    errors = sorted(validator.iter_errors(workout), key=lambda e: list(e.absolute_path))
    if errors:
        lines = []
        for error in errors:
            error = _most_specific(error)
            lines.append(f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: {error.message}")
        raise WorkoutError("\n".join(dict.fromkeys(lines)))

    sport = workout["sport"]
    problems = []
    for step in _iter_steps(workout["steps"]):
        label = step.get("description") or step["type"]
        target = step.get("target")
        if target and target["type"] == "pace":
            if sport == "cycling":
                problems.append(f"{label}: pace targets are only for running/swimming")
            elif _pace_to_seconds(target["from"]) < _pace_to_seconds(target["to"]):
                problems.append(f"{label}: pace 'from' must be the slower pace (from {target['from']} > to {target['to']})")
        elif target and "from" in target and target["from"] > target["to"]:
            problems.append(f"{label}: {target['type']} 'from' must be <= 'to'")
        if sport != "swimming" and ("stroke" in step or "equipment" in step):
            problems.append(f"{label}: stroke/equipment are only for swimming")
    if problems:
        raise WorkoutError("\n".join(problems))


def load(path: Path) -> dict:
    try:
        workout = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise WorkoutError(f"not valid JSON: {e}") from e
    validate(workout)
    return workout


def _pace_speed_mps(pace: str, sport: str) -> float:
    meters = 100.0 if sport == "swimming" else 1000.0
    return meters / _pace_to_seconds(pace)


def _target_fields(target: dict | None, sport: str) -> dict:
    def target_type(key: str) -> dict:
        return {"workoutTargetTypeId": TARGET_TYPES[key], "workoutTargetTypeKey": key, "displayOrder": TARGET_TYPES[key]}

    if target is None:
        return {"targetType": target_type("no.target")}
    kind = target["type"]
    if kind == "pace":
        # Garmin stores pace ranges as speeds in m/s, slower bound first.
        return {
            "targetType": target_type("pace.zone"),
            "targetValueOne": round(_pace_speed_mps(target["from"], sport), 4),
            "targetValueTwo": round(_pace_speed_mps(target["to"], sport), 4),
        }
    if kind in ("heart_rate_zone", "power_zone"):
        key = "heart.rate.zone" if kind == "heart_rate_zone" else "power.zone"
        return {"targetType": target_type(key), "zoneNumber": target["zone"]}
    key = {"heart_rate": "heart.rate.zone", "power": "power.zone", "cadence": "cadence"}[kind]
    return {"targetType": target_type(key), "targetValueOne": float(target["from"]), "targetValueTwo": float(target["to"])}


def _end_condition_fields(duration: dict) -> dict:
    if "time_s" in duration:
        return {"endCondition": END_CONDITIONS["time"], "endConditionValue": float(duration["time_s"])}
    if "distance_m" in duration:
        return {"endCondition": END_CONDITIONS["distance"], "endConditionValue": float(duration["distance_m"])}
    return {"endCondition": END_CONDITIONS["lap.button"]}


class _Counter:
    def __init__(self) -> None:
        self.step_order = 0
        self.child_step_id = 0


def _convert_steps(steps: list[dict], sport: str, counter: _Counter, child_step_id: int | None) -> list[dict]:
    out = []
    for step in steps:
        counter.step_order += 1
        if "repeat" in step:
            # Real workouts number each repeat group 1, 2, ... in childStepId and stamp that
            # same id on the group's own children.
            counter.child_step_id += 1
            group_id = counter.child_step_id
            group = {
                "type": "RepeatGroupDTO",
                "stepOrder": counter.step_order,
                "stepType": {"stepTypeId": STEP_TYPES["repeat"], "stepTypeKey": "repeat", "displayOrder": STEP_TYPES["repeat"]},
                "childStepId": group_id,
                "numberOfIterations": step["repeat"],
                "endCondition": END_CONDITIONS["iterations"],
                "endConditionValue": float(step["repeat"]),
                "smartRepeat": False,
            }
            group["workoutSteps"] = _convert_steps(step["steps"], sport, counter, group_id)
            out.append(group)
            continue

        dto = {
            "type": "ExecutableStepDTO",
            "stepOrder": counter.step_order,
            "stepType": {"stepTypeId": STEP_TYPES[step["type"]], "stepTypeKey": step["type"], "displayOrder": STEP_TYPES[step["type"]]},
            "childStepId": child_step_id,
            "description": step.get("description"),
            **_end_condition_fields(step["duration"]),
            **_target_fields(step.get("target"), sport),
        }
        if sport == "swimming":
            stroke = step.get("stroke", "free")
            dto["strokeType"] = {"strokeTypeId": STROKE_TYPES[stroke], "strokeTypeKey": stroke, "displayOrder": STROKE_TYPES[stroke]}
            if "equipment" in step:
                eq = step["equipment"]
                dto["equipmentType"] = {"equipmentTypeId": EQUIPMENT_TYPES[eq], "equipmentTypeKey": eq, "displayOrder": EQUIPMENT_TYPES[eq]}
        out.append(dto)
    return out


def _estimate(steps: list[dict], sport: str, repeat: int = 1) -> tuple[float, float]:
    """(seconds, metres) — rough, for Garmin's estimated duration/distance fields. A distance
    step without a pace target, or a time step without one, contributes only what it knows."""
    seconds = meters = 0.0
    for step in steps:
        if "repeat" in step:
            s, m = _estimate(step["steps"], sport, step["repeat"])
            seconds += s
            meters += m
            continue
        duration, target = step["duration"], step.get("target")
        speed = None
        if target and target["type"] == "pace":
            speed = (_pace_speed_mps(target["from"], sport) + _pace_speed_mps(target["to"], sport)) / 2
        if "time_s" in duration:
            seconds += duration["time_s"]
            meters += duration["time_s"] * speed if speed else 0.0
        elif "distance_m" in duration:
            meters += duration["distance_m"]
            seconds += duration["distance_m"] / speed if speed else 0.0
    return seconds * repeat, meters * repeat


def to_garmin(workout: dict) -> dict:
    sport = workout["sport"]
    seconds, meters = _estimate(workout["steps"], sport)
    # The agent's own estimate wins: it knows how long lap-button steps are meant to take.
    seconds = workout.get("estimated_duration_s") or seconds
    dto = {
        "workoutName": workout["name"],
        "description": workout.get("description"),
        "sportType": SPORT_TYPES[sport],
        "estimatedDurationInSecs": int(round(seconds)),
        "estimatedDistanceInMeters": round(meters, 1) if meters else None,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": SPORT_TYPES[sport],
                "workoutSteps": _convert_steps(workout["steps"], sport, _Counter(), None),
            }
        ],
    }
    if sport == "swimming":
        dto["poolLength"] = workout["pool_length_m"]
        dto["poolLengthUnit"] = POOL_LENGTH_UNIT
    return {k: v for k, v in dto.items() if v is not None}


def _describe_target(target: dict | None, sport: str) -> str:
    if target is None:
        return ""
    kind = target["type"]
    if kind == "pace":
        unit = "/100m" if sport == "swimming" else "/km"
        return f"@ {target['from']}–{target['to']} {unit}"
    if kind.endswith("_zone"):
        return f"@ {'HR' if kind.startswith('heart') else 'power'} zone {target['zone']}"
    unit = {"heart_rate": "bpm", "power": "W", "cadence": "rpm"}[kind]
    return f"@ {target['from']}–{target['to']} {unit}"


def describe(workout: dict) -> list[str]:
    """Human-readable outline of a workout for previewing before upload."""
    sport = workout["sport"]

    def lines_for(steps: list[dict], indent: int) -> list[str]:
        lines = []
        for step in steps:
            pad = "  " * indent
            if "repeat" in step:
                lines.append(f"{pad}{step['repeat']}x:")
                lines.extend(lines_for(step["steps"], indent + 1))
                continue
            d = step["duration"]
            amount = (
                format_duration(d["time_s"]) if "time_s" in d
                else f"{d['distance_m']:g} m" if "distance_m" in d
                else "until lap"
            )
            parts = [step["type"], amount, _describe_target(step.get("target"), sport)]
            if sport == "swimming" and step["type"] != "rest":
                parts.append(step.get("stroke", "free"))
            if step.get("description"):
                parts.append(f"— {step['description']}")
            lines.append(pad + " ".join(p for p in parts if p))
        return lines

    seconds, meters = _estimate(workout["steps"], sport)
    header = f"{workout['name']}  ({sport}"
    if sport == "swimming":
        header += f", {workout['pool_length_m']:g} m pool"
    header += f", ~{format_duration(seconds)}" if seconds else ""
    header += f", ~{meters / 1000:.2f} km)" if meters else ")"
    if workout.get("schedule_date"):
        header += f"  → scheduled {workout['schedule_date']}"
    return [header, *lines_for(workout["steps"], 0)]
