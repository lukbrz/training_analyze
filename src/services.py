"""Application operations shared by every front end (Textual TUI and both GUIs): they talk
to Garmin, storage and the parsers, and report progress through a plain callback, so no UI
code lives here and no front end re-implements them."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import uuid

import dedupe
import fit_parser
import garmin_client
import geocode
import metrics
import routes
import settings
import storage
import training_load as tl
import workouts
from models import ActivitySummary, Route

Progress = Callable[[str], None]

INITIAL_SYNC_DAYS = 365
# Where a full-history sync starts looking (Garmin Connect launched in 2007-08).
FULL_HISTORY_START = "2005-01-01"
FULL_HISTORY_PROMPT = (
    "Download every activity in your Garmin Connect account since "
    f"{FULL_HISTORY_START[:4]}, not just the last year?\n\n"
    "Each one is a FIT download, so years of training can take a long while; Garmin may also "
    "rate-limit it part-way. Everything downloaded is kept, so just run it again to carry on."
)

# How far back the first training-stress fetch goes; later refreshes re-check the last week
# (activities can appear late, e.g. synced from a second device).
STATS_HISTORY_DAYS = 400
STATS_RECHECK_DAYS = 7
# Days of history drawn on the fitness chart.
PMC_CHART_DAYS = 90


def _months_of_week(start: date) -> list[tuple[int, int]]:
    end = start + timedelta(days=6)
    return sorted({(start.year, start.month), (end.year, end.month)})


def refresh_training_data(client, week: date, progress: Progress = lambda _: None) -> list[str]:
    """Pull everything the training-week view needs from Garmin into the local caches:
    thresholds, per-activity load stats (incrementally), the calendar months the week touches,
    and the planned workouts of that week. Returns warnings to show (nothing here is fatal —
    the view falls back to whatever is cached)."""
    warnings = []
    progress("Fetching thresholds (FTP, lactate threshold)...")
    thresholds = garmin_client.get_thresholds(client)
    if thresholds:
        storage.save_thresholds(thresholds)
    else:
        warnings.append("Couldn't fetch thresholds from Garmin — using cached/override values")

    today = date.today()
    fetched_until, stats = storage.load_activity_stats()
    start = (
        date.fromisoformat(fetched_until) - timedelta(days=STATS_RECHECK_DAYS)
        if fetched_until
        else today - timedelta(days=STATS_HISTORY_DAYS)
    )
    progress(f"Fetching activity load data since {start}...")
    stats.update(garmin_client.activity_stats(client, start.isoformat(), today.isoformat()))
    storage.save_activity_stats(today.isoformat(), stats)

    for year, month in _months_of_week(week):
        progress(f"Fetching calendar {year}-{month:02d}...")
        storage.save_calendar_month(year, month, garmin_client.get_calendar_month(client, year, month))

    planned = [i for i in _calendar_items(week) if i.get("itemType") == "workout" and i.get("workoutId")]
    for n, item in enumerate(planned, start=1):
        progress(f"Fetching planned workout {n}/{len(planned)}: {item.get('title')}")
        try:
            storage.save_cached_workout(item["workoutId"], garmin_client.get_workout(client, item["workoutId"]))
        except Exception as e:  # one unreadable workout shouldn't hide the rest of the week
            warnings.append(f"Couldn't fetch workout '{item.get('title')}': {e}")
    return warnings


def _calendar_items(week: date) -> list[dict]:
    """The cached calendar items of one week. Garmin's month response also covers the
    neighbouring months' days shown in its month grid (October's includes the end of
    September), so a week spanning two months finds the same item in both files: keep one."""
    end = week + timedelta(days=6)
    items, seen = [], set()
    for year, month in _months_of_week(week):
        for item in storage.load_calendar_month(year, month) or []:
            key = (item.get("itemType"), item.get("id"))
            if week.isoformat() <= item.get("date", "") <= end.isoformat() and key not in seen:
                seen.add(key)
                items.append(item)
    return items


def listed_activities(period_days: int = 0, today: date | None = None) -> tuple[list[ActivitySummary], int]:
    """(activities to list, how many are left out because their sport is hidden in
    settings). `period_days` > 0 keeps only the last that many days (today included)."""
    _, activities = storage.load_index()
    if period_days:
        since = ((today or date.today()) - timedelta(days=period_days - 1)).isoformat()
        activities = [a for a in activities if a.start_time_local[:10] >= since]
    prefs = settings.load()
    shown = [a for a in activities if not settings.is_hidden(a.sport, prefs)]
    return shown, len(activities) - len(shown)


def load_thresholds() -> tl.Thresholds:
    known = {f for f in tl.Thresholds.__dataclass_fields__}
    return tl.Thresholds(**{k: v for k, v in storage.load_thresholds().items() if k in known})


def activity_estimates(thresholds: tl.Thresholds) -> list[tuple[ActivitySummary, tl.TssEstimate]]:
    """TSS for every synced (deduplicated) activity."""
    _, activities = storage.load_index()
    _, stats = storage.load_activity_stats()
    return [
        (a, tl.activity_tss(a, stats.get(a.activity_id), storage.load_laps(a.activity_id) if a.sport == "multisport" else [], thresholds))
        for a in activities
    ]


@dataclass
class TrainingWeekView:
    week: tl.WeekSummary
    thresholds: tl.Thresholds
    pmc: list[tl.PmcPoint]  # history + projection over the planned rest of the week
    today_point: tl.PmcPoint | None
    week_end_point: tl.PmcPoint | None
    notes: list[str] = field(default_factory=list)
    calendar_cached: bool = True
    # The week's rows minus those of sports hidden in settings (their load still counts).
    visible_items: list[tl.WeekItem] = field(default_factory=list)
    load_metrics: metrics.WeekMetrics = field(default_factory=metrics.WeekMetrics)


def load_training_week(week: date, today: date | None = None, history_days: int = PMC_CHART_DAYS) -> TrainingWeekView:
    """Assemble the week view from local data only (synced activities + cached Garmin data).
    `history_days`: how far before the week (or today) the fitness series reaches."""
    today = today or date.today()
    thresholds = load_thresholds()
    estimates = activity_estimates(thresholds)
    calendar = _calendar_items(week)
    calendar_cached = all(storage.load_calendar_month(y, m) is not None for y, m in _months_of_week(week))

    planned = []
    for item in calendar:
        if item.get("itemType") != "workout":
            continue
        dto = storage.load_cached_workout(item["workoutId"]) if item.get("workoutId") else None
        estimate = tl.workout_tss(dto, thresholds) if dto else tl.PlannedEstimate(0.0, 0.0)
        planned.append((item, estimate))
    summary = tl.build_week(week, today, estimates, planned, storage.load_plan_links())

    notes = []
    known_ids = {a.activity_id for a, _ in estimates} | set(storage.load_merge_ledger())
    unsynced = [i for i in calendar if i.get("itemType") == "activity" and str(i.get("id")) not in known_ids]
    if unsynced:
        notes.append(f"{len(unsynced)} activity(ies) this week exist in Garmin but aren't synced yet — sync to include them")
    if any(p[0].get("workoutId") and storage.load_cached_workout(p[0]["workoutId"]) is None for p in planned):
        notes.append("Some planned workouts aren't downloaded yet — press r to refresh from Garmin")
    prefs = settings.load()
    visible = [i for i in summary.items if not settings.is_hidden(i.sport, prefs)]
    if len(visible) < len(summary.items):
        hidden = len(summary.items) - len(visible)
        notes.append(f"{hidden} row(s) of hidden sports ({', '.join(prefs['hidden_sports'])}) not shown — their TSS still counts")
    missing = [n for n, v in (("cycling FTP", thresholds.ftp_cycling_w), ("running LTHR", thresholds.lthr_running)) if not v]
    if missing:
        notes.append("Missing thresholds: " + ", ".join(missing) + " — press r, or set them in data/thresholds_override.json")

    actual: dict[date, float] = {}
    for activity, estimate in estimates:
        day = tl.activity_date(activity)
        actual[day] = actual.get(day, 0.0) + estimate.tss
    # Projection: the shown week's planned, not-yet-done workouts added on their days.
    projected = dict(actual)
    for item in summary.items:
        if item.status == "planned" and item.day >= today:
            projected[item.day] = projected.get(item.day, 0.0) + item.tss
    week_end = week + timedelta(days=6)
    history_start = min(actual) if actual else today
    end = max(week_end, today)
    actual_points = {p.day: p for p in tl.pmc(actual, history_start, end)}
    projected_points = {p.day: p for p in tl.pmc(projected, history_start, end)}
    # Up to and including today the chart shows what was done; after today, the plan.
    points = [actual_points[d] if d <= today else projected_points[d] for d in sorted(actual_points)]
    chart_start = min(week, today) - timedelta(days=history_days)
    week_points = [actual_points[d] if d <= today else projected_points[d] for d in sorted(actual_points) if week <= d <= week_end]
    return TrainingWeekView(
        week=summary,
        thresholds=thresholds,
        pmc=[p for p in points if p.day >= chart_start],
        today_point=actual_points.get(today),
        week_end_point=(actual_points if week_end <= today else projected_points).get(week_end),
        notes=notes,
        calendar_cached=calendar_cached,
        visible_items=visible,
        load_metrics=metrics.week_metrics(week_points),
    )


# --- Export ------------------------------------------------------------------------


ATHLETE_LOOKBACK_DAYS = 90


def refresh_athlete(client, start: date, end: date) -> None:
    """Fetch the athlete's profile and the daily history of their parameters (weight,
    resting HR, VO2max, thresholds) between two dates into the local cache, merged with
    what's cached already."""
    athlete = storage.load_athlete()
    athlete["profile"] = garmin_client.athlete_profile(client)
    history = athlete.setdefault("history", {})
    for name, values in garmin_client.athlete_history(client, start.isoformat(), end.isoformat()).items():
        history.setdefault(name, {}).update(values)
    storage.save_athlete(athlete)


def export_training(client, choice: int, progress: Progress = lambda _: None) -> tuple[Path, list[str]]:
    """Write a training_export.CHOICES period (0 = this week so far, else that many full
    weeks ending last Sunday) to one JSON file in exports/ and return (its path, warnings).
    Logged in, it first fetches from Garmin what the period is missing locally: thresholds
    and load stats, every calendar month the period touches, and planned workouts not
    downloaded yet."""
    import training_export  # imports services itself

    warnings: list[str] = []
    today = date.today()
    start, end_week, _ = training_export.period(choice, today)
    weeks = (end_week - start).days // 7 + 1  # weeks touched (1 for this week so far)
    if client is not None:
        warnings += refresh_training_data(client, end_week, progress)
        months = sorted({m for n in range(weeks) for m in _months_of_week(start + timedelta(days=7 * n))})
        for year, month in months:
            if (year, month) in _months_of_week(end_week):
                continue  # refresh_training_data just fetched it
            progress(f"Fetching calendar {year}-{month:02d}...")
            try:
                storage.save_calendar_month(year, month, garmin_client.get_calendar_month(client, year, month))
            except Exception as e:  # export what's cached; the file notes the gap
                warnings.append(f"Couldn't fetch calendar {year}-{month:02d}: {e}")
        missing = []
        for n in range(weeks):
            for item in _calendar_items(start + timedelta(days=7 * n)):
                workout_id = item.get("workoutId")
                if item.get("itemType") == "workout" and workout_id and workout_id not in missing \
                        and storage.load_cached_workout(workout_id) is None:
                    missing.append(workout_id)
        for k, workout_id in enumerate(missing, start=1):
            progress(f"Fetching planned workout {k}/{len(missing)}...")
            try:
                storage.save_cached_workout(workout_id, garmin_client.get_workout(client, workout_id))
            except Exception as e:
                warnings.append(f"Couldn't fetch planned workout {workout_id}: {e}")
        progress("Fetching athlete parameters (weight, resting HR, VO2max, thresholds)...")
        try:
            # From well before the period, so its first weeks get the last weigh-in and
            # threshold set before it.
            refresh_athlete(client, start - timedelta(days=ATHLETE_LOOKBACK_DAYS), today)
        except Exception as e:  # export with what's cached
            warnings.append(f"Couldn't fetch athlete parameters: {e}")
        _, activities = storage.load_index()
        in_period = [a for a in activities if start.isoformat() <= a.start_time_local[:10]]
        _, sets_failed = fetch_missing_exercise_sets(client, in_period, progress)
        if sets_failed:
            warnings.append(f"Strength sets of {sets_failed} session(s) couldn't be fetched")
    else:
        warnings.append("Not logged in to Garmin — exported from cached data only")

    progress("Writing export...")
    document = training_export.build(choice, today, notes=list(warnings))
    path = storage.save_export(training_export.file_name(choice, today), document)
    return path, warnings


# --- Planned-workout changes (move / cancel in Garmin, link to an activity locally) ----


class CalendarError(Exception):
    """Something the user should be told (wrong kind of row, nothing to act on) — not a bug."""


PLAN_STATUSES = ("planned", "missed")


def _require_plan(item: tl.WeekItem | None, verb: str) -> tl.WeekItem:
    if item is None or item.status not in PLAN_STATUSES or not item.schedule_id:
        raise CalendarError(f"Select a planned (or missed) workout to {verb}")
    return item


def move_targets(item: tl.WeekItem | None) -> list[date]:
    """The days a planned workout can move to: the other days of its own week."""
    item = _require_plan(item, "move")
    start = tl.week_start(item.day)
    return [start + timedelta(days=n) for n in range(7) if start + timedelta(days=n) != item.day]


def cancel_prompt(item: tl.WeekItem | None) -> str:
    item = _require_plan(item, "cancel")
    return (
        f"Remove '{item.title}' ({item.day:%a %d.%m}) from the Garmin Connect calendar?\n\n"
        "The watch drops it at its next sync. The workout itself stays in your Garmin workout "
        "library, so it can be scheduled again."
    )


def _refetch_calendar(client, day: date) -> None:
    for year, month in _months_of_week(tl.week_start(day)):
        storage.save_calendar_month(year, month, garmin_client.get_calendar_month(client, year, month))


def _drop_link(schedule_id: int) -> None:
    links = storage.load_plan_links()
    if links.pop(str(schedule_id), "absent") != "absent":
        storage.save_plan_links(links)


def move_planned(client, item: tl.WeekItem, day: date) -> str:
    """Reschedule a planned workout in Garmin Connect to `day` (within its week). The new
    calendar entry is created before the old one is removed, so a failure part-way leaves the
    workout on both days rather than on neither. Returns a status message."""
    if day not in move_targets(item):
        raise CalendarError("A workout can only move to another day of its own week")
    garmin_client.schedule_workout(client, item.workout_id, day.isoformat())
    try:
        garmin_client.unschedule_workout(client, item.schedule_id)
    except Exception as e:
        _refetch_calendar(client, item.day)
        return f"'{item.title}' added on {day:%a %d.%m}, but removing it from {item.day:%a} failed: {e}"
    _drop_link(item.schedule_id)  # the old calendar entry no longer exists
    _refetch_calendar(client, item.day)
    return f"Moved '{item.title}' to {day:%a %d.%m}"


def cancel_planned(client, item: tl.WeekItem) -> str:
    _require_plan(item, "cancel")
    garmin_client.unschedule_workout(client, item.schedule_id)
    _drop_link(item.schedule_id)
    _refetch_calendar(client, item.day)
    return f"Cancelled '{item.title}' ({item.day:%a %d.%m})"


def link_candidates(week: tl.WeekSummary, item: tl.WeekItem | None) -> list[tl.WeekItem]:
    """What the selected row can be paired with: a plan with the week's unplanned
    activities, an unplanned activity with the week's open (planned or missed) plans."""
    if item is not None and item.status in PLAN_STATUSES and item.schedule_id:
        candidates = [i for i in week.items if i.status == "unplanned"]
        what = "unplanned activities"
    elif item is not None and item.status == "unplanned":
        candidates = [i for i in week.items if i.status in PLAN_STATUSES and i.schedule_id]
        what = "open planned workouts"
    else:
        raise CalendarError("Select a planned workout or an unplanned activity to link")
    if not candidates:
        raise CalendarError(f"No {what} this week to link it with")
    return candidates


def move_prompt(item: tl.WeekItem) -> str:
    return f"Move '{item.title}' ({item.day:%a %d.%m}) in Garmin Connect to:"


def link_prompt(item: tl.WeekItem) -> str:
    what = "the activity that carried out this plan" if item.schedule_id else "the plan this activity carried out"
    return f"Link '{item.title}' ({item.day:%a %d.%m}) with {what}:"


def unlink_prompt(item: tl.WeekItem | None) -> str:
    if item is None or item.status != "done (planned)" or not item.schedule_id:
        raise CalendarError("Select a done (planned) row to unlink")
    return (
        f"Unlink '{item.title}' ({item.day:%a}) from the plan '{item.plan_title}' "
        f"({item.plan_day:%a})?\n\nThey'll show as separate rows, and this plan won't be "
        "matched to an activity automatically again (l links it by hand)."
    )


def link(a: tl.WeekItem, b: tl.WeekItem) -> str:
    """Pair a planned workout with a completed activity (either order), e.g. Saturday's
    planned run that was done on Sunday. Local only — Garmin has no such link."""
    plan, activity = (a, b) if a.schedule_id else (b, a)
    links = storage.load_plan_links()
    links[str(plan.schedule_id)] = activity.activity_id
    storage.save_plan_links(links)
    return f"Linked '{plan.title}' ({plan.day:%a}) with '{activity.title}' ({activity.day:%a})"


def unlink(item: tl.WeekItem | None) -> str:
    """Split a done (planned) row back into the plan and the activity; the plan then stays
    unpaired (no automatic same-day match) until linked again."""
    unlink_prompt(item)
    links = storage.load_plan_links()
    links[str(item.schedule_id)] = None
    storage.save_plan_links(links)
    return f"Unlinked '{item.plan_title}' from '{item.title}'"


# --- Activity sync / reprocess -------------------------------------------------


def assign_route(activity_id: str, fit_bytes: bytes, sport: str, routes_list: list[Route]) -> str | None:
    """Compute an activity's route_id, matching against `routes_list` (mutated in place —
    a newly created route is appended and persisted immediately, so a later activity in the
    same sync/reprocess run can match it). Returns None if there isn't enough GPS data."""
    records = fit_parser.load_records(activity_id, fit_bytes)
    signature = routes.build_signature(records)
    if signature is None:
        return None
    route, is_new = routes.match_or_create(signature, sport, routes_list, lambda: uuid.uuid4().hex)
    if is_new:
        lat, lon = signature.points[0]
        route.place = geocode.reverse_geocode(lat, lon)
        routes_list.append(route)
        storage.save_routes(routes_list)
    return route.route_id


def merge_duplicates(
    activities: list[ActivitySummary], recompute_existing: bool
) -> tuple[list[ActivitySummary], int]:
    """Detect activities that are the same physical session recorded by two devices and
    fold each into its primary (see dedupe.py) — filling in HR/power/GPS coverage the
    primary's own device missed, and dropping the secondary from the visible list.

    Fully idempotent and safe to run on every sync/reprocess: a secondary's raw .fit is
    never deleted, only excluded from the index via the merge ledger, so re-running this
    just recomputes the same result from scratch rather than accumulating state.

    Rebuilding a merge means fully re-parsing both FIT files (tens of seconds for a few long
    activities), so with `recompute_existing=False` (sync) only pairs that are new or have no
    saved merged stream are rebuilt — `activities` loaded from the index already carry the
    merged summaries of the rest. Reprocess passes True, since it has just re-parsed every
    summary from scratch and the merge has to be reapplied on top.
    """
    ledger = storage.load_merge_ledger()
    visible = [a for a in activities if a.activity_id not in ledger]

    newly_merged = 0
    new_primaries: set[str] = set()
    for a, b in dedupe.find_duplicate_pairs(visible):
        primary, secondary = dedupe.choose_primary(a, b)
        if secondary.activity_id in ledger:
            continue
        ledger[secondary.activity_id] = primary.activity_id
        new_primaries.add(primary.activity_id)
        newly_merged += 1
    if newly_merged:
        storage.save_merge_ledger(ledger)

    by_id = {a.activity_id: a for a in activities}
    secondaries_by_primary: dict[str, list[str]] = {}
    for secondary_id, primary_id in ledger.items():
        secondaries_by_primary.setdefault(primary_id, []).append(secondary_id)

    for primary_id, secondary_ids in secondaries_by_primary.items():
        primary = by_id.get(primary_id)
        if primary is None:
            continue
        if not (recompute_existing or primary_id in new_primaries or not storage.has_merged_records(primary_id)):
            continue

        streams = []
        # Index of the virtual-ride stream, whose distance/position/elevation win (see
        # dedupe.merge_records); None when no recording of the pair is virtual.
        geo_source = None
        if storage.has_fit(primary_id):
            streams.append(fit_parser.parse_records(storage.fit_path(primary_id).read_bytes()))
            if primary.virtual:
                geo_source = 0

        secondary_summaries = []
        for secondary_id in secondary_ids:
            if not storage.has_fit(secondary_id):
                continue
            secondary_fit_bytes = storage.fit_path(secondary_id).read_bytes()
            streams.append(fit_parser.parse_records(secondary_fit_bytes))
            # name/start_time_local only affect display fields we discard here — the
            # summary-level HR/power/ascent/speed fields come straight from the FIT
            # session message regardless.
            secondary_summary, _ = fit_parser.parse(
                secondary_fit_bytes, secondary_id, "", primary.start_time_local
            )
            secondary_summaries.append(secondary_summary)
            if secondary_summary.virtual and geo_source is None:
                geo_source = len(streams) - 1

        if len(streams) >= 2:
            merged_records = dedupe.merge_records(streams, geo_source)
            storage.save_merged_records(primary_id, merged_records)
            if geo_source not in (None, 0):
                # The primary's device laps carry its own (trainer-estimated) distance.
                laps = storage.load_laps(primary_id)
                offset = fit_parser.load_utc_offset(primary_id, primary.start_time_local)
                fit_parser.restate_lap_geo(laps, merged_records, offset)
                storage.save_laps(primary_id, laps)
        if secondary_summaries:
            by_id[primary_id] = dedupe.merge_summary(primary, secondary_summaries)

    merged_activities = [a for a in by_id.values() if a.activity_id not in ledger]
    return merged_activities, newly_merged


# --- Strength sets -------------------------------------------------------------------


def is_strength(activity: ActivitySummary) -> bool:
    return tl.sport_family(activity.sport) == "strength"


def exercise_sets(client, activity_id: str) -> list[dict] | None:
    """A strength activity's sets: cached, else fetched from Garmin (when logged in) and
    cached. None when not cached and not logged in."""
    sets = storage.load_exercise_sets(activity_id)
    if sets is None and client is not None:
        sets = garmin_client.exercise_sets(client, activity_id)
        storage.save_exercise_sets(activity_id, sets)
    return sets


def fetch_missing_exercise_sets(client, activities: list[ActivitySummary], progress: Progress) -> tuple[int, int]:
    """Fetch the sets of every strength activity that has none cached (new ones after a
    sync, and the backlog the first time); returns (fetched, failed)."""
    missing = [a for a in activities if is_strength(a) and storage.load_exercise_sets(a.activity_id) is None]
    fetched = failed = 0
    for n, activity in enumerate(missing, start=1):
        progress(f"Fetching strength sets {n}/{len(missing)}: {activity.name}...")
        try:
            exercise_sets(client, activity.activity_id)
            fetched += 1
        except garmin_client.GarminConnectTooManyRequestsError:
            failed += len(missing) - n + 1  # the next sync carries on from here
            break
        except Exception:  # one unreadable activity shouldn't stop the rest
            failed += 1
    return fetched, failed


def sync_activities(client, progress: Progress = lambda _: None, full_history: bool = False) -> str:
    """Download every new Garmin activity since the last sync — or, with `full_history`,
    every activity in the account not downloaded yet — (FIT file, laps, route), merge
    duplicate recordings, and return a summary message."""
    last_synced_date, activities = storage.load_index()
    routes_list = storage.load_routes()
    # Also treat any activity already folded into another as "known" — otherwise a
    # future sync would re-download and re-add it, undoing a prior merge.
    known_ids = {a.activity_id for a in activities} | set(storage.load_merge_ledger())
    start_date = (
        FULL_HISTORY_START if full_history
        else last_synced_date or (date.today() - timedelta(days=INITIAL_SYNC_DAYS)).isoformat()
    )
    end_date = date.today().isoformat()

    progress(f"Fetching activity list since {start_date}...")
    try:
        raw_activities = garmin_client.list_activities(client, start_date, end_date)
    except garmin_client.GarminConnectTooManyRequestsError:
        return "Rate limited by Garmin — try syncing again later."
    except (garmin_client.GarminConnectAuthenticationError, garmin_client.GarminConnectConnectionError) as e:
        return f"Sync failed: {e}"

    new_count = 0
    skipped = 0
    for raw in raw_activities:
        activity_id = str(raw.get("activityId"))
        if activity_id in known_ids:
            continue

        name = raw.get("activityName") or "Untitled"
        start_time_local = raw.get("startTimeLocal") or ""
        progress(f"Downloading {name} ({new_count + skipped + 1}/{len(raw_activities)})...")

        try:
            fit_bytes = garmin_client.download_fit(client, activity_id)
            summary, laps = fit_parser.parse(fit_bytes, activity_id, name, start_time_local)
        except garmin_client.GarminConnectTooManyRequestsError:
            return f"Rate limited after {new_count} new activities — try again later."
        except (
            garmin_client.GarminConnectNotFoundError,
            garmin_client.GarminConnectInvalidFileFormatError,
            ValueError,
        ):
            skipped += 1
            known_ids.add(activity_id)  # avoid retrying a permanently-missing FIT within this run
            continue

        summary.route_id = assign_route(activity_id, fit_bytes, summary.sport, routes_list)
        storage.save_fit(activity_id, fit_bytes)
        storage.save_laps(activity_id, laps)
        activities.append(summary)
        known_ids.add(activity_id)
        new_count += 1

        activity_date = summary.start_time_local[:10]
        if not last_synced_date or activity_date > last_synced_date:
            last_synced_date = activity_date
        storage.save_index(last_synced_date, activities)

    progress("Checking for duplicate recordings...")
    activities, merged_count = merge_duplicates(activities, recompute_existing=False)
    storage.save_index(last_synced_date, activities)
    if full_history and new_count:
        _fetch_history_stats(client, activities, progress)
    sets_fetched, sets_failed = fetch_missing_exercise_sets(client, activities, progress)

    message = f"Sync complete: {new_count} new activities"
    if merged_count:
        message += f", {merged_count} merged as duplicates"
    if skipped:
        message += f", {skipped} skipped (no usable FIT file)"
    if sets_fetched:
        message += f", strength sets of {sets_fetched} session(s) imported"
    if sets_failed:
        message += f" ({sets_failed} not fetched — sync again later)"
    return message


def _fetch_history_stats(client, activities: list[ActivitySummary], progress: Progress) -> None:
    """Garmin's load fields (normalized power, its own TSS) for activities older than the
    window refresh_training_data keeps up to date — one paginated list call."""
    fetched_until, stats = storage.load_activity_stats()
    oldest = min(a.start_time_local[:10] for a in activities)
    progress(f"Fetching activity load data since {oldest}...")
    try:
        stats.update(garmin_client.activity_stats(client, oldest, date.today().isoformat()))
    except Exception:  # optional: TSS falls back to HR/per-hour estimates without it
        return
    storage.save_activity_stats(fetched_until or date.today().isoformat(), stats)


def reprocess_activities(progress: Progress = lambda _: None) -> str:
    """Re-parse every downloaded FIT file with the current parser (no network), keeping
    interval marks made by hand, and return a summary message."""
    last_synced_date, activities = storage.load_index()
    routes_list = storage.load_routes()
    by_id = {a.activity_id: a for a in activities}

    reprocessed = 0
    skipped = 0
    for i, activity in enumerate(activities, start=1):
        progress(f"Reprocessing {activity.name} ({i}/{len(activities)})...")
        if not storage.has_fit(activity.activity_id):
            skipped += 1
            continue

        fit_bytes = storage.fit_path(activity.activity_id).read_bytes()
        try:
            new_summary, new_laps = fit_parser.parse(
                fit_bytes, activity.activity_id, activity.name, activity.start_time_local
            )
        except ValueError:
            skipped += 1
            continue

        old_laps = storage.load_laps(activity.activity_id)
        old_marks = {lap.lap_index: lap.is_interval for lap in old_laps}
        if len(old_laps) > 1 and all(old_marks.values()):
            # Every lap marked is the old parser stamping all 'active' laps of an
            # unstructured activity as intervals, not marks anyone made by hand.
            old_marks = {}
        for lap in new_laps:
            if old_marks.get(lap.lap_index):
                lap.is_interval = True

        new_summary.route_id = assign_route(
            activity.activity_id, fit_bytes, new_summary.sport, routes_list
        )
        by_id[activity.activity_id] = new_summary
        storage.save_laps(activity.activity_id, new_laps)
        storage.save_index(last_synced_date, list(by_id.values()))
        reprocessed += 1

    progress("Re-merging duplicate recordings...")
    merged_activities, merged_count = merge_duplicates(list(by_id.values()), recompute_existing=True)
    storage.save_index(last_synced_date, merged_activities)

    message = f"Reprocessed {reprocessed} activities"
    if merged_count:
        message += f", {merged_count} merged as duplicates"
    if skipped:
        message += f", {skipped} skipped (no local FIT file)"
    return message


# --- Workout upload -------------------------------------------------------------


def _is_scheduled(client, workout_id: int, day: str, months: dict) -> bool:
    """Whether Garmin's calendar has `workout_id` on `day` (YYYY-MM-DD); `months` caches the
    month responses fetched during one upload run."""
    year, month = int(day[:4]), int(day[5:7])
    if (year, month) not in months:
        months[(year, month)] = garmin_client.get_calendar_month(client, year, month)
    return any(
        i.get("itemType") == "workout" and i.get("workoutId") == workout_id and i.get("date") == day
        for i in months[(year, month)]
    )


def upload_and_record(client, key: str, entry: dict, months: dict | None = None) -> tuple[str, str]:
    """Put one workout file into Garmin: (outcome, warning), outcome "uploaded",
    "rescheduled" or "unchanged".

    A file the ledger says was uploaded is checked against Garmin first: while that workout
    is still in the Garmin library it's reused — put back on the calendar when its date has
    no entry for it (e.g. the calendar entry was removed), left alone when it does — and
    only a workout deleted from Garmin is uploaded again. So no step here ever leaves two
    copies of a workout in Garmin.

    A new upload is recorded in the ledger before scheduling on purpose: an unrecorded
    upload would get uploaded again on retry, leaving a duplicate workout in Garmin."""
    months = {} if months is None else months
    day = entry.get("schedule_date")
    workout_id = storage.load_uploaded_workouts().get(key)
    if workout_id is not None and garmin_client.workout_exists(client, workout_id):
        if not day or _is_scheduled(client, workout_id, day, months):
            return "unchanged", ""
        garmin_client.schedule_workout(client, workout_id, day)
        return "rescheduled", ""

    workout_id = garmin_client.upload_workout(client, workouts.to_garmin(entry))
    uploaded = storage.load_uploaded_workouts()
    uploaded[key] = workout_id
    storage.save_uploaded_workouts(uploaded)
    if day:
        try:
            garmin_client.schedule_workout(client, workout_id, day)
        except Exception as e:  # the workout exists; only the calendar entry is missing
            return "uploaded", f"{entry['name']} uploaded but not scheduled: {e}"
    return "uploaded", ""
