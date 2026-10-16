"""Training stress (TSS) estimation for completed activities and planned workouts, and the
ATL/CTL/TSB fitness model built on it. Pure logic — no network, no UI.

TSS = hours x IF^2 x 100, where IF (intensity factor) is effort relative to threshold:
normalized power / FTP when power exists, otherwise average heart rate / lactate-threshold HR,
otherwise a per-sport rate per hour. When Garmin itself computed a TSS (rides with a power
meter and FTP set) that value is used as-is. Checked against Garmin on a real indoor ride:
NP 195 W, FTP 270 W, 58.5 min -> 50.9 here vs Garmin's 50.4.

CTL ("fitness") and ATL ("fatigue") are the usual exponentially weighted averages of daily
TSS over 42 and 7 days; TSB ("form") = CTL - ATL, taken from the previous day's values as in
TrainingPeaks' Performance Management Chart.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from models import ActivitySummary, Lap

CTL_DAYS = 42
ATL_DAYS = 7

# Map both FIT session sports (local activities) and Garmin keys (calendar/workouts) to one
# family, so a planned run can be matched to a completed trail run, etc.
SPORT_FAMILIES = {
    "running": "running", "trail_running": "running", "treadmill_running": "running",
    "track_running": "running", "cycling": "cycling", "indoor_cycling": "cycling",
    "road_biking": "cycling", "virtual_ride": "cycling", "gravel_cycling": "cycling",
    "mountain_biking": "cycling", "swimming": "swimming", "lap_swimming": "swimming",
    "open_water_swimming": "swimming", "strength_training": "strength", "training": "strength",
    "fitness_equipment": "strength", "hiking": "hiking", "walking": "hiking",
    "multisport": "multisport", "multi_sport": "multisport", "transition": "transition",
}

# TSS per hour when nothing better is known (no power, no HR).
FALLBACK_TSS_PER_HOUR = {
    "running": 60.0, "cycling": 50.0, "swimming": 50.0, "strength": 35.0, "hiking": 40.0,
    "transition": 40.0,
}
DEFAULT_TSS_PER_HOUR = 40.0

# Intensity factor of a step by its Garmin heart-rate / power zone number (5 HR zones, 7 power
# zones) — midpoints of the usual zone definitions relative to threshold.
HR_ZONE_IF = {1: 0.60, 2: 0.72, 3: 0.82, 4: 0.92, 5: 1.02}
POWER_ZONE_IF = {1: 0.50, 2: 0.65, 3: 0.82, 4: 0.97, 5: 1.12, 6: 1.30, 7: 1.50}
# ... and by step type when a step has no target at all.
STEP_TYPE_IF = {
    "warmup": 0.65, "cooldown": 0.60, "interval": 0.85, "recovery": 0.55, "rest": 0.40,
    "other": 0.70, "main": 0.75,
}
STRENGTH_IF = 0.60
# Assumed length of a step that ends on the lap button, when the workout carries no
# estimated duration to size it from. Deliberately modest; such estimates are flagged.
LAP_BUTTON_DEFAULT_S = {
    "warmup": 600.0, "cooldown": 600.0, "interval": 900.0, "recovery": 180.0, "rest": 120.0,
}
LAP_BUTTON_FALLBACK_S = 600.0
# Lap-button work intervals get shorter the harder they are (VO2 efforts last minutes, tempo
# blocks tens of minutes): (minimum intensity factor, assumed seconds), first match wins.
LAP_BUTTON_INTERVAL_S = ((1.05, 240.0), (0.95, 480.0), (0.85, 600.0), (0.0, 900.0))
# Speed at threshold for sports where Garmin gives none (cycling distance steps, swimming).
DEFAULT_THRESHOLD_SPEED_MPS = {"cycling": 10.0, "swimming": 1.1, "running": 3.5, "hiking": 1.5}


def sport_family(sport: str | None) -> str:
    return SPORT_FAMILIES.get(sport or "", sport or "other")


@dataclass
class Thresholds:
    ftp_cycling_w: float | None = None
    ftp_running_w: float | None = None
    lthr_running: float | None = None
    lthr_cycling: float | None = None
    lthr_default: float | None = None
    threshold_speed_running_mps: float | None = None

    def ftp(self, family: str) -> float | None:
        return {"cycling": self.ftp_cycling_w, "running": self.ftp_running_w}.get(family)

    def lthr(self, family: str) -> float | None:
        specific = {"cycling": self.lthr_cycling, "running": self.lthr_running}.get(family)
        return specific or self.lthr_default

    def threshold_speed(self, family: str) -> float:
        if family == "running" and self.threshold_speed_running_mps:
            return self.threshold_speed_running_mps
        return DEFAULT_THRESHOLD_SPEED_MPS.get(family, 2.0)


def _tss(seconds: float, intensity: float) -> float:
    return seconds / 3600.0 * intensity * intensity * 100.0


@dataclass
class TssEstimate:
    tss: float
    method: str  # e.g. "garmin", "power", "hr", "per-hour"


def _single_sport_tss(
    family: str, seconds: float, avg_hr: float | None, power: float | None, th: Thresholds,
    power_label: str = "power",
) -> TssEstimate:
    ftp = th.ftp(family)
    if power and ftp:
        return TssEstimate(_tss(seconds, power / ftp), power_label)
    lthr = th.lthr(family)
    if avg_hr and lthr:
        return TssEstimate(_tss(seconds, avg_hr / lthr), "hr")
    rate = FALLBACK_TSS_PER_HOUR.get(family, DEFAULT_TSS_PER_HOUR)
    return TssEstimate(seconds / 3600.0 * rate, "per-hour")


def activity_tss(
    activity: ActivitySummary, stats: dict | None, laps: list[Lap], th: Thresholds
) -> TssEstimate:
    """TSS of a completed activity. `stats` is Garmin's activity-list entry for it (see
    garmin_client.activity_stats), which carries normalized power and Garmin's own TSS that
    the local FIT summary doesn't; `laps` are only used for multisport activities, which are
    scored leg by leg with each leg's own sport thresholds."""
    family = sport_family(activity.sport)
    if family == "multisport" and laps:
        total, methods = 0.0, set()
        for lap in laps:
            est = _single_sport_tss(sport_family(lap.sport), lap.duration_s, lap.avg_hr, lap.avg_power, th)
            total += est.tss
            methods.add(est.method)
        return TssEstimate(total, "legs:" + "+".join(sorted(methods)))

    stats = stats or {}
    if stats.get("trainingStressScore"):
        return TssEstimate(float(stats["trainingStressScore"]), "garmin")
    seconds = activity.duration_s
    norm_power = stats.get("normPower")
    if norm_power:
        return _single_sport_tss(family, seconds, activity.avg_hr, norm_power, th, power_label="NP")
    return _single_sport_tss(family, seconds, activity.avg_hr, activity.avg_power, th)


@dataclass
class PlannedEstimate:
    tss: float
    duration_s: float
    # Seconds of the estimate that come from lap-button steps of assumed length (the less
    # reliable part); 0 when every step had a time, distance or known overall duration.
    assumed_s: float = 0.0


def _step_intensity(step: dict, family: str, th: Thresholds) -> tuple[float, float | None]:
    """(intensity factor, expected speed m/s or None) for one executable Garmin step."""
    target = (step.get("targetType") or {}).get("workoutTargetTypeKey") or "no.target"
    low, high = step.get("targetValueOne"), step.get("targetValueTwo")
    mid = (low + high) / 2 if low is not None and high is not None else None
    zone = step.get("zoneNumber")
    threshold_speed = th.threshold_speed(family)

    intensity = None
    speed = None
    if target in ("pace.zone", "speed.zone") and mid:
        speed = mid
        intensity = mid / threshold_speed
    elif target.startswith("power") and (mid or zone):
        ftp = th.ftp(family)
        if mid and ftp:
            intensity = mid / ftp
        elif zone:
            intensity = POWER_ZONE_IF.get(zone)
    elif target.startswith("heart.rate") and (mid or zone):
        lthr = th.lthr(family)
        if mid and lthr:
            intensity = mid / lthr
        elif zone:
            intensity = HR_ZONE_IF.get(zone)
    if intensity is None:
        step_type = (step.get("stepType") or {}).get("stepTypeKey") or "other"
        intensity = STEP_TYPE_IF.get(step_type, 0.7)
    if speed is None:
        # Speed scales roughly with intensity around threshold for running/swimming; for
        # cycling it's a coarse guess, only used to turn distance steps into time.
        speed = threshold_speed * max(intensity, 0.4)
    return intensity, speed


def _flatten_steps(steps: list[dict], repeat: int = 1):
    for step in steps:
        if step.get("type") == "RepeatGroupDTO" or step.get("workoutSteps"):
            iterations = int(step.get("numberOfIterations") or step.get("endConditionValue") or 1)
            yield from _flatten_steps(step.get("workoutSteps") or [], repeat * iterations)
        else:
            yield step, repeat


def _assumed_lap_button_s(step_type: str, intensity: float) -> float:
    if step_type == "interval":
        return next(seconds for min_if, seconds in LAP_BUTTON_INTERVAL_S if intensity >= min_if)
    return LAP_BUTTON_DEFAULT_S.get(step_type, LAP_BUTTON_FALLBACK_S)


def workout_tss(dto: dict, th: Thresholds) -> PlannedEstimate:
    """Estimated TSS and duration of a planned Garmin workout (DTO from the workout service,
    or workouts.to_garmin output)."""
    family = sport_family((dto.get("sportType") or {}).get("sportTypeKey"))
    known: list[tuple[float, float]] = []  # (seconds, intensity) of steps with a length
    open_steps: list[tuple[str, float, int]] = []  # (step type, intensity, repeats) ending on lap button
    for segment in dto.get("workoutSegments") or []:
        for step, repeat in _flatten_steps(segment.get("workoutSteps") or []):
            intensity, speed = _step_intensity(step, family, th)
            if family == "strength":
                intensity = STRENGTH_IF
            condition = (step.get("endCondition") or {}).get("conditionTypeKey")
            value = step.get("endConditionValue") or 0.0
            if condition == "time" and value:
                known.append((value * repeat, intensity))
            elif condition == "distance" and value:
                known.append((value / speed * repeat, intensity))
            else:
                step_type = (step.get("stepType") or {}).get("stepTypeKey") or "other"
                open_steps.append((step_type, intensity, repeat))

    known_s = sum(s for s, _ in known)
    tss = sum(_tss(s, i) for s, i in known)
    assumed_s = 0.0
    if open_steps:
        open_repeats = sum(r for _, _, r in open_steps)
        garmin_estimate = float(dto.get("estimatedDurationInSecs") or 0.0)
        spare = garmin_estimate - known_s
        for step_type, intensity, repeat in open_steps:
            if spare > 0:
                seconds = spare / open_repeats * repeat
            else:
                seconds = _assumed_lap_button_s(step_type, intensity) * repeat
                assumed_s += seconds
            tss += _tss(seconds, intensity)
            known_s += seconds
    elif not known and dto.get("estimatedDurationInSecs"):
        seconds = float(dto["estimatedDurationInSecs"])
        intensity = STRENGTH_IF if family == "strength" else 0.7
        return PlannedEstimate(_tss(seconds, intensity), seconds)
    return PlannedEstimate(tss, known_s, assumed_s)


@dataclass
class PmcPoint:
    day: date
    tss: float
    ctl: float
    atl: float
    tsb: float


def pmc(daily_tss: dict[date, float], start: date, end: date) -> list[PmcPoint]:
    """CTL/ATL/TSB for every day in [start, end]. Starts from zero at `start`, so start well
    (several weeks) before the period you care about for the averages to have settled."""
    points = []
    ctl = atl = 0.0
    day = start
    while day <= end:
        tsb = ctl - atl  # form going into the day: yesterday's fitness minus fatigue
        tss = daily_tss.get(day, 0.0)
        ctl += (tss - ctl) / CTL_DAYS
        atl += (tss - atl) / ATL_DAYS
        points.append(PmcPoint(day, tss, ctl, atl, tsb))
        day += timedelta(days=1)
    return points


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def activity_date(activity: ActivitySummary) -> date:
    return datetime.fromisoformat(activity.start_time_local.replace(" ", "T")).date()


@dataclass
class WeekItem:
    """One row of the week view: a completed activity, a planned workout, or both when the
    plan was carried out (matched by date and sport family)."""
    day: date
    sport: str
    title: str
    status: str  # "done (planned)", "planned", "missed", "unplanned"
    tss: float
    method: str | None  # how a completed activity's TSS was computed (see TssEstimate.method)
    duration_s: float | None = None
    distance_m: float | None = None
    activity_id: str | None = None
    workout_id: int | None = None
    planned_tss: float | None = None  # the plan's estimate, when a planned workout is involved
    plan_assumed: bool = False  # the plan estimate guessed some lap-button step lengths


@dataclass
class WeekSummary:
    start: date
    items: list[WeekItem] = field(default_factory=list)

    @property
    def done_tss(self) -> float:
        return sum(i.tss for i in self.items if i.status.startswith("done") or i.status == "unplanned")

    @property
    def remaining_tss(self) -> float:
        return sum(i.tss for i in self.items if i.status == "planned")

    @property
    def missed_tss(self) -> float:
        return sum(i.tss for i in self.items if i.status == "missed")


def build_week(
    start: date,
    today: date,
    activities: list[tuple[ActivitySummary, TssEstimate]],
    planned: list[tuple[dict, PlannedEstimate]],
) -> WeekSummary:
    """Combine a week's completed activities and planned workouts (calendar items with
    their estimate) into rows. A planned workout counts as done when an activity of the same
    sport family happened on its day; each activity satisfies at most one plan."""
    end = start + timedelta(days=6)
    week = WeekSummary(start)
    unmatched = [(a, est) for a, est in activities if start <= activity_date(a) <= end]
    for item, estimate in sorted(planned, key=lambda p: p[0]["date"]):
        day = date.fromisoformat(item["date"])
        if not start <= day <= end:
            continue
        family = sport_family(item.get("sportTypeKey"))
        match = next(
            ((a, e) for a, e in unmatched if activity_date(a) == day and sport_family(a.sport) == family),
            None,
        )
        assumed = bool(estimate.assumed_s)
        if match:
            unmatched.remove(match)
            activity, est = match
            week.items.append(WeekItem(
                day, activity.sport, activity.name, "done (planned)", est.tss, est.method,
                activity.duration_s, activity.distance_m, activity.activity_id, item.get("workoutId"),
                estimate.tss, assumed,
            ))
        else:
            status = "missed" if day < today else "planned"
            week.items.append(WeekItem(
                day, item.get("sportTypeKey") or "", item.get("title") or "", status, estimate.tss,
                None, estimate.duration_s, None, None, item.get("workoutId"), estimate.tss, assumed,
            ))
    for activity, est in unmatched:
        week.items.append(WeekItem(
            activity_date(activity), activity.sport, activity.name, "unplanned", est.tss, est.method,
            activity.duration_s, activity.distance_m, activity.activity_id,
        ))
    week.items.sort(key=lambda i: (i.day, i.status))
    return week
