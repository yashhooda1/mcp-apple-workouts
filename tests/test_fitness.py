"""Offline tests: pace math, the fitness snapshot, and the honesty of the gap analysis."""
from datetime import date, timedelta

from apple_workouts_mcp.fitness import (
    M_PER_MILE,
    MI_MARATHON,
    assess_gap,
    fmt_hms,
    fmt_pace,
    mileage_ramp,
    paces_from_marathon,
    parse_hms,
    required_peak_mileage,
    riegel,
    summarize_fitness,
)

TODAY = date(2026, 9, 15)


def _runs(weekly_miles, weeks=8, per_week=4, pace_s=540, today=TODAY):
    """Synthetic activity feed: `weekly_miles` spread over `per_week` runs."""
    out = []
    for w in range(weeks):
        start = today - timedelta(weeks=weeks) + timedelta(days=7 * w)
        for d in range(per_week):
            miles = weekly_miles / per_week
            out.append({
                "type": "running",
                "start": (start + timedelta(days=d)).isoformat() + "T06:00:00",
                "distance_m": miles * M_PER_MILE,
                "duration_s": miles * pace_s,
                "name": f"run w{w}d{d}",
            })
    return out


def test_time_parsing_round_trip():
    assert parse_hms("2:59:30") == 10770
    assert parse_hms("15:00") == 900
    assert fmt_hms(10770) == "2:59:30"
    assert fmt_pace(412) == "6:52"


def test_riegel_equivalents_are_sane():
    marathon = parse_hms("3:00:00")
    half = riegel(marathon, MI_MARATHON, 13.1094)
    # A 3:00 marathon equates to roughly a 1:26 half.
    assert 85 * 60 < half < 88 * 60


def test_paces_order_correctly():
    p = paces_from_marathon(parse_hms("3:00:00"))
    assert p.rep < p.five_k < p.ten_k < p.threshold < p.half < p.marathon < p.easy
    assert p.targets()["EASY"] == "hr:2"   # easy is HR-capped, never pace-chased


def test_snapshot_reads_weekly_volume():
    snap = summarize_fitness(_runs(30), weeks=8, today=TODAY)
    assert 28 < snap.avg_weekly_miles < 32
    assert snap.runs_per_week == 4.0
    assert snap.best_effort is not None
    assert snap.confidence == "high"


def test_snapshot_ignores_non_runs_and_stale_activities():
    acts = _runs(30) + [
        {"type": "cycling", "start": "2026-09-10T06:00:00",
         "distance_m": 40000, "duration_s": 4800},
        {"type": "running", "start": "2024-01-01T06:00:00",
         "distance_m": 20000, "duration_s": 7200},
    ]
    snap = summarize_fitness(acts, weeks=8, today=TODAY)
    assert 28 < snap.avg_weekly_miles < 32


def test_thin_history_is_flagged_not_hidden():
    snap = summarize_fitness(_runs(10, per_week=1), weeks=8, today=TODAY)
    assert snap.confidence == "low"
    assert snap.notes


def test_peak_mileage_table_is_monotonic():
    assert required_peak_mileage(parse_hms("2:45:00")) > \
        required_peak_mileage(parse_hms("3:30:00"))


def test_gap_analysis_calls_out_an_unreachable_goal():
    """18 mi/wk, 14 weeks, sub-3 goal: the plan must say no, not ramp into injury."""
    snap = summarize_fitness(_runs(18), weeks=8, today=TODAY)
    race = (TODAY + timedelta(weeks=14)).isoformat()
    g = assess_gap(snap, "3:00:00", race, today=TODAY)
    assert g.verdict == "out_of_reach"
    assert g.recommended_goal_seconds > g.goal_seconds   # slower goal
    assert g.gaps and g.actions
    assert g.achievable_peak_mpw < g.required_peak_mpw


def test_gap_analysis_accepts_a_well_matched_goal():
    snap = summarize_fitness(_runs(45), weeks=8, today=TODAY)
    race = (TODAY + timedelta(weeks=20)).isoformat()
    g = assess_gap(snap, "3:15:00", race, today=TODAY)
    assert g.verdict in ("on_track", "stretch")


def test_ramp_is_safe_and_ends_in_a_taper():
    ramp = mileage_ramp(25, 50, 18)
    assert len(ramp) == 18
    body = ramp[:-3]
    # Compare build week to build week: a down week dips to 75% and the next
    # build week steps back up past it, which is the point of a down week.
    build = [m for i, m in enumerate(body) if (i + 1) % 4 != 0]
    for prev, nxt in zip(build, build[1:]):
        assert nxt <= prev * 1.09 + 0.1, "no build week may jump more than ~8%"
    for i, m in enumerate(body):
        if (i + 1) % 4 == 0:
            assert m < body[i - 1], "down weeks must actually come down"
    assert ramp[-1] < ramp[-2] < ramp[-3] < max(body), "taper must descend"
    assert max(ramp) <= 50.1
