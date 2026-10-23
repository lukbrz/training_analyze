"""Application operations shared by every front end (Textual TUI and both GUIs): they talk
to Garmin, storage and the parsers, and report progress through a plain callback, so no UI
code lives here and no front end re-implements them."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import uuid

import dedupe
import fit_parser
import garmin_client
import geocode
import routes
import storage
import training_load as tl
import workouts
from models import ActivitySummary, Route

Progress = Callable[[str], None]

INITIAL_SYNC_DAYS = 365

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
    end = week + timedelta(days=6)
    items = []
    for year, month in _months_of_week(week):
        for item in storage.load_calendar_month(year, month) or []:
            if week.isoformat() <= item.get("date", "") <= end.isoformat():
                items.append(item)
    return items


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


def load_training_week(week: date, today: date | None = None) -> TrainingWeekView:
    """Assemble the week view from local data only (synced activities + cached Garmin data)."""
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
    summary = tl.build_week(week, today, estimates, planned)

    notes = []
    known_ids = {a.activity_id for a, _ in estimates} | set(storage.load_merge_ledger())
    unsynced = [i for i in calendar if i.get("itemType") == "activity" and str(i.get("id")) not in known_ids]
    if unsynced:
        notes.append(f"{len(unsynced)} activity(ies) this week exist in Garmin but aren't synced yet — sync to include them")
    if any(p[0].get("workoutId") and storage.load_cached_workout(p[0]["workoutId"]) is None for p in planned):
        notes.append("Some planned workouts aren't downloaded yet — press r to refresh from Garmin")
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
    chart_start = min(week, today) - timedelta(days=PMC_CHART_DAYS)
    return TrainingWeekView(
        week=summary,
        thresholds=thresholds,
        pmc=[p for p in points if p.day >= chart_start],
        today_point=actual_points.get(today),
        week_end_point=(actual_points if week_end <= today else projected_points).get(week_end),
        notes=notes,
        calendar_cached=calendar_cached,
    )


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


def sync_activities(client, progress: Progress = lambda _: None) -> str:
    """Download every new Garmin activity since the last sync (FIT file, laps, route), merge
    duplicate recordings, and return a summary message."""
    last_synced_date, activities = storage.load_index()
    routes_list = storage.load_routes()
    # Also treat any activity already folded into another as "known" — otherwise a
    # future sync would re-download and re-add it, undoing a prior merge.
    known_ids = {a.activity_id for a in activities} | set(storage.load_merge_ledger())
    start_date = last_synced_date or (date.today() - timedelta(days=INITIAL_SYNC_DAYS)).isoformat()
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

    message = f"Sync complete: {new_count} new activities"
    if merged_count:
        message += f", {merged_count} merged as duplicates"
    if skipped:
        message += f", {skipped} skipped (no usable FIT file)"
    return message


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


def upload_and_record(client, key: str, entry: dict) -> str:
    """Create one workout in Garmin, record it in the ledger, then schedule it. Returns a
    warning when the workout was created but scheduling failed. The ledger is written
    before scheduling on purpose: an unrecorded upload would get uploaded again on retry,
    leaving a duplicate workout in Garmin."""
    workout_id = garmin_client.upload_workout(client, workouts.to_garmin(entry))
    uploaded = storage.load_uploaded_workouts()
    uploaded[key] = workout_id
    storage.save_uploaded_workouts(uploaded)
    if entry.get("schedule_date"):
        try:
            garmin_client.schedule_workout(client, workout_id, entry["schedule_date"])
        except Exception as e:  # the workout exists; only the calendar entry is missing
            return f"{entry['name']} uploaded but not scheduled: {e}"
    return ""
