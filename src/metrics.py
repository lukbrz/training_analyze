"""Advanced training metrics on top of training_load's TSS / CTL / ATL — pure logic, no UI.

Per activity (from its record stream):
- Efficiency factor (EF): output per heartbeat — normalized (else average) power / avg HR
  for rides, speed in metres per minute / avg HR for runs and hikes. Rising EF at the same
  effort over weeks means aerobic fitness is improving.
- Aerobic decoupling (Pw:HR or Pa:HR): how much output per heartbeat drops from the first
  half of the activity to the second. Under ~5% on a long steady session means good aerobic
  endurance; it's meaningless on interval sessions, so it's only computed for rides and runs
  of at least MIN_DECOUPLING_S (hikes stop and start too much for it to mean anything).

Per week (from the daily TSS / CTL / ATL series):
- Ramp rate: how much CTL rose over the week. Above ~5-8 per week is aggressive.
- ACWR (acute:chronic workload ratio, here ATL / CTL at week end): ~0.8-1.3 is the usual
  "sweet spot"; above ~1.5 injury risk rises sharply.
- Monotony (Foster): mean daily TSS / its standard deviation over the week — above ~2 means
  little variation between hard and easy days.
- Strain (Foster): weekly TSS x monotony.
"""

import statistics
from dataclasses import dataclass

import fit_parser
import training_load as tl
from models import ActivitySummary, RecordPoint

MIN_DECOUPLING_S = 20 * 60
MIN_MOVING_SPEED_MPS = 0.5  # running/hiking samples slower than this are stops, not effort


@dataclass
class ActivityMetrics:
    efficiency_factor: float | None = None
    ef_basis: str = ""  # "W/bpm" or "m/min per bpm"
    decoupling_pct: float | None = None  # positive = output per heartbeat dropped in the second half


@dataclass
class WeekMetrics:
    ramp_rate: float | None = None  # CTL change over the week
    acwr: float | None = None
    monotony: float | None = None
    strain: float | None = None


def _speed_series(records: list[RecordPoint]) -> list[float | None]:
    speeds: list[float | None] = []
    for prev, rec in zip([None] + records[:-1], records):
        if rec.speed_mps is not None:
            speeds.append(rec.speed_mps)
        elif prev is not None and prev.distance_m is not None and rec.distance_m is not None:
            seconds = (rec.timestamp_utc - prev.timestamp_utc).total_seconds()
            speeds.append((rec.distance_m - prev.distance_m) / seconds if seconds > 0 else None)
        else:
            speeds.append(None)
    return speeds


def _ratio(samples: list[tuple[float, float]]) -> float | None:
    """Mean output / mean heart rate over (output, hr) samples."""
    if not samples:
        return None
    hr = statistics.fmean(h for _, h in samples)
    return statistics.fmean(o for o, _ in samples) / hr if hr else None


def moving(records: list[RecordPoint]) -> list[RecordPoint]:
    """The records outside pauses (fit_parser.pause_mask)."""
    return [r for r, paused in zip(records, fit_parser.pause_mask(records)) if not paused]


def activity_metrics(activity: ActivitySummary, stats: dict | None, records: list[RecordPoint]) -> ActivityMetrics:
    """EF and decoupling of one activity, over its moving time; `stats` is its Garmin list
    entry (for normalized power, see garmin_client.activity_stats). Fields stay None when the
    data doesn't allow them (no HR, no power on a ride, a sport they don't apply to)."""
    records = moving(records)
    family = tl.sport_family(activity.sport)
    out = ActivityMetrics()
    # A merged recording's summary can lack HR its stream has (e.g. a virtual ride whose
    # HR came from the bike computer): fall back to the stream's average.
    heart_rates = [r.heart_rate for r in records if r.heart_rate]
    avg_hr = activity.avg_hr or (statistics.fmean(heart_rates) if heart_rates else None)
    if family == "cycling":
        powers = [r.power for r in records if r.power is not None]
        power = (stats or {}).get("normPower") or activity.avg_power or (statistics.fmean(powers) if powers else None)
        if power and avg_hr:
            out.efficiency_factor, out.ef_basis = power / avg_hr, "W/bpm"
        samples = [(r.power, r.heart_rate) for r in records if r.power is not None and r.heart_rate]
    elif family in ("running", "hiking"):
        if activity.avg_speed_mps and avg_hr:
            out.efficiency_factor, out.ef_basis = activity.avg_speed_mps * 60 / avg_hr, "m/min per bpm"
        samples = [
            (speed * 60, r.heart_rate) for r, speed in zip(records, _speed_series(records))
            if speed is not None and speed >= MIN_MOVING_SPEED_MPS and r.heart_rate
        ]
    else:
        return out

    if family != "hiking" and activity.duration_s >= MIN_DECOUPLING_S and len(samples) >= 20:
        half = len(samples) // 2
        first, second = _ratio(samples[:half]), _ratio(samples[half:])
        if first and second:
            out.decoupling_pct = (first - second) / first * 100
    return out


def week_metrics(points: list[tl.PmcPoint]) -> WeekMetrics:
    """Metrics of the days in `points` (a week of the PMC series, in order)."""
    if not points:
        return WeekMetrics()
    daily = [p.tss for p in points]
    total = sum(daily)
    first, last = points[0], points[-1]
    ctl_before = first.ctl - (first.tss - first.ctl) / (tl.CTL_DAYS - 1)  # undo the first day's update
    out = WeekMetrics(ramp_rate=last.ctl - ctl_before, acwr=last.atl / last.ctl if last.ctl else None)
    if len(daily) > 1 and total:
        spread = statistics.pstdev(daily)
        if spread:
            out.monotony = statistics.fmean(daily) / spread
            out.strain = total * out.monotony
    return out


# --- Per-segment statistics (a lap, a split, a rest) from the record stream ---------------

NP_WINDOW_S = 30
GRADE_WINDOW_M = 20.0  # altitude is noisy: measure grade over at least this much distance
MAX_GRADE = 0.45
HR_RECOVERY_S = 60


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def normalized_power(records: list[RecordPoint]) -> float | None:
    """Coggan normalized power: the 4th-power mean of the 30 s rolling average power —
    what the effort cost physiologically, unlike a plain average that coasting and surges
    cancel out in. None under 30 s of power data."""
    samples = [(r.timestamp_utc, r.power) for r in records if r.power is not None]
    if len(samples) < NP_WINDOW_S:
        return None
    rolling, window, total = [], [], 0.0
    for ts, power in samples:
        window.append((ts, power))
        total += power
        while (ts - window[0][0]).total_seconds() >= NP_WINDOW_S:
            total -= window.pop(0)[1]
        if (ts - samples[0][0]).total_seconds() >= NP_WINDOW_S - 1:
            rolling.append(total / len(window))
    return statistics.fmean(v ** 4 for v in rolling) ** 0.25 if rolling else None


def _minetti_cost(grade: float) -> float:
    """Energy cost of running at a grade (J/kg/m), Minetti et al. 2002; 3.6 on the flat."""
    i = max(-MAX_GRADE, min(MAX_GRADE, grade))
    return 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6


def grade_adjusted_speed(records: list[RecordPoint]) -> float | None:
    """The flat-ground speed costing the same effort (GAP, in m/s): distance weighted by
    the energy cost of its grade, over the elapsed time. None without distance and
    altitude."""
    points = [r for r in records if r.distance_m is not None and r.altitude_m is not None]
    if len(points) < 2:
        return None
    adjusted = seconds = 0.0
    anchor = points[0]
    for r in points[1:]:
        run = r.distance_m - anchor.distance_m
        if run < GRADE_WINDOW_M and r is not points[-1]:
            continue
        if run > 0:
            adjusted += run * _minetti_cost((r.altitude_m - anchor.altitude_m) / run) / _minetti_cost(0.0)
        seconds += (r.timestamp_utc - anchor.timestamp_utc).total_seconds()
        anchor = r
    return adjusted / seconds if seconds else None


def segment_stats(records: list[RecordPoint], foot: bool) -> dict:
    """Exact statistics of a stretch of the record stream over its moving time (pauses —
    fit_parser.pause_mask — left out; zero-cadence samples also left out of cadence and
    dynamics): HR avg/min/max, power avg and NP, cadence and, on foot, running dynamics and
    grade-adjusted speed, plus moving and paused seconds. Keys with no data are left out."""
    elapsed = (records[-1].timestamp_utc - records[0].timestamp_utc).total_seconds() if len(records) > 1 else 0.0
    mask = fit_parser.pause_mask(records)
    paused_s = sum((b.timestamp_utc - a.timestamp_utc).total_seconds() for a, b, p in zip(records, records[1:], mask[1:]) if p)
    all_records, records = records, [r for r, p in zip(records, mask) if not p]
    # Lap / split boundaries are interpolated samples, so values can be fractional: round HR.
    hr = [round(r.heart_rate) for r in records if r.heart_rate]
    power = [r.power for r in records if r.power is not None]
    moving = [r for r in records if r.cadence]
    out = {
        "avg_hr": _mean(hr), "min_hr": min(hr) if hr else None, "max_hr": max(hr) if hr else None,
        "avg_power_w": _mean(power), "np_w": normalized_power(records),
        "cadence": _mean([r.cadence for r in moving]),
    }
    out["moving_s"] = elapsed - paused_s
    if paused_s:
        out["paused_s"] = paused_s
    if foot:
        gap = grade_adjusted_speed(all_records)
        out.update({
            # GAP's distance over moving time, not elapsed
            "gap_speed_mps": gap * elapsed / (elapsed - paused_s) if gap and elapsed > paused_s else gap,
            "ground_contact_ms": _mean([r.stance_time_ms for r in moving if r.stance_time_ms]),
            "vertical_oscillation_mm": _mean([r.vertical_oscillation_mm for r in moving if r.vertical_oscillation_mm]),
            "vertical_ratio_pct": _mean([r.vertical_ratio_pct for r in moving if r.vertical_ratio_pct]),
            "step_length_m": (lambda v: v / 1000 if v else None)(_mean([r.step_length_mm for r in moving if r.step_length_mm])),
        })
    return {k: v for k, v in out.items() if v is not None}


def hr_recovery(records: list[RecordPoint]) -> dict | None:
    """Heart-rate recovery over a rest: HR at its start, HR 60 s in (or at its end if
    shorter), and the drop — faster drops mean better recovery between efforts."""
    hr = [(r.timestamp_utc, round(r.heart_rate)) for r in records if r.heart_rate]
    if len(hr) < 2:
        return None
    start_ts, start_hr = hr[0]
    later = [(ts, v) for ts, v in hr if (ts - start_ts).total_seconds() <= HR_RECOVERY_S]
    end_ts, end_hr = later[-1]
    return {
        "start_bpm": start_hr, "end_bpm": end_hr, "drop_bpm": start_hr - end_hr,
        "over_s": round((end_ts - start_ts).total_seconds()),
    }


def time_in_zones(records: list[RecordPoint], field: str, floors: list[float], max_gap_s: float = 10) -> list[float] | None:
    """Seconds in each zone of a metric (zone i from floors[i] up to floors[i+1]; below the
    first floor counts as zone 1) — the exact distribution, not lap averages."""
    if not floors:
        return None
    seconds = [0.0] * len(floors)
    for prev, rec in zip(records, records[1:]):
        value = getattr(prev, field)
        gap = (rec.timestamp_utc - prev.timestamp_utc).total_seconds()
        if value is None or not 0 < gap <= max_gap_s:
            continue
        seconds[max((i for i, f in enumerate(floors) if value >= f), default=0)] += gap
    return seconds if any(seconds) else None
