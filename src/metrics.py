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


def activity_metrics(activity: ActivitySummary, stats: dict | None, records: list[RecordPoint]) -> ActivityMetrics:
    """EF and decoupling of one activity; `stats` is its Garmin list entry (for normalized
    power, see garmin_client.activity_stats). Fields stay None when the data doesn't allow
    them (no HR, no power on a ride, a sport they don't apply to)."""
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
