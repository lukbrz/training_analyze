"""Coaching-oriented analysis of a training week, for the export a coach model reads to give
the athlete feedback: how each planned session was executed (step by step against its
targets), where the week's time went by heart-rate zone, how much of the plan was done, and
rule-based flags worth a coach's attention. Pure logic over local data, no UI, no network.
"""

from datetime import date, timedelta

import fit_parser
import metrics
import training_load as tl
from models import ActivitySummary, Lap, RecordPoint, format_speed_or_pace

# Margins before a lap counts as off target: pace within 2% of the range, HR within 3 bpm,
# power within 3%.
PACE_TOLERANCE = 0.02
HR_TOLERANCE_BPM = 3
POWER_TOLERANCE = 0.03
# HR trails effort by a minute or two, so an average-HR target is only judged on steps at
# least this long.
MIN_HR_CHECK_S = 180
# Duration of a planned step vs its lap, within this fraction = "as planned".
DURATION_TOLERANCE = 0.1
# Coggan power zones as fractions of FTP (lower bounds of zones 1-7).
POWER_ZONE_FLOORS = (0.0, 0.56, 0.76, 0.91, 1.06, 1.21, 1.51)
MAX_GAP_S = 10  # a longer gap between records is a pause, not time spent in a zone
# Steps where coming in under an HR/power target is fine (easier than planned), not a miss.
EASY_STEP_TYPES = ("warmup", "cooldown", "recovery", "rest")

# Rule thresholds for weekly flags.
ACWR_CAUTION, ACWR_HIGH = 1.3, 1.5
RAMP_HIGH = 8.0
MONOTONY_HIGH = 2.0
RHR_RISE_BPM = 3.0
DECOUPLING_HIGH = 5.0
MIN_DECOUPLING_FLAG_S = 60 * 60
EASY_HR_ZONE_MAX = 2  # a session whose plan keeps HR in zone 1-2 is an easy one
EASY_ABOVE_Z2_SHARE = 0.2  # ... and counts as done too hard above this share of time over zone 2
ON_TARGET_SHARE_LOW = 0.7
# The athlete's running-cadence reference: share of running time at or above it is reported.
CADENCE_TARGET_SPM = 166


def _hr_family(sport: str | None) -> str:
    family = tl.sport_family(sport)
    return family if family in ("running", "cycling") else "default"


def hr_zone_bounds(hr_zones: dict, sport: str | None) -> list[tuple[float, float]] | None:
    """[(low, high)] bpm of zones 1-5 for a sport, from the athlete's Garmin zones."""
    zones = hr_zones.get(_hr_family(sport)) or hr_zones.get("default") or {}
    floors, max_hr = zones.get("zone_floors_bpm"), zones.get("max_hr_bpm")
    if not floors or not max_hr:
        return None
    return [(floors[i], (floors[i + 1] - 1) if i + 1 < len(floors) else max_hr) for i in range(len(floors))]


def expand_steps(dto: dict) -> list[dict]:
    """A Garmin workout's executable steps in the order the watch runs them (repeats
    unrolled) — one per lap when the workout is followed on the watch."""
    def expand(steps):
        for step in steps:
            if step.get("type") == "RepeatGroupDTO" or step.get("workoutSteps"):
                for _ in range(int(step.get("numberOfIterations") or step.get("endConditionValue") or 1)):
                    yield from expand(step.get("workoutSteps") or [])
            else:
                yield step
    return [s for segment in dto.get("workoutSegments") or [] for s in expand(segment.get("workoutSteps") or [])]


def _pace(speed: float | None, sport: str) -> str | None:
    text = format_speed_or_pace(sport, speed)
    return None if text == "–" else text


def _target_range(step: dict, sport: str, hr_zones: dict, ftp: float | None) -> dict | None:
    """The step's target as {"metric", "low", "high", "text"} in comparable units: speed in
    m/s, HR in bpm, power in W. None when it has no target this can judge."""
    key = (step.get("targetType") or {}).get("workoutTargetTypeKey") or "no.target"
    one, two, zone = step.get("targetValueOne"), step.get("targetValueTwo"), step.get("zoneNumber")
    if key in ("pace.zone", "speed.zone") and one and two:
        low, high = sorted((one, two))
        return {"metric": "speed", "low": low, "high": high, "text": f"{_pace(low, sport)} – {_pace(high, sport)}"}
    if key == "heart.rate.zone":
        if one and two:
            return {"metric": "hr", "low": one, "high": two, "text": f"{one:.0f}-{two:.0f} bpm"}
        bounds = hr_zone_bounds(hr_zones, sport)
        if zone and bounds and 1 <= zone <= len(bounds):
            low, high = bounds[zone - 1]
            return {"metric": "hr", "low": low, "high": high, "text": f"HR zone {zone} ({low:.0f}-{high:.0f} bpm)", "zone": zone}
    if key == "power.zone":
        if one and two:
            return {"metric": "power", "low": one, "high": two, "text": f"{one:.0f}-{two:.0f} W"}
        if zone and ftp and 1 <= zone <= len(POWER_ZONE_FLOORS):
            low = POWER_ZONE_FLOORS[zone - 1] * ftp
            high = POWER_ZONE_FLOORS[zone] * ftp - 1 if zone < len(POWER_ZONE_FLOORS) else 2.5 * ftp
            return {"metric": "power", "low": low, "high": high, "text": f"power zone {zone} ({low:.0f}-{high:.0f} W)", "zone": zone}
    return None


def _check(target: dict, lap: Lap, sport: str, step_type: str) -> dict:
    """Judge one lap against its step's target: verdict + how far off. Under an HR or power
    target on an easy step (warm-up, cool-down, recovery) is fine, not a miss."""
    metric = target["metric"]
    if metric == "hr" and lap.duration_s < MIN_HR_CHECK_S:
        return {"verdict": "not judged (step too short for HR)"}
    actual = {"speed": lap.avg_speed_mps, "hr": lap.avg_hr, "power": lap.avg_power}[metric]
    if actual is None:
        return {"verdict": "no data"}
    low, high = target["low"], target["high"]
    margin = {"speed": high * PACE_TOLERANCE, "hr": HR_TOLERANCE_BPM, "power": high * POWER_TOLERANCE}[metric]
    if low - margin <= actual <= high + margin:
        return {"verdict": "on target"}
    over = actual > high
    if metric == "speed":
        if sport in ("running", "hiking", "swimming"):
            per = 100 if sport == "swimming" else 1000
            diff = abs(per / actual - per / (high if over else low))
            return {"verdict": "faster than target" if over else "slower than target",
                    "off_by": f"{diff:.0f} s/{'100m' if per == 100 else 'km'}"}
        return {"verdict": "faster than target" if over else "slower than target",
                "off_by": f"{abs(actual - (high if over else low)) * 3.6:.1f} km/h"}
    unit = "bpm" if metric == "hr" else "W"
    if not over and step_type in EASY_STEP_TYPES:
        return {"verdict": "easier than planned (fine for this step)",
                "off_by": f"{abs(actual - low):.0f} {unit}"}
    return {"verdict": f"{'above' if over else 'below'} target",
            "off_by": f"{abs(actual - (high if over else low)):.0f} {unit}"}


def _planned_amount(step: dict) -> dict:
    condition = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if condition == "time" and value:
        return {"time_s": round(value)}
    if condition == "distance" and value:
        return {"distance_m": round(value)}
    return {"until": "lap button"}


def _amount_check(planned: dict, lap: Lap) -> str | None:
    if "time_s" in planned:
        planned_value, actual = planned["time_s"], lap.duration_s
    elif "distance_m" in planned:
        planned_value, actual = planned["distance_m"], lap.distance_m
    else:
        return None
    if not planned_value:
        return None
    ratio = actual / planned_value
    if abs(ratio - 1) <= DURATION_TOLERANCE:
        return "as planned"
    return f"{'longer' if ratio > 1 else 'shorter'} than planned ({ratio * 100:.0f}%)"


def execution(
    dto: dict, laps: list[Lap], activity: ActivitySummary, hr_zones: dict, ftp: float | None,
    records: list[RecordPoint], offset: timedelta | None,
) -> dict:
    """How a planned endurance workout was executed: when the activity's laps line up one
    to one with the workout's steps (it was followed on the watch), each step against its
    target — with max HR, NP and grade-adjusted pace from the record stream, and HR recovery
    on recovery/rest steps; otherwise a note saying why steps can't be compared."""
    steps = expand_steps(dto)
    sport = activity.sport
    if len(steps) != len(laps):
        return {
            "steps_compared": False,
            "note": f"{len(laps)} laps vs {len(steps)} planned steps — the workout wasn't followed step by step "
                    "on the watch (or laps were pressed by hand), so only the totals compare",
        }
    foot = tl.sport_family(sport) in ("running", "hiking")
    out_steps, judged, on_target = [], 0, 0
    for n, (step, lap) in enumerate(zip(steps, laps), start=1):
        planned = _planned_amount(step)
        step_type = (step.get("stepType") or {}).get("stepTypeKey") or "other"
        lap_records = fit_parser.lap_record_slice(records, lap, offset) if offset is not None else []
        stats = metrics.segment_stats(lap_records, foot) if lap_records else {}
        entry = {
            "step": n,
            "type": step_type,
            "planned": planned,
            "actual": {
                "duration_s": round(lap.duration_s, 1), "distance_m": round(lap.distance_m),
                "pace": _pace(lap.avg_speed_mps, sport),
                "gap_pace": _pace(stats.get("gap_speed_mps"), "running") if foot else None,
                "avg_hr": round(stats["avg_hr"]) if stats.get("avg_hr") else lap.avg_hr,
                "max_hr": stats.get("max_hr"),
                "avg_power_w": round(stats.get("avg_power_w") or lap.avg_power) if (stats.get("avg_power_w") or lap.avg_power) else None,
                "np_w": round(stats["np_w"]) if stats.get("np_w") else None,
            },
        }
        if step_type in ("recovery", "rest") and lap_records:
            recovery = metrics.hr_recovery(lap_records)
            if recovery:
                entry["hr_recovery"] = recovery
        if step.get("description"):
            entry["note"] = step["description"]
        amount = _amount_check(planned, lap)
        if amount:
            entry["amount"] = amount
        target = _target_range(step, sport, hr_zones, ftp)
        if target:
            entry["planned"]["target"] = target["text"]
            check = _check(target, lap, sport, entry["type"])
            entry["target_check"] = check
            if check["verdict"] not in ("no data",) and not check["verdict"].startswith("not judged"):
                judged += 1
                on_target += check["verdict"] == "on target" or check["verdict"].startswith("easier")
        entry["actual"] = {k: v for k, v in entry["actual"].items() if v is not None}
        out_steps.append(entry)
    result = {"steps_compared": True, "steps": out_steps}
    if judged:
        result["targets_hit"] = f"{on_target}/{judged}"
    return result


def strength_execution(dto: dict, sets: list[dict]) -> dict:
    """Planned vs done per exercise: sets x reps @ kg."""
    def summary(entries):
        return [f"{e['reps']}" + (f" @ {e['weight_kg']:g} kg" if e.get("weight_kg") else "") for e in entries]

    planned: dict[str, list[dict]] = {}
    for step in expand_steps(dto):
        condition = (step.get("endCondition") or {}).get("conditionTypeKey")
        if condition == "reps" and step.get("exerciseName"):
            planned.setdefault(step["exerciseName"].lower(), []).append(
                {"reps": int(step.get("endConditionValue") or 0), "weight_kg": step.get("weightValue")}
            )
    done: dict[str, list[dict]] = {}
    for s in sets:
        if s.get("type") == "active" and s.get("reps"):
            done.setdefault(s.get("exercise") or s.get("category") or "unknown", []).append(s)
    exercises = []
    for name in list(planned) + [n for n in done if n not in planned]:
        p, d = planned.get(name, []), done.get(name, [])
        entry = {"exercise": name.replace("_", " "), "planned": summary(p), "done": summary(d)}
        planned_reps, done_reps = sum(e["reps"] for e in p), sum(e["reps"] for e in d)
        if p and d:
            entry["reps_done_pct"] = round(done_reps / planned_reps * 100) if planned_reps else None
        elif p:
            entry["status"] = "not done"
        else:
            entry["status"] = "not planned"
        exercises.append(entry)
    return {"exercises": exercises}


def hr_zone_seconds(records: list[RecordPoint], bounds: list[tuple[float, float]] | None) -> list[float] | None:
    """Seconds spent in HR zones 1-5 (below zone 1 counts as zone 1, above 5 as 5)."""
    if not bounds:
        return None
    seconds = [0.0] * len(bounds)
    for prev, rec in zip(records, records[1:]):
        if prev.heart_rate is None:
            continue
        gap = (rec.timestamp_utc - prev.timestamp_utc).total_seconds()
        if not 0 < gap <= MAX_GAP_S:
            continue
        zone = next((i for i, (low, _) in reversed(list(enumerate(bounds))) if prev.heart_rate >= low), 0)
        seconds[zone] += gap
    return seconds if any(seconds) else None


def intensity_distribution(zone_seconds: list[float]) -> dict:
    """Share of time low (zones 1-2), moderate (3) and high (4-5), in percent."""
    total = sum(zone_seconds)
    if not total:
        return {}
    return {
        "low_pct": round((zone_seconds[0] + zone_seconds[1]) / total * 100),
        "moderate_pct": round(zone_seconds[2] / total * 100),
        "high_pct": round(sum(zone_seconds[3:]) / total * 100),
    }


def is_easy_plan(dto: dict) -> bool:
    """Every HR-zone target of the workout is zone 1-2 and nothing has a pace/power target."""
    keys = [((s.get("targetType") or {}).get("workoutTargetTypeKey"), s.get("zoneNumber")) for s in expand_steps(dto)]
    hr_zones = [z for k, z in keys if k == "heart.rate.zone" and z]
    hard = [k for k, _ in keys if k in ("pace.zone", "speed.zone", "power.zone")]
    return bool(hr_zones) and not hard and max(hr_zones) <= EASY_HR_ZONE_MAX


def week_flags(
    load: dict, rhr_week: float | None, rhr_baseline: float | None, sessions: list[dict],
) -> list[str]:
    """Rule-based observations a coach should look at — hints, not diagnoses. `sessions` are
    the export's session dicts of the week."""
    flags = []
    acwr, ramp, monotony = load.get("acwr"), load.get("ramp_rate"), load.get("monotony")
    if acwr is not None and acwr > ACWR_HIGH:
        flags.append(f"ACWR {acwr:.2f}: acute load well above chronic — injury risk rising")
    elif acwr is not None and acwr > ACWR_CAUTION:
        flags.append(f"ACWR {acwr:.2f}: load rising faster than fitness")
    if ramp is not None and ramp > RAMP_HIGH:
        flags.append(f"CTL ramp +{ramp:.1f} this week: aggressive build")
    if monotony is not None and monotony > MONOTONY_HIGH:
        flags.append(f"monotony {monotony:.1f}: little difference between hard and easy days")
    if rhr_week is not None and rhr_baseline is not None and rhr_week - rhr_baseline >= RHR_RISE_BPM:
        flags.append(f"resting HR {rhr_week:.0f} bpm, {rhr_week - rhr_baseline:+.0f} vs 4-week baseline: possible fatigue, illness or stress")
    for s in sessions:
        title = f"{s['date']} {s['title']}"
        if s["status"] == "missed":
            flags.append(f"missed: {title} (planned ~{s.get('tss', 0)} TSS)")
        ex = s.get("execution") or {}
        hit = ex.get("targets_hit")
        if hit:
            on, judged = (int(x) for x in hit.split("/"))
            if judged and on / judged < ON_TARGET_SHARE_LOW:
                flags.append(f"{title}: only {hit} steps on target")
        if s.get("easy_but_hard"):
            flags.append(f"{title}: planned easy (HR zone 1-2) but {s['easy_but_hard']}")
        act = s.get("activity") or {}
        dec = act.get("aerobic_decoupling_pct")
        if dec is not None and dec > DECOUPLING_HIGH and act.get("duration_s", 0) >= MIN_DECOUPLING_FLAG_S and not act.get("marked_intervals", 0) > 1:
            flags.append(f"{title}: aerobic decoupling {dec:.1f}% on a long steady session — endurance or fuelling/heat limit")
    return flags


def rhr_baseline(history: dict[str, float], week_start: date) -> float | None:
    """Average resting HR over the 4 weeks before a week."""
    start = (week_start - timedelta(days=28)).isoformat()
    values = [v for d, v in history.items() if start <= d < week_start.isoformat()]
    return sum(values) / len(values) if values else None
