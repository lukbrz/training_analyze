"""Export of several training weeks as one JSON document, for another agent (e.g. the
planning assistant that writes the weekly workout zips) to analyse. Built from local data
only — services.export_training fetches whatever is missing from Garmin first.

Each week lists its rows as the week view shows them: a planned workout, a completed
activity, or both when the plan was carried out. A planned workout carries its steps in the
shape of workout_schema.json (plus reps / exercise / weight for strength), a completed
activity its summary, laps (with the user's interval marks) and time in zones when cached.
Raw values are SI (seconds, metres, m/s, W, bpm); paces are also given as display strings.
Written without indentation: a 24-week export is ~250 KB, and the reader is a program.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import coaching
import fit_parser
import metrics
import services
import storage
import training_load as tl
from models import ActivitySummary, Lap, format_speed_or_pace

FORMAT = "training_analyze/export"
VERSION = 4
# What can be exported: 0 = this week so far (Monday to today), else that many complete
# Monday-Sunday weeks ending last Sunday.
CHOICES = (0, 1, 4, 12, 24)
EXPORT_PROMPT = "Export to one JSON file in the exports/ folder:"

UNITS = {
    "duration": "seconds", "distance": "metres", "speed": "m/s", "power": "W",
    "heart_rate": "bpm", "ascent": "metres", "weight": "kg",
    "pace": "m:ss per km (running/hiking), per 100 m (swimming); km/h for cycling",
    "tss": "training stress score (100 = one hour at threshold)",
    "age": "whole years, from the birth year only",
    "laps": "an activity's structure: the device's own laps (auto-lap / lap button / workout steps) in order, 'interval' = marked by the user as a work interval",
    "splits": "inside a lap: that lap cut every 300 m (runs) or 1 km (rides) from the recorded stream, the last one possibly shorter; a leftover under 30% of a split joins the one before; laps under 1.5 splits long have none",
}
ATHLETE_HELP = (
    "Top-level 'athlete': current profile and HR zones (zone_floors_bpm = lower limits of "
    "zones 1-5). Each week's 'athlete': weight (latest weigh-in up to the week's last day), "
    "average resting HR of the week, and VO2max / thresholds as Garmin had them on the "
    "week's last day; W/kg = threshold power / that weight."
)
METRICS_HELP = {
    "efficiency_factor": "output per heartbeat: normalized power / avg HR (W/bpm) for rides, speed in m/min / avg HR for runs; compare across similar sessions",
    "aerobic_decoupling_pct": "drop in output per heartbeat from the first to the second half; < 5% on a long steady session = good aerobic endurance; meaningless for intervals",
    "ramp_rate": "CTL gained over the week; > 5-8 per week is aggressive",
    "acwr": "acute:chronic workload ratio (ATL / CTL); 0.8-1.3 sweet spot, > 1.5 injury risk rises",
    "monotony": "mean daily TSS / its standard deviation over the week; > 2 = too little hard/easy variation",
    "strain": "weekly TSS x monotony",
}
ANALYSIS_GUIDE = [
    "This file is one athlete's training over the period, prepared for a coach to review it and give the athlete feedback.",
    "Read top-down: 'athlete' (who, zones, thresholds), 'trend' (one row per week), then each week's 'summary' "
    "(volume, compliance with the plan, intensity distribution, load, flags) before its 'sessions'.",
    "Each session: 'status' says whether a plan was done, missed or extra; 'execution' compares plan and reality step by step "
    "(each step's target, what was done, a verdict); 'planned' is the workout as written; 'activity' is what was recorded, "
    "its 'laps' the session's structure (watch laps / workout steps) with 'splits' inside each lap.",
    "'flags' are rule-based hints (thresholds in 'reference'), not diagnoses — confirm them against the data.",
    "Useful feedback covers: was the week's plan done, were key sessions executed as intended (targets, pacing, fade), "
    "was easy kept easy, is load progressing safely, recovery signs (resting HR, decoupling, HR drift), and what to adjust next.",
]
STATUS_HELP = {
    "done (planned)": "a planned workout and the activity that carried it out",
    "planned": "a planned workout still ahead",
    "missed": "a planned workout whose day has passed without a matching activity",
    "unplanned": "an activity with no planned workout",
}
FLAG_RULES = {
    "acwr": f"> {coaching.ACWR_CAUTION} load rising faster than fitness; > {coaching.ACWR_HIGH} injury risk rising",
    "ramp_rate": f"> {coaching.RAMP_HIGH} CTL per week",
    "monotony": f"> {coaching.MONOTONY_HIGH}",
    "resting_hr": f"week average >= {coaching.RHR_RISE_BPM:g} bpm above the 4 weeks before",
    "targets": f"a session with under {coaching.ON_TARGET_SHARE_LOW:.0%} of judged steps on target "
               f"(pace within {coaching.PACE_TOLERANCE:.0%}, HR within {coaching.HR_TOLERANCE_BPM} bpm, power within {coaching.POWER_TOLERANCE:.0%}; "
               f"HR only judged on steps of {coaching.MIN_HR_CHECK_S} s or more)",
    "easy_but_hard": f"a session planned in HR zone 1-2 with over {coaching.EASY_ABOVE_Z2_SHARE:.0%} of its time above zone 2",
    "decoupling": f"> {coaching.DECOUPLING_HIGH:g}% on a steady session of {coaching.MIN_DECOUPLING_FLAG_S // 60} min or more",
}
FITNESS_HELP = (
    "CTL (fitness) and ATL (fatigue) are 42- and 7-day exponentially weighted averages of "
    "daily TSS; TSB (form) = yesterday's CTL - ATL."
)


def _r(value: float | None, digits: int = 1) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(value, digits)


def _iso(timestamp: str | None) -> str | None:
    return timestamp.replace(" ", "T") if timestamp else timestamp


def _pace_text(speed_mps: float | None, sport: str) -> str | None:
    """A m/s speed as a pace string for the sport, or None."""
    text = format_speed_or_pace(sport, speed_mps)
    return None if text == "–" else text


def _family_sport(sport_key: str | None) -> str:
    family = tl.sport_family(sport_key)
    return {"hiking": "running"}.get(family, family)


def _target(step: dict, sport: str) -> dict | None:
    key = (step.get("targetType") or {}).get("workoutTargetTypeKey") or "no.target"
    one, two, zone = step.get("targetValueOne"), step.get("targetValueTwo"), step.get("zoneNumber")
    if key == "no.target":
        return None
    if key in ("pace.zone", "speed.zone") and one and two:
        pace_sport = "swimming" if sport == "swimming" else "running"
        pace = lambda v: (_pace_text(v, pace_sport) or "").split(" ")[0]  # noqa: E731
        # Garmin stores the slower bound first, as the workout schema does.
        return {"type": "pace", "from": pace(one), "to": pace(two)}
    kind = {"heart.rate.zone": "heart_rate", "power.zone": "power", "cadence": "cadence"}.get(key, key)
    if zone and not (one and two):
        return {"type": f"{kind}_zone", "zone": zone}
    if one is not None and two is not None:
        return {"type": kind, "from": _r(one, 0), "to": _r(two, 0)}
    return {"type": kind}


def _duration(step: dict) -> dict:
    condition = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if condition == "time" and value:
        return {"time_s": _r(value, 0)}
    if condition == "distance" and value:
        return {"distance_m": _r(value, 0)}
    if condition == "reps" and value:
        return {"reps": int(value)}
    return {"lap_button": True}


def workout_steps(steps: list[dict], sport: str) -> list[dict]:
    """Garmin workout steps in the shape of workout_schema.json."""
    out = []
    for step in steps:
        if step.get("type") == "RepeatGroupDTO" or step.get("workoutSteps"):
            iterations = int(step.get("numberOfIterations") or step.get("endConditionValue") or 1)
            out.append({"repeat": iterations, "steps": workout_steps(step.get("workoutSteps") or [], sport)})
            continue
        item = {"type": (step.get("stepType") or {}).get("stepTypeKey") or "other", "duration": _duration(step)}
        target = _target(step, sport)
        if target:
            item["target"] = target
        if step.get("description"):
            item["description"] = step["description"]
        if step.get("exerciseName"):
            item["exercise"] = step["exerciseName"].lower()
        if step.get("weightValue"):
            unit = (step.get("weightUnit") or {}).get("unitKey")
            item["weight_kg" if unit in (None, "kilogram") else f"weight_{unit}"] = step["weightValue"]
        stroke = (step.get("strokeType") or {}).get("strokeTypeKey")
        if stroke:
            item["stroke"] = stroke
        out.append(item)
    return out


def _planned(item: tl.WeekItem, title: str, day: date) -> dict:
    dto = storage.load_cached_workout(item.workout_id) if item.workout_id else None
    sport_key = ((dto or {}).get("sportType") or {}).get("sportTypeKey") or item.sport
    out = {
        "title": title,
        "date": day.isoformat(),
        "sport": sport_key,
        "garmin_workout_id": item.workout_id,
        "estimated_tss": _r(item.planned_tss, 0),
    }
    if dto is None:
        out["steps_unavailable"] = "workout not downloaded"
        return out
    estimate = tl.workout_tss(dto, services.load_thresholds())
    out["estimated_duration_s"] = _r(estimate.duration_s, 0)
    if estimate.assumed_s:
        out["estimate_note"] = f"{estimate.assumed_s:.0f} s of lap-button steps have an assumed length"
    if dto.get("description"):
        out["description"] = dto["description"]
    out["steps"] = [
        step for segment in dto.get("workoutSegments") or []
        for step in workout_steps(segment.get("workoutSteps") or [], _family_sport(sport_key))
    ]
    return out


# An activity's structure is a tree: its device laps (auto-lap / lap button / workout
# steps, with the user's interval marks) are the branches, each with its own fixed-distance
# splits as leaves — fine enough to see pacing within a rep or a long block (300 m on foot,
# 1 km on the bike). A lap under MIN_SPLITS_PER_LAP splits long has no leaves: it is
# about one split already (a 300-400 m rep). A leftover shorter than MIN_LAST_SPLIT of a
# split joins the one before it (a 2059 m lap is 1000 + 1059 m, not 1000 + 1000 + 59 m).
SPLIT_DISTANCE_M = {"running": 300.0, "cycling": 1000.0}
MIN_SPLITS_PER_LAP = 1.5
MIN_LAST_SPLIT = 0.3
# Columns of a lap / split, per kind of sport (laps carry the same keys as objects).
FOOT_COLUMNS = (
    "split", "start_local", "duration_s", "distance_m", "pace", "gap_pace", "avg_hr", "min_hr", "max_hr",
    "avg_power_w", "np_w", "cadence_spm", "ground_contact_ms", "vertical_oscillation_mm",
    "vertical_ratio_pct", "step_length_m", "ascent_m", "descent_m",
)
RIDE_COLUMNS = (
    "split", "start_local", "duration_s", "distance_m", "speed", "avg_hr", "min_hr", "max_hr",
    "avg_power_w", "np_w", "cadence_rpm", "ascent_m", "descent_m",
)


def _is_foot(sport: str) -> bool:
    return tl.sport_family(sport) in ("running", "hiking")


def _segment(segment: Lap, sport: str, records: list) -> dict:
    """A lap or split's figures — HR, power, NP, cadence, running dynamics and grade-adjusted
    pace computed exactly from its stretch of the record stream (`records`), falling back to
    the lap's own averages where the stream has nothing (e.g. no records cached)."""
    foot = _is_foot(sport)
    stats = metrics.segment_stats(records, foot) if records else {}
    hr = stats.get("avg_hr") or segment.avg_hr
    power = stats.get("avg_power_w") or segment.avg_power
    out = {
        "split": segment.lap_index,
        "start_local": _iso(segment.start_time_local),
        "duration_s": _r(segment.duration_s),
        "distance_m": _r(segment.distance_m),
        ("pace" if foot else "speed"): _pace_text(segment.avg_speed_mps, sport),
        "gap_pace": _pace_text(stats.get("gap_speed_mps"), "running") if foot else None,
        "avg_hr": _r(hr, 0), "min_hr": stats.get("min_hr"), "max_hr": stats.get("max_hr"),
        "avg_power_w": _r(power, 0), "np_w": _r(stats.get("np_w"), 0),
        ("cadence_spm" if foot else "cadence_rpm"): _r(stats.get("cadence"), 0),
        "ground_contact_ms": _r(stats.get("ground_contact_ms"), 0),
        "vertical_oscillation_mm": _r(stats.get("vertical_oscillation_mm")),
        "vertical_ratio_pct": _r(stats.get("vertical_ratio_pct")),
        "step_length_m": _r(stats.get("step_length_m"), 2),
        "ascent_m": _r(segment.total_ascent_m, 0), "descent_m": _r(segment.total_descent_m, 0),
    }
    return out


def _lap_node(lap: Lap, activity_sport: str, records: list, offset: timedelta | None) -> dict:
    sport = lap.sport or activity_sport
    lap_records = fit_parser.lap_record_slice(records, lap, offset) if offset is not None else []
    values = _segment(lap, sport, lap_records)
    node = {"lap": values.pop("split"), **{k: v for k, v in values.items() if v is not None}, "interval": lap.is_interval}
    if lap.sport and lap.sport != activity_sport:
        node["sport"] = lap.sport
    meters = SPLIT_DISTANCE_M.get(tl.sport_family(sport))
    if meters and lap.distance_m >= MIN_SPLITS_PER_LAP * meters and sum(r.distance_m is not None for r in lap_records) >= 2:
        splits = fit_parser.split_by_distance(lap_records, meters, offset, min_tail_fraction=MIN_LAST_SPLIT)
        if len(splits) > 1:
            columns = FOOT_COLUMNS if _is_foot(sport) else RIDE_COLUMNS
            rows = []
            for split in splits:
                values = _segment(split, sport, fit_parser.lap_record_slice(lap_records, split, offset))
                rows.append([values.get(c) for c in columns])
            # Drop columns with no data in any split (no power meter, no dynamics pod...).
            keep = [i for i, c in enumerate(columns) if any(row[i] is not None for row in rows)]
            node["splits"] = {
                "split_m": meters,
                "columns": [columns[i] for i in keep],
                "rows": [[row[i] for i in keep] for row in rows],
            }
    return node


def _lap_tree(a: ActivitySummary, laps: list[Lap], records: list) -> list[dict]:
    offset = fit_parser.load_utc_offset(a.activity_id, a.start_time_local) if records else None
    return [_lap_node(lap, a.sport, records, offset) for lap in laps]


def _zones(zones) -> list[dict]:
    return [
        {"zone": z.zone_number, "from": _r(z.low_boundary, 0), "to": _r(z.high_boundary, 0), "seconds": _r(z.seconds, 0)}
        for z in zones
    ]


# Strength sets are a table: column names once, one row per set.
SET_COLUMNS = ("type", "exercise", "category", "reps", "weight_kg", "duration_s", "start", "workout_step")


def _sets_table(sets: list[dict]) -> dict:
    return {
        "columns": list(SET_COLUMNS),
        "rows": [
            [s.get("type"), s.get("exercise"), s.get("category"), s.get("reps"), s.get("weight_kg"),
             _r(s.get("duration_s")), s.get("start"), s.get("step")]
            for s in sets
        ],
    }


def _records(a: ActivitySummary) -> list:
    try:
        return fit_parser.load_records(a.activity_id) if storage.has_fit(a.activity_id) else []
    except Exception:  # an unreadable FIT file just means no metrics or splits
        return []


def _activity_metrics(a: ActivitySummary, stats: dict | None, records: list) -> dict:
    m = metrics.activity_metrics(a, stats, records)
    out = {}
    if m.efficiency_factor is not None:
        out["efficiency_factor"] = _r(m.efficiency_factor, 3)
        out["efficiency_factor_unit"] = m.ef_basis
    if m.decoupling_pct is not None:
        out["aerobic_decoupling_pct"] = _r(m.decoupling_pct)
    return out


@dataclass
class _Context:
    """What every session of an export needs, loaded once."""
    activities: dict[str, ActivitySummary]
    route_names: dict[str, str]
    stats: dict[str, dict]
    hr_zones: dict
    power_zones: dict
    thresholds: tl.Thresholds


def _activity(a: ActivitySummary, item: tl.WeekItem, ctx: _Context) -> dict:
    route_names, stats = ctx.route_names, ctx.stats
    out = {
        "garmin_activity_id": a.activity_id,
        "name": a.name,
        "sport": a.sport,
        "start_local": _iso(a.start_time_local),
        "duration_s": _r(a.duration_s),
        "distance_m": _r(a.distance_m),
        "avg_speed_mps": _r(a.avg_speed_mps, 3),
        "pace": _pace_text(a.avg_speed_mps, a.sport),
        "avg_hr": a.avg_hr,
        "avg_power_w": _r(a.avg_power, 0),
        "ascent_m": _r(a.total_ascent_m, 0),
        "tss": _r(item.tss, 0),
        "tss_method": item.method,
    }
    if item.tss and a.duration_s:
        # TSS = hours x IF^2 x 100, so the session's average intensity relative to threshold is:
        out["intensity_factor"] = _r(math.sqrt(item.tss / (a.duration_s / 3600) / 100), 2)
    if a.route_id and route_names.get(a.route_id):
        out["route"] = route_names[a.route_id]
    if a.virtual:
        out["virtual"] = True
    records = _records(a)
    whole = metrics.segment_stats(records, _is_foot(a.sport)) if records else {}
    # A merged recording's summary can lack the HR / power its stream has (a virtual ride
    # whose HR and power came from the bike computer): take them from the stream.
    for key, value in (("avg_hr", whole.get("avg_hr")), ("avg_power_w", whole.get("avg_power_w"))):
        if out[key] is None and value is not None:
            out[key] = _r(value, 0)
    if whole.get("np_w"):
        out["np_w"] = _r(whole["np_w"], 0)
    if whole.get("max_hr"):
        out["max_hr"] = whole["max_hr"]
    if _is_foot(a.sport) and whole.get("gap_speed_mps"):
        out["gap_pace"] = _pace_text(whole["gap_speed_mps"], "running")
    out.update(_activity_metrics(a, stats.get(a.activity_id), records))
    zone_seconds = coaching.hr_zone_seconds(records, coaching.hr_zone_bounds(ctx.hr_zones, a.sport))
    if zone_seconds:
        out["hr_zone_minutes"] = [_r(s / 60) for s in zone_seconds]
    family = tl.sport_family(a.sport)
    power_floors = (ctx.power_zones.get(family) or {}).get("zone_floors_w")
    power_seconds = metrics.time_in_zones(records, "power", power_floors) if power_floors else None
    if power_seconds:
        out["power_zone_minutes"] = [_r(s / 60) for s in power_seconds]
    if family == "running":
        cadence = [r.cadence for r in records if r.cadence]
        if cadence:
            out["cadence"] = {
                "avg_spm": _r(sum(cadence) / len(cadence), 0),
                "reference_spm": coaching.CADENCE_TARGET_SPM,
                "time_at_or_above_reference_pct": round(sum(c >= coaching.CADENCE_TARGET_SPM for c in cadence) / len(cadence) * 100),
            }
    laps = storage.load_laps(a.activity_id)
    out["laps"] = _lap_tree(a, laps, records)
    sets = storage.load_exercise_sets(a.activity_id) if services.is_strength(a) else None
    if sets:
        out["strength_sets"] = _sets_table(sets)
    intervals = [lap for lap in laps if lap.is_interval]
    if intervals:
        out["marked_intervals"] = len(intervals)
    zones = storage.load_zones(a.activity_id)
    if zones:
        hr, power = zones
        if hr:
            out["hr_zones"] = _zones(hr)
        if power:
            out["power_zones"] = _zones(power)
    return out


def _execution(item: tl.WeekItem, activity: ActivitySummary, act: dict, ctx: _Context) -> tuple[dict | None, str | None]:
    """(plan vs execution, "done too hard" note for a planned-easy session) of a done
    planned session."""
    dto = storage.load_cached_workout(item.workout_id) if item.workout_id else None
    if dto is None:
        return None, None
    if services.is_strength(activity):
        sets = storage.load_exercise_sets(activity.activity_id)
        return (coaching.strength_execution(dto, sets) if sets else {"note": "sets not downloaded"}), None
    family = tl.sport_family(activity.sport)
    records = _records(activity)
    offset = fit_parser.load_utc_offset(activity.activity_id, activity.start_time_local) if records else None
    ex = coaching.execution(
        dto, storage.load_laps(activity.activity_id), activity, ctx.hr_zones, ctx.thresholds.ftp(family), records, offset,
    )
    estimate = tl.workout_tss(dto, ctx.thresholds)
    totals = {}
    if estimate.duration_s:
        totals["duration_vs_plan_pct"] = round(activity.duration_s / estimate.duration_s * 100)
    if item.planned_tss:
        totals["tss_vs_plan_pct"] = round(item.tss / item.planned_tss * 100)
    ex = {"totals": totals, **ex} if totals else ex
    easy_note = None
    zones = act.get("hr_zone_minutes")
    if coaching.is_easy_plan(dto) and zones and sum(zones):
        above = sum(zones[2:]) / sum(zones)
        if above > coaching.EASY_ABOVE_Z2_SHARE:
            easy_note = f"{above * 100:.0f}% of the time above HR zone 2 (avg HR {act.get('avg_hr')})"
    return ex, easy_note


def _session(item: tl.WeekItem, ctx: _Context) -> dict:
    out = {
        "date": item.day.isoformat(),
        "weekday": item.day.strftime("%A"),
        "status": item.status,
        "sport": item.sport,
        "title": item.title,
        "tss": _r(item.tss, 0),
    }
    activity = ctx.activities.get(item.activity_id) if item.activity_id else None
    act = _activity(activity, item, ctx) if activity is not None else None
    if item.status == "done (planned)":
        out["paired_by"] = "user link" if item.linked else "same day and sport"
        if activity is not None:
            ex, easy_note = _execution(item, activity, act, ctx)
            if ex:
                out["execution"] = ex
            if easy_note:
                out["easy_but_hard"] = easy_note
        out["planned"] = _planned(item, item.plan_title or "", item.plan_day or item.day)
    elif item.status in ("planned", "missed"):
        out["planned"] = _planned(item, item.title, item.day)
    if act is not None:
        out["activity"] = act
    return out


def _week_summary(sessions: list[dict], load: dict, fitness: dict | None, athlete: dict, rhr_base: float | None) -> dict:
    """The week at a glance: volume per sport, plan compliance, intensity distribution,
    load, athlete markers and flags."""
    done = [s for s in sessions if s["status"] in ("done (planned)", "unplanned")]
    by_sport: dict[str, dict] = {}
    zone_minutes = [0.0] * 5
    for s in done:
        act = s.get("activity") or {}
        family = tl.sport_family(act.get("sport") or s["sport"])
        entry = by_sport.setdefault(family, {"sessions": 0, "time_min": 0.0, "distance_km": 0.0, "tss": 0})
        entry["sessions"] += 1
        entry["time_min"] += (act.get("duration_s") or 0) / 60
        entry["distance_km"] += (act.get("distance_m") or 0) / 1000
        entry["tss"] += s.get("tss") or 0
        for i, minutes in enumerate(act.get("hr_zone_minutes") or []):
            zone_minutes[i] += minutes or 0
    for entry in by_sport.values():
        entry["time_min"], entry["distance_km"] = round(entry["time_min"]), _r(entry["distance_km"])

    with_plan = [s for s in sessions if s["status"] in ("done (planned)", "missed", "planned")]
    planned_tss = sum((s.get("planned") or {}).get("estimated_tss") or 0 for s in with_plan)
    done_tss = sum(s.get("tss") or 0 for s in done)
    summary = {
        "sessions": {
            "done_as_planned": sum(s["status"] == "done (planned)" for s in sessions),
            "missed": sum(s["status"] == "missed" for s in sessions),
            "extra_unplanned": sum(s["status"] == "unplanned" for s in sessions),
            "still_planned": sum(s["status"] == "planned" for s in sessions),
        },
        "volume_by_sport": by_sport,
        "tss": {"done": _r(done_tss, 0), "planned": _r(planned_tss, 0),
                **({"done_vs_planned_pct": round(done_tss / planned_tss * 100)} if planned_tss else {})},
    }
    if any(zone_minutes):
        summary["time_in_hr_zones_min"] = [_r(m) for m in zone_minutes]
        summary["intensity_distribution"] = coaching.intensity_distribution(zone_minutes)
    summary["load"] = load
    summary["fitness_at_end"] = fitness
    summary["athlete"] = athlete
    if rhr_base is not None:
        summary["athlete"] = {**athlete, "resting_hr_4wk_baseline_bpm": _r(rhr_base)}
    if done:
        longest = max(done, key=lambda s: (s.get("activity") or {}).get("duration_s") or 0)
        hardest = max(done, key=lambda s: s.get("tss") or 0)
        summary["key_sessions"] = [{"date": longest["date"], "title": longest["title"], "why": "longest"}]
        if hardest is not longest:
            summary["key_sessions"].append({"date": hardest["date"], "title": hardest["title"], "why": "highest TSS"})
    summary["flags"] = coaching.week_flags(load, athlete.get("resting_hr_avg_bpm"), rhr_base, sessions)
    return summary


def _trend_row(week: dict) -> dict:
    s = week["summary"]
    sport = s["volume_by_sport"]
    row = {
        "week_start": week["start"],
        "tss_done": s["tss"]["done"], "tss_planned": s["tss"]["planned"],
        "hours": _r(sum(v["time_min"] for v in sport.values()) / 60),
        "run_km": (sport.get("running") or {}).get("distance_km", 0),
        "bike_km": (sport.get("cycling") or {}).get("distance_km", 0),
        "swim_km": (sport.get("swimming") or {}).get("distance_km", 0),
        "strength_sessions": (sport.get("strength") or {}).get("sessions", 0),
        "missed": s["sessions"]["missed"],
        **{k: v for k, v in (s.get("intensity_distribution") or {}).items()},
        **{k: (s.get("fitness_at_end") or {}).get(k) for k in ("ctl", "atl", "tsb")},
        **{k: s["load"].get(k) for k in ("acwr", "monotony")},
        "resting_hr": s["athlete"].get("resting_hr_avg_bpm"),
        "weight_kg": s["athlete"].get("weight_kg"),
    }
    return row


def _point(point: tl.PmcPoint | None, today: date) -> dict | None:
    if point is None:
        return None
    out = {"date": point.day.isoformat(), "ctl": _r(point.ctl), "atl": _r(point.atl), "tsb": _r(point.tsb)}
    if point.day > today:
        out["projected"] = True
    return out


def _week_metrics(points: list[tl.PmcPoint]) -> dict:
    m = metrics.week_metrics(points)
    out = {"ramp_rate": _r(m.ramp_rate), "acwr": _r(m.acwr, 2), "monotony": _r(m.monotony, 2), "strain": _r(m.strain, 0)}
    return {k: v for k, v in out.items() if v is not None}


def _latest(series: dict[str, float], until: date) -> tuple[str, float] | None:
    """The last (date, value) of a daily series on or before `until`."""
    dates = [d for d in series if d <= until.isoformat()]
    return (max(dates), series[max(dates)]) if dates else None


def _week_athlete(history: dict[str, dict[str, float]], start: date, end: date) -> dict:
    """The athlete's parameters for one week: weight, VO2max and thresholds as of its last
    day (each with the date it was last updated where that matters), and resting HR as the
    week's average."""
    out: dict = {}
    weight = _latest(history.get("weight_kg", {}), end)
    if weight:
        out["weight_kg"], out["weight_date"] = _r(weight[1]), weight[0]
    rhr = [v for d, v in history.get("resting_hr_bpm", {}).items() if start.isoformat() <= d <= end.isoformat()]
    if rhr:
        out["resting_hr_avg_bpm"] = _r(sum(rhr) / len(rhr))
    for name in ("vo2max_running", "vo2max_cycling", "ftp_cycling_w", "threshold_power_running_w", "lthr_running_bpm"):
        latest = _latest(history.get(name, {}), end)
        if latest:
            out[name] = _r(latest[1])
    speed = _latest(history.get("threshold_speed_running_mps", {}), end)
    if speed:
        out["threshold_speed_running_mps"] = _r(speed[1], 3)
        out["threshold_pace_running"] = _pace_text(speed[1], "running")
    if weight:
        for name, out_name in (("ftp_cycling_w", "ftp_cycling_w_per_kg"), ("threshold_power_running_w", "threshold_power_running_w_per_kg")):
            if out.get(name):
                out[out_name] = _r(out[name] / weight[1], 2)
    return out


def _athlete(profile: dict, today: date) -> dict:
    out = {
        "gender": profile.get("gender"),
        "age_years": today.year - profile["birth_year"] if profile.get("birth_year") else None,
        "height_cm": profile.get("height_cm"),
        "hr_zones": profile.get("hr_zones"),
    }
    return {k: v for k, v in out.items() if v}


def _thresholds(th: tl.Thresholds) -> dict:
    out = {
        "ftp_cycling_w": th.ftp_cycling_w,
        "threshold_power_running_w": th.ftp_running_w,
        "lthr_running_bpm": th.lthr_running,
        "lthr_cycling_bpm": th.lthr_cycling,
        "lthr_default_bpm": th.lthr_default,
        "threshold_speed_running_mps": _r(th.threshold_speed_running_mps, 3),
        "threshold_pace_running": _pace_text(th.threshold_speed_running_mps, "running"),
    }
    return {k: v for k, v in out.items() if v is not None}


def period(weeks: int, today: date) -> tuple[date, date, date]:
    """(first Monday, last Monday, last day) of a CHOICES entry: this week up to today for
    0, else `weeks` complete weeks ending last Sunday."""
    this_monday = tl.week_start(today)
    if weeks == 0:
        return this_monday, this_monday, today
    last_monday = this_monday - timedelta(days=7)
    return last_monday - timedelta(days=7 * (weeks - 1)), last_monday, last_monday + timedelta(days=6)


def choice_labels(today: date) -> list[str]:
    """One pick-list line per CHOICES entry, naming the period it covers."""
    labels = []
    for weeks in CHOICES:
        first, _, last = period(weeks, today)
        name = "This week so far" if weeks == 0 else f"Last {weeks} full week{'s' if weeks > 1 else ''}"
        labels.append(f"{name}  ({first:%a %d.%m.%Y} – {last:%a %d.%m.%Y})")
    return labels


def file_name(weeks: int, today: date) -> str:
    first, _, last = period(weeks, today)
    return f"training_{first:%Y-%m-%d}_{last:%Y-%m-%d}_{'this-week' if weeks == 0 else f'{weeks}w'}.json"


def build(weeks: int, today: date | None = None, notes: list[str] | None = None) -> dict:
    """The export document for a CHOICES entry (see `period`): every day from its first
    Monday to its last day — Sunday, or today for this week so far."""
    today = today or date.today()
    period_start, last_monday, period_end = period(weeks, today)
    starts = [period_start + timedelta(days=7 * n) for n in range((last_monday - period_start).days // 7 + 1)]
    _, activities = storage.load_index()
    by_id = {a.activity_id: a for a in activities}
    route_names = {r.route_id: r.name for r in storage.load_routes()}
    _, stats = storage.load_activity_stats()
    athlete = storage.load_athlete()
    profile = athlete.get("profile") or {}
    history = athlete.get("history") or {}
    notes = list(notes or [])
    if not history:
        notes.append("Athlete parameters (weight, resting HR, VO2max, threshold history) aren't downloaded — export while logged in")

    last = services.load_training_week(last_monday, today, history_days=(today - period_start).days + 7)
    ctx = _Context(by_id, route_names, stats, profile.get("hr_zones") or {}, profile.get("power_zones") or {}, last.thresholds)
    points = {p.day: p for p in last.pmc}
    week_docs = []
    for start in starts:
        view = services.load_training_week(start, today)
        if not view.calendar_cached:
            notes.append(f"Garmin calendar for the week of {start} isn't downloaded: its planned workouts are missing")
        end = min(start + timedelta(days=6), period_end)
        items = [i for i in view.week.items if i.day <= end]
        sessions = [_session(i, ctx) for i in items]
        summary = _week_summary(
            sessions,
            _week_metrics([p for d, p in sorted(points.items()) if start <= d <= end]),
            _point(points.get(end), today),
            _week_athlete(history, start, end),
            coaching.rhr_baseline(history.get("resting_hr_bpm", {}), start),
        )
        week_docs.append({
            "week": start.isocalendar().week,
            "start": start.isoformat(),
            "end": end.isoformat(),
            **({"partial": "this week up to today"} if end < start + timedelta(days=6) else {}),
            "summary": summary,
            "sessions": sessions,
        })

    daily = [
        {"date": p.day.isoformat(), "tss": _r(p.tss, 0), "ctl": _r(p.ctl), "atl": _r(p.atl), "tsb": _r(p.tsb)}
        for p in last.pmc if period_start <= p.day <= period_end
    ]
    return {
        "format": FORMAT,
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {
            "kind": "this week so far" if weeks == 0 else f"last {weeks} full week(s), Monday to Sunday",
            "start": period_start.isoformat(), "end": period_end.isoformat(), "today": today.isoformat(),
        },
        "analysis_guide": ANALYSIS_GUIDE,
        "athlete": {
            **_athlete(profile, today),
            "current_thresholds": _thresholds(last.thresholds),
            "fitness_today": _point(last.today_point, today),
        },
        "trend": [_trend_row(w) for w in week_docs],
        "weeks": week_docs,
        "daily_load": daily,
        "notes": sorted(set(notes), key=notes.index),
        "reference": {
            "units": UNITS,
            "status_meanings": STATUS_HELP,
            "fitness_meaning": FITNESS_HELP,
            "metrics_meaning": METRICS_HELP,
            "athlete_meaning": ATHLETE_HELP,
            "flag_rules": FLAG_RULES,
        },
    }
