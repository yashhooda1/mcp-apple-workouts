"""Fitness assessment and pace derivation — the part that makes a plan *yours*.

Everything here is a pure function over the activity dicts that
`list_activities` already returns, so the whole gap-analysis path is testable
offline with no credentials and no network.

The pipeline a plan generator runs:

    activities  ->  summarize_fitness()  ->  FitnessSnapshot
    snapshot + goal + race date  ->  assess_gap()  ->  GapAssessment
    GapAssessment  ->  plans.marathon / plans.track_hs  ->  TrainingPlan

The honest bit is `assess_gap`: it will tell you the goal doesn't fit in the
weeks you have. A plan generator that quietly ramps someone from 15 to 55 miles
a week in ten weeks isn't ambitious, it's an injury with a calendar attached.
When the gap is too big it returns an achievable goal alongside the requested
one and builds toward the achievable one unless the caller insists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

M_PER_MILE = 1609.34

# Race distances in miles
MI_5K = 3.10686
MI_10K = 6.21371
MI_HALF = 13.10940
MI_MARATHON = 26.21880
MI_800 = 0.49710
MI_1600 = 0.99419
MI_3200 = 1.98839

# Riegel's fatigue exponent. 1.06 is the classic value and holds well from
# ~1500m out to the marathon for a trained runner with the endurance to back it.
RIEGEL_EXP = 1.06


# --------------------------------------------------------------------------- #
# time / pace formatting
# --------------------------------------------------------------------------- #
def parse_hms(text: str) -> float:
    """'2:59:30' / '15:00' / '1:58' -> seconds."""
    parts = [float(p) for p in str(text).strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_hms(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(sec_per_mile: float) -> str:
    s = int(round(sec_per_mile))
    return f"{s // 60}:{s % 60:02d}"


def window(center_s: float, fast: float = 6, slow: float = 6) -> str:
    """Build a `pace:M:SS-M:SS` target window around a centre pace (sec/mile)."""
    return f"pace:{fmt_pace(center_s - fast)}-{fmt_pace(center_s + slow)}"


def riegel(time_s: float, from_mi: float, to_mi: float,
           exp: float = RIEGEL_EXP) -> float:
    """Equivalent time over another distance."""
    return time_s * (to_mi / from_mi) ** exp


# --------------------------------------------------------------------------- #
# training paces
# --------------------------------------------------------------------------- #
@dataclass
class Paces:
    """Training paces in seconds per mile, plus ready-made target windows."""
    marathon: float
    half: float
    threshold: float
    ten_k: float
    five_k: float
    rep: float
    easy: float
    steady: float

    def targets(self, heat_penalty: float = 0.0) -> dict:
        """`pace:` target strings. heat_penalty adds seconds/mile to quality work —
        25-40 s/mi is realistic for a Gulf Coast August."""
        h = heat_penalty
        return {
            "MP": window(self.marathon + h, 4, 4),
            "HMP": window(self.half + h, 5, 5),
            "THRESH": window(self.threshold + h, 7, 7),
            "TEN_K": window(self.ten_k + h, 6, 7),
            "FIVE_K": window(self.five_k + h, 6, 6),
            "REP": window(self.rep + h, 7, 8),
            "STEADY": window(self.steady + h, 8, 8),
            "EASY": "hr:2",  # easy is always HR-capped, never pace-chased
        }


def paces_from_marathon(goal_seconds: float) -> Paces:
    """Derive the full training-pace set from a marathon goal time."""
    mp = goal_seconds / MI_MARATHON
    half_s = riegel(goal_seconds, MI_MARATHON, MI_HALF)
    ten_s = riegel(goal_seconds, MI_MARATHON, MI_10K)
    five_s = riegel(goal_seconds, MI_MARATHON, MI_5K)
    hmp = half_s / MI_HALF
    return Paces(
        marathon=mp,
        half=hmp,
        threshold=hmp - 6,      # ~60-minute race effort
        ten_k=ten_s / MI_10K,
        five_k=five_s / MI_5K,
        rep=five_s / MI_5K - 15,
        easy=mp + 75,
        steady=mp + 30,
    )


def paces_from_race(time_s: float, distance_mi: float) -> Paces:
    """Derive training paces from any recent race result, via its marathon
    equivalent. Note the caveat: for a pure 800m runner the marathon equivalent
    is optimistic, which is exactly why plan builders use *current-fitness*
    paces for aerobic work and *goal* paces only for race-specific reps."""
    return paces_from_marathon(riegel(time_s, distance_mi, MI_MARATHON))


# --------------------------------------------------------------------------- #
# fitness snapshot
# --------------------------------------------------------------------------- #
@dataclass
class FitnessSnapshot:
    weeks_analyzed: int
    weekly_miles: list = field(default_factory=list)   # oldest -> newest
    avg_weekly_miles: float = 0.0        # mean of the last 4 weeks
    peak_weekly_miles: float = 0.0
    longest_run_miles: float = 0.0
    runs_per_week: float = 0.0
    avg_easy_pace_s: Optional[float] = None
    best_effort: Optional[dict] = None   # {'distance_mi','time_s','pace_s','date','name'}
    confidence: str = "low"              # low | medium | high
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "weeks_analyzed": self.weeks_analyzed,
            "weekly_miles": [round(m, 1) for m in self.weekly_miles],
            "avg_weekly_miles": round(self.avg_weekly_miles, 1),
            "peak_weekly_miles": round(self.peak_weekly_miles, 1),
            "longest_run_miles": round(self.longest_run_miles, 1),
            "runs_per_week": round(self.runs_per_week, 1),
            "avg_easy_pace": fmt_pace(self.avg_easy_pace_s) if self.avg_easy_pace_s else None,
            "best_effort": (
                {**self.best_effort,
                 "distance_mi": round(self.best_effort["distance_mi"], 2),
                 "time": fmt_hms(self.best_effort["time_s"]),
                 "pace": fmt_pace(self.best_effort["pace_s"])}
                if self.best_effort else None
            ),
            "confidence": self.confidence,
            "notes": self.notes,
        }


def _activity_date(act: dict) -> Optional[date]:
    raw = act.get("start") or act.get("startTimeLocal")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "")).date()
    except ValueError:
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def summarize_fitness(activities: list, weeks: int = 8,
                      today: Optional[date] = None) -> FitnessSnapshot:
    """Reduce raw activities to the handful of numbers a plan needs.

    Accepts the shape `list_activities` returns (`distance_m`, `duration_s`,
    `type`, `start`) and tolerates the raw Garmin/Strava shapes too.
    """
    today = today or date.today()
    start = today - timedelta(weeks=weeks)
    buckets = [0.0] * weeks
    counts = [0] * weeks
    longest = 0.0
    easy_paces: list[float] = []
    best: Optional[dict] = None

    for act in activities:
        kind = act.get("type") or act.get("activityType", {}).get("typeKey", "")
        if "run" not in str(kind).lower():
            continue
        when = _activity_date(act)
        if not when or when < start or when > today:
            continue
        meters = float(act.get("distance_m") or act.get("distance") or 0)
        seconds = float(act.get("duration_s") or act.get("duration") or 0)
        miles = meters / M_PER_MILE
        if miles <= 0:
            continue
        idx = min(weeks - 1, (when - start).days // 7)
        buckets[idx] += miles
        counts[idx] += 1
        longest = max(longest, miles)

        if seconds > 0:
            pace = seconds / miles
            if miles >= 3:
                if best is None or pace < best["pace_s"]:
                    best = {"distance_mi": miles, "time_s": seconds, "pace_s": pace,
                            "date": when.isoformat(),
                            "name": act.get("name") or act.get("activityName") or "run"}
            if 2 <= miles <= 10:
                easy_paces.append(pace)

    recent = buckets[-4:] if len(buckets) >= 4 else buckets
    avg = sum(recent) / max(1, len([b for b in recent])) if recent else 0.0
    total_runs = sum(counts)

    notes: list[str] = []
    confidence = "high"
    if total_runs < max(10, weeks + 2):
        confidence = "low"
        notes.append(
            f"Only {total_runs} runs found in the last {weeks} weeks — the snapshot is "
            "thin, so the plan starts conservatively. Pass current_weekly_miles "
            "explicitly if your history lives somewhere other than Strava."
        )
    elif total_runs < 20 or best is None:
        confidence = "medium"
    if best is None:
        notes.append(
            "No run of 3+ miles with a time to anchor current fitness. Paces are derived "
            "from the goal rather than from measured fitness — add a recent race or time "
            "trial for a sharper plan."
        )

    # Median-ish easy pace: trim the fastest quarter so quality days don't skew it.
    avg_easy = None
    if easy_paces:
        easy_paces.sort()
        keep = easy_paces[len(easy_paces) // 4:]
        avg_easy = sum(keep) / len(keep)

    return FitnessSnapshot(
        weeks_analyzed=weeks,
        weekly_miles=buckets,
        avg_weekly_miles=avg,
        peak_weekly_miles=max(buckets) if buckets else 0.0,
        longest_run_miles=longest,
        runs_per_week=total_runs / weeks if weeks else 0.0,
        avg_easy_pace_s=avg_easy,
        best_effort=best,
        confidence=confidence,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# gap analysis
# --------------------------------------------------------------------------- #
# Peak weekly mileage that the goal realistically asks for. These are the
# low end of what the goal is usually run off, not the maximum anyone has done.
MARATHON_PEAK_MILEAGE = [
    (2 * 3600 + 30 * 60, 80),   # sub-2:30
    (2 * 3600 + 45 * 60, 70),   # sub-2:45
    (3 * 3600, 55),             # sub-3:00
    (3 * 3600 + 15 * 60, 48),
    (3 * 3600 + 30 * 60, 42),
    (3 * 3600 + 45 * 60, 38),
    (4 * 3600, 33),
    (4 * 3600 + 30 * 60, 28),
    (10 * 3600, 25),            # everything slower
]

MAX_WEEKLY_RAMP = 1.08          # 8%/week on build weeks
ABSOLUTE_WEEKLY_CEILING = 90.0


def required_peak_mileage(goal_seconds: float) -> int:
    for cutoff, miles in MARATHON_PEAK_MILEAGE:
        if goal_seconds <= cutoff:
            return miles
    return 25


def achievable_peak(current_mpw: float, weeks: int) -> float:
    """Where a safe ramp actually lands: 3 build weeks up, 1 down, repeating,
    with the last 3 weeks given to the taper."""
    build_weeks = max(0, weeks - 3)
    mpw = max(current_mpw, 12.0)
    for w in range(build_weeks):
        if (w + 1) % 4 == 0:       # down week — no increase
            continue
        mpw = min(mpw * MAX_WEEKLY_RAMP, ABSOLUTE_WEEKLY_CEILING)
    return mpw


def weeks_to_reach(current_mpw: float, target_mpw: float, cap: int = 104) -> Optional[int]:
    """Shortest block length that ramps `current_mpw` to `target_mpw`.

    Inverts `achievable_peak` under the same 3-up-1-down rule, so the number
    quoted to a runner matches the ramp the plan would actually build. Returns
    None when the target sits above the absolute weekly ceiling, i.e. when no
    amount of runway gets there.
    """
    if achievable_peak(current_mpw, cap) < target_mpw:
        return None
    for w in range(1, cap + 1):
        if achievable_peak(current_mpw, w) >= target_mpw:
            return w
    return None


@dataclass
class GapAssessment:
    goal_seconds: float
    race_date: date
    weeks_available: int
    current_mpw: float
    required_peak_mpw: float
    achievable_peak_mpw: float
    verdict: str                      # on_track | stretch | out_of_reach
    recommended_goal_seconds: float
    longest_run_needed: float
    gaps: list = field(default_factory=list)
    actions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "goal": fmt_hms(self.goal_seconds),
            "goal_pace": fmt_pace(self.goal_seconds / MI_MARATHON) + "/mi",
            "race_date": self.race_date.isoformat(),
            "weeks_available": self.weeks_available,
            "current_weekly_miles": round(self.current_mpw, 1),
            "required_peak_weekly_miles": round(self.required_peak_mpw, 1),
            "achievable_peak_weekly_miles": round(self.achievable_peak_mpw, 1),
            "longest_run_needed_miles": self.longest_run_needed,
            "verdict": self.verdict,
            "recommended_goal": fmt_hms(self.recommended_goal_seconds),
            "recommended_goal_pace": fmt_pace(
                self.recommended_goal_seconds / MI_MARATHON) + "/mi",
            "gaps": self.gaps,
            "actions": self.actions,
        }


def _goal_for_peak_mileage(peak: float) -> float:
    """Invert the mileage table: the fastest goal that peak realistically supports."""
    for cutoff, miles in MARATHON_PEAK_MILEAGE:
        if peak >= miles:
            return float(cutoff)
    return float(MARATHON_PEAK_MILEAGE[-1][0])


def assess_gap(snapshot: FitnessSnapshot, goal_time: str, race_date: str,
               today: Optional[date] = None,
               current_weekly_miles: Optional[float] = None) -> GapAssessment:
    """Compare where the athlete is against what the goal asks for."""
    today = today or date.today()
    race = date.fromisoformat(race_date)
    goal_s = parse_hms(goal_time)
    weeks = max(1, (race - today).days // 7)
    current = current_weekly_miles if current_weekly_miles is not None \
        else snapshot.avg_weekly_miles

    required = float(required_peak_mileage(goal_s))
    reachable = achievable_peak(current, weeks)
    longest_needed = 20.0 if goal_s <= 3 * 3600 + 30 * 60 else 18.0

    gaps: list[str] = []
    actions: list[str] = []

    mileage_gap = required - reachable
    if mileage_gap > 0:
        gaps.append(
            f"Peak mileage: the goal is usually run off ~{required:.0f} mi/wk. From "
            f"{current:.0f} mi/wk, a safe ramp over {weeks} weeks tops out near "
            f"{reachable:.0f} mi/wk — about {mileage_gap:.0f} mi/wk short."
        )
    if snapshot.longest_run_miles and snapshot.longest_run_miles < longest_needed - 6:
        gaps.append(
            f"Long run: longest recent run is {snapshot.longest_run_miles:.0f} mi; the "
            f"plan needs to reach {longest_needed:.0f} mi at least twice before the taper."
        )
    if snapshot.runs_per_week and snapshot.runs_per_week < 4:
        gaps.append(
            f"Frequency: {snapshot.runs_per_week:.1f} runs/week. Marathon-specific work "
            "needs 5, so the first block adds days before it adds miles."
        )
    if weeks < 12:
        gaps.append(
            f"Time: {weeks} weeks to race day. Under 12 weeks the block skips general "
            "base-building and goes straight to specific work, which caps how much "
            "fitness can actually be added."
        )

    if mileage_gap <= 0 and weeks >= 12:
        verdict = "on_track"
        recommended = goal_s
        actions.append("Goal fits the runway — the plan builds straight toward it.")
    elif mileage_gap <= required * 0.20:
        verdict = "stretch"
        recommended = goal_s
        actions.append(
            "Goal is a stretch but reachable. The plan ramps to the ceiling a safe build "
            "allows and treats the goal as the A-target with a B-target fallback on the day."
        )
    else:
        verdict = "out_of_reach"
        recommended = max(goal_s, _goal_for_peak_mileage(reachable))
        actions.append(
            f"The plan builds toward {fmt_hms(recommended)} instead, which is what this "
            f"runway supports. Pass force_goal=True to build to {fmt_hms(goal_s)} anyway — "
            "the sessions will be paced at a fitness you won't have yet."
        )
        needed = weeks_to_reach(current, required)
        if needed is None:
            actions.append(
                f"To hold {fmt_hms(goal_s)} you'd need to peak near {required} mi/wk, which is "
                f"past the {ABSOLUTE_WEEKLY_CEILING:.0f} mi/wk ceiling this builder ramps to. "
                "That goal is a coach-and-multi-year conversation, not a scheduling one."
            )
        else:
            actions.append(
                f"To hold {fmt_hms(goal_s)}: either move the goal race later — reaching "
                f"{required} mi/wk from {current:.0f} takes about {needed} weeks at 8%/wk with "
                f"a down week every fourth, so roughly {max(1, needed - weeks)} more than you "
                "have — or pick a later race in the same series."
            )

    actions.append(
        "Two strength sessions a week are scheduled regardless — they are the cheapest "
        "injury insurance in the block, not an optional extra."
    )
    if snapshot.confidence == "low":
        actions.append(
            "Fitness snapshot confidence is low. Re-run the assessment after 3-4 weeks of "
            "logged running and the plan will re-scale."
        )

    return GapAssessment(
        goal_seconds=goal_s,
        race_date=race,
        weeks_available=weeks,
        current_mpw=current,
        required_peak_mpw=required,
        achievable_peak_mpw=reachable,
        verdict=verdict,
        recommended_goal_seconds=recommended,
        longest_run_needed=longest_needed,
        gaps=gaps,
        actions=actions,
    )


def mileage_ramp(current: float, peak: float, weeks: int) -> list[float]:
    """Weekly mileage across the block: 3 up / 1 down, then a 3-week taper.

    Down weeks sit at ~75% of the week before; the taper steps 75/55/35% of peak.
    """
    current = max(current, 10.0)
    peak = max(peak, current)
    build_weeks = max(1, weeks - 3)
    out: list[float] = []
    mpw = current
    # growth factor that lands on `peak` at the end of the build, capped for safety
    ups = len([w for w in range(build_weeks) if (w + 1) % 4 != 0]) or 1
    growth = min(MAX_WEEKLY_RAMP, (peak / current) ** (1 / ups))
    for w in range(build_weeks):
        if (w + 1) % 4 == 0:
            out.append(round(mpw * 0.75, 1))
        else:
            out.append(round(mpw, 1))
            mpw = min(mpw * growth, peak)
    for factor in (0.75, 0.55, 0.35):
        out.append(round(peak * factor, 1))
    return out[:weeks]
