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

from datetime import date, datetime, timedelta

import services
import storage
import training_load as tl
from models import ActivitySummary, Lap, format_speed_or_pace

FORMAT = "training_analyze/export"
VERSION = 1
WEEK_CHOICES = (1, 4, 12, 24)

UNITS = {
    "duration": "seconds", "distance": "metres", "speed": "m/s", "power": "W",
    "heart_rate": "bpm", "ascent": "metres", "weight": "kg",
    "pace": "m:ss per km (running/hiking), per 100 m (swimming); km/h for cycling",
    "tss": "training stress score (100 = one hour at threshold)",
}
STATUS_HELP = {
    "done (planned)": "a planned workout and the activity that carried it out",
    "planned": "a planned workout still ahead",
    "missed": "a planned workout whose day has passed without a matching activity",
    "unplanned": "an activity with no planned workout",
}
FITNESS_HELP = (
    "CTL (fitness) and ATL (fatigue) are 42- and 7-day exponentially weighted averages of "
    "daily TSS; TSB (form) = yesterday's CTL - ATL. Days after today are projected from the "
    "planned workouts."
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


# Laps are a table — column names once, one row per lap — since activities can have dozens.
LAP_COLUMNS = (
    "lap", "start_local", "duration_s", "distance_m", "avg_speed_mps", "pace", "avg_hr",
    "avg_power_w", "ascent_m", "descent_m", "interval",
)


def _lap_row(lap: Lap, activity_sport: str, multisport: bool) -> list:
    sport = lap.sport or activity_sport
    row = [
        lap.lap_index, _iso(lap.start_time_local), _r(lap.duration_s), _r(lap.distance_m),
        _r(lap.avg_speed_mps, 3), _pace_text(lap.avg_speed_mps, sport), lap.avg_hr,
        _r(lap.avg_power, 0), _r(lap.total_ascent_m, 0), _r(lap.total_descent_m, 0), lap.is_interval,
    ]
    return row + [sport] if multisport else row


def _laps_table(laps: list[Lap], activity_sport: str) -> dict:
    multisport = any(lap.sport and lap.sport != activity_sport for lap in laps)
    return {
        "columns": list(LAP_COLUMNS) + (["sport"] if multisport else []),
        "rows": [_lap_row(lap, activity_sport, multisport) for lap in laps],
    }


def _zones(zones) -> list[dict]:
    return [
        {"zone": z.zone_number, "from": _r(z.low_boundary, 0), "to": _r(z.high_boundary, 0), "seconds": _r(z.seconds, 0)}
        for z in zones
    ]


def _activity(a: ActivitySummary, item: tl.WeekItem, route_names: dict[str, str]) -> dict:
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
    if a.route_id and route_names.get(a.route_id):
        out["route"] = route_names[a.route_id]
    if a.virtual:
        out["virtual"] = True
    laps = storage.load_laps(a.activity_id)
    out["laps"] = _laps_table(laps, a.sport)
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


def _item(item: tl.WeekItem, activities: dict[str, ActivitySummary], route_names: dict[str, str]) -> dict:
    out = {
        "date": item.day.isoformat(),
        "weekday": item.day.strftime("%A"),
        "status": item.status,
        "sport": item.sport,
        "title": item.title,
        "tss": _r(item.tss, 0),
    }
    if item.status == "done (planned)":
        out["paired_by"] = "user link" if item.linked else "same day and sport"
        out["planned"] = _planned(item, item.plan_title or "", item.plan_day or item.day)
    elif item.status in ("planned", "missed"):
        out["planned"] = _planned(item, item.title, item.day)
    activity = activities.get(item.activity_id) if item.activity_id else None
    if activity is not None:
        out["activity"] = _activity(activity, item, route_names)
    return out


def _point(point: tl.PmcPoint | None, today: date) -> dict | None:
    if point is None:
        return None
    out = {"date": point.day.isoformat(), "ctl": _r(point.ctl), "atl": _r(point.atl), "tsb": _r(point.tsb)}
    if point.day > today:
        out["projected"] = True
    return out


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


def export_prompt(end_week: date) -> str:
    return f"Export to one JSON file (exports/ folder) the weeks ending with {end_week:%d.%m} – {end_week + timedelta(days=6):%d.%m.%Y}:"


def week_choices(end_week: date) -> list[str]:
    """One pick-list line per WEEK_CHOICES entry, naming the period it covers."""
    end = end_week + timedelta(days=6)
    return [
        f"{n} week{'s' if n > 1 else ''}  ({end_week - timedelta(days=7 * (n - 1)):%d.%m.%Y} – {end:%d.%m.%Y})"
        for n in WEEK_CHOICES
    ]


def build(end_week: date, weeks: int, today: date | None = None, notes: list[str] | None = None) -> dict:
    """The export document for `weeks` weeks ending with the week starting `end_week`."""
    today = today or date.today()
    starts = [end_week - timedelta(days=7 * n) for n in reversed(range(weeks))]
    period_start, period_end = starts[0], end_week + timedelta(days=6)
    _, activities = storage.load_index()
    by_id = {a.activity_id: a for a in activities}
    route_names = {r.route_id: r.name for r in storage.load_routes()}
    notes = list(notes or [])

    week_docs = []
    for start in starts:
        view = services.load_training_week(start, today)
        if not view.calendar_cached:
            notes.append(f"Garmin calendar for the week of {start} isn't downloaded: its planned workouts are missing")
        w = view.week
        week_docs.append({
            "week": start.isocalendar().week,
            "start": start.isoformat(),
            "end": (start + timedelta(days=6)).isoformat(),
            "tss": {"done": _r(w.done_tss, 0), "remaining": _r(w.remaining_tss, 0), "missed": _r(w.missed_tss, 0)},
            "fitness_at_week_end": _point(view.week_end_point, today),
            "items": [_item(i, by_id, route_names) for i in w.items],
        })

    last = services.load_training_week(end_week, today, history_days=(today - period_start).days + 7)
    daily = [
        {"date": p.day.isoformat(), "tss": _r(p.tss, 0), "ctl": _r(p.ctl), "atl": _r(p.atl), "tsb": _r(p.tsb), **({"projected": True} if p.day > today else {})}
        for p in last.pmc if period_start <= p.day <= period_end
    ]
    return {
        "format": FORMAT,
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"weeks": weeks, "start": period_start.isoformat(), "end": period_end.isoformat(), "today": today.isoformat()},
        "units": UNITS,
        "status_meanings": STATUS_HELP,
        "fitness_meaning": FITNESS_HELP,
        "thresholds": _thresholds(last.thresholds),
        "fitness_today": _point(last.today_point, today),
        "weeks": week_docs,
        "daily_load": daily,
        "notes": sorted(set(notes), key=notes.index),
    }
