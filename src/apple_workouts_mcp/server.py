"""apple-workouts-mcp server — the Apple Watch counterpart to mcp-garmin.

Tools (least-privilege):
  read   — list_activities, assess_fitness, list_scheduled, sync_status,
           preview_workout, list_marathons, pick_random_marathon
  write  — create_workout, create_strength_workout, unschedule_workout,
           clear_scheduled
  plan   — create_training_plan, create_marathon_plan, create_hs_track_plan

Writes go to the workout feed (see feed.py), never to a cloud account. HoodaRun
on the iPhone turns the feed into WorkoutKit workouts and schedules them onto
the Watch. Strava is read-only and optional.

Every plan-building tool schedules two strength/core sessions a week and
returns nutrition and recovery guidance alongside the schedule.
"""
from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .apple import compile_any, parse_spec, step_count
from .feed import FeedStore
from .fitness import summarize_fitness
from .plans import TrainingPlan, feed_item, preview_plan, push_plan
from .strength import StrengthSpec
from .workouts import WorkoutSpec

mcp = FastMCP("apple-workouts-mcp")


def store() -> FeedStore:
    return FeedStore()


def _summary(w: dict) -> dict:
    return {"id": w["id"], "date": w["date"], "time": w.get("time"), "name": w["name"],
            "sport": w["sport"], "plan": w.get("plan")}


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_activities(days: int = 30, limit: int = 30) -> list:
    """Recent activities from Strava (read-only): id, name, type, start, distance, HR."""
    from .strava import list_activities as strava_list

    return strava_list(days=days, limit=limit)


def _snapshot(weeks: int = 8):
    from .strava import list_activities as strava_list

    return summarize_fitness(strava_list(days=weeks * 7 + 7, limit=400), weeks=weeks)


@mcp.tool()
def assess_fitness(goal_time: Optional[str] = None, race_date: Optional[str] = None,
                   weeks_of_history: int = 8,
                   current_weekly_miles: Optional[float] = None) -> dict:
    """Read-only: summarise recent training (from Strava), and size the gap to a
    marathon goal.

    With `goal_time` ('3:15:00') and `race_date` ('2027-04-19') also returns what
    the goal asks for, what a safe ramp reaches, and — when those disagree — the
    goal this runway does support.
    """
    from .fitness import assess_gap

    snap = _snapshot(weeks_of_history)
    out = {"fitness": snap.as_dict()}
    if goal_time and race_date:
        out["assessment"] = assess_gap(
            snap, goal_time, race_date, current_weekly_miles=current_weekly_miles
        ).as_dict()
    return out


@mcp.tool()
def list_scheduled(start_date: Optional[str] = None,
                   end_date: Optional[str] = None) -> dict:
    """Workouts in the feed (optionally within [start_date, end_date]), each marked
    with whether HoodaRun has confirmed it's scheduled on the Watch."""
    s = store()
    status = s.status() or {}
    on_watch = {x.get("id"): x for x in status.get("scheduled", [])}
    rows = []
    for w in s.between(start_date, end_date):
        row = _summary(w)
        hit = on_watch.get(w["id"])
        row["on_watch"] = bool(hit and hit.get("rev") == w["rev"])
        rows.append(row)
    return {"feed": str(s.feed_path), "count": len(rows), "workouts": rows,
            "last_app_sync": status.get("synced_at")}


@mcp.tool()
def sync_status() -> dict:
    """What HoodaRun last reported: when it synced, what's on the Watch, what's
    waiting for a free slot (WorkoutKit caps scheduled workouts), and errors."""
    s = store()
    status = s.status()
    if status is None:
        return {"feed": str(s.feed_path), "status": None,
                "note": "HoodaRun hasn't synced yet — open the app on the iPhone."}
    return {"feed": str(s.feed_path), **status}


@mcp.tool()
def preview_workout(spec: dict) -> dict:
    """Compile a workout or strength spec and return the exact WorkoutKit-shaped
    JSON HoodaRun would receive, without publishing anything."""
    parsed = parse_spec(spec)
    w = compile_any(parsed)
    return {"name": parsed.name, "steps": step_count(w), "workout": w}


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def _publish_one(spec, schedule_date: str, time: Optional[str]) -> dict:
    item = feed_item(schedule_date, spec, time=time)
    stored = store().upsert([item])[0]
    return {**_summary(stored), "published": True,
            "note": "HoodaRun schedules it on the Watch at its next sync."}


@mcp.tool()
def create_workout(spec: WorkoutSpec, schedule_date: str,
                   time: Optional[str] = None) -> dict:
    """Create a structured running workout and schedule it for a date.

    schedule_date is 'YYYY-MM-DD'; time is optional 24h 'HH:MM'. The same name on
    the same date overwrites instead of duplicating. Targets: 'hr:2' (HR zone)
    or 'pace:6:35-6:55' (min/mi window); durations '15:00', '1mi', '400m', 'open'.
    Set spec.location='indoor' for treadmill sessions.
    """
    return _publish_one(spec, schedule_date, time)


@mcp.tool()
def create_strength_workout(spec: StrengthSpec, schedule_date: str,
                            time: Optional[str] = None) -> dict:
    """Create a guided strength/core workout and schedule it for a date.

    Each exercise takes `reps` (an open set you end with the Action button) or
    `hold` ('45s', timed), plus sets and rest_s. Each set shows on the Watch by
    name, e.g. 'Romanian Deadlift · 8 reps', with a timed rest between sets.
    """
    return _publish_one(spec, schedule_date, time)


@mcp.tool()
def unschedule_workout(workout_id: str) -> dict:
    """Remove one workout from the feed by id; HoodaRun removes it from the Watch
    at its next sync."""
    gone = store().remove([workout_id])
    return {"removed": [_summary(w) for w in gone], "found": bool(gone)}


@mcp.tool()
def clear_scheduled(start_date: str, end_date: str, dry_run: bool = True) -> dict:
    """Remove every feed workout in [start_date, end_date] inclusive.

    Destructive. Defaults to dry_run=True — call again with dry_run=False to commit.
    Only touches workouts this server published; workouts made in HoodaRun's own
    builder are never removed.
    """
    s = store()
    hits = s.between(start_date, end_date)
    if dry_run or not hits:
        return {"range": [start_date, end_date], "found": len(hits), "dry_run": dry_run,
                "would_remove": [_summary(w) for w in hits]}
    gone = s.remove([w["id"] for w in hits])
    return {"range": [start_date, end_date], "removed": len(gone),
            "items": [_summary(w) for w in gone]}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_training_plan(name: str, items: list, time: Optional[str] = None,
                         replace: bool = True, dry_run: bool = False) -> dict:
    """Publish a multi-week plan you wrote yourself.

    `items` is a list of {"date": "YYYY-MM-DD", "spec": <WorkoutSpec or
    StrengthSpec>} — a spec with `exercises` is strength, otherwise running.
    Re-pushing the same plan name updates it in place, and (replace=True) drops
    sessions from the earlier push that are no longer in the plan.
    dry_run=True validates and reports without publishing.
    """
    plan = TrainingPlan(name=name,
                        items=[(it["date"], parse_spec(it["spec"])) for it in items])
    if dry_run:
        return {"plan": name, "dry_run": True, "sessions": preview_plan(plan, time)}
    return {"plan": name, **push_plan(store(), plan, replace=replace, time=time)}


def _finish(plan, report, replace: bool, dry_run: bool, time: Optional[str]) -> dict:
    if dry_run:
        return {"plan": plan.name, "dry_run": True,
                "sessions": preview_plan(plan, time), "report": report}
    return {"plan": plan.name, **push_plan(store(), plan, replace=replace, time=time),
            "report": report}


@mcp.tool()
def create_marathon_plan(race_name: str, race_date: str, goal_time: str,
                         current_weekly_miles: Optional[float] = None,
                         days_per_week: int = 5, start_date: Optional[str] = None,
                         heat_penalty_s_per_mile: float = 0.0,
                         use_strava_history: bool = True,
                         force_goal: bool = False, time: Optional[str] = None,
                         replace: bool = True, dry_run: bool = True) -> dict:
    """Build a marathon block for ANY race and ANY goal, scaled to current fitness.

    Reads recent Strava history (unless `use_strava_history=False` or
    `current_weekly_miles` is supplied), compares it to what the goal asks for,
    and ramps from where the runner actually is — 8%/week ceiling,
    3-up-1-down, 3-week taper, ending on race day. If the goal doesn't fit the
    runway, the plan targets the goal that does and says so; `force_goal=True`
    overrides. Two strength sessions a week; weekly fuelling/recovery guidance
    in the report. dry_run=True validates without publishing.
    """
    from plans.marathon import build_marathon_plan

    snap = _snapshot() if use_strava_history and current_weekly_miles is None else None
    plan, report = build_marathon_plan(
        race_name=race_name, race_date=race_date, goal_time=goal_time,
        snapshot=snap, current_weekly_miles=current_weekly_miles,
        days_per_week=days_per_week, start_date=start_date,
        heat_penalty=heat_penalty_s_per_mile, force_goal=force_goal,
    )
    return _finish(plan, report, replace, dry_run, time)


@mcp.tool()
def create_hs_track_plan(event: str = "1600", goal_time: Optional[str] = None,
                         weeks: int = 16, start_date: Optional[str] = None,
                         current_pr: Optional[str] = None,
                         current_weekly_miles: float = 0.0,
                         training_age: str = "developing",
                         meet_days: Optional[list] = None, time: Optional[str] = None,
                         replace: bool = True, dry_run: bool = True) -> dict:
    """Build a high-school track block: 800 / 1600 / 3200 / 5k.

    `goal_time` defaults to the barrier — sub-2:00 800, sub-4:30 1600, sub-10:00
    3200, sub-15:00 5K. With `current_pr` the report sizes the gap honestly.
    `training_age` ('new' | 'developing' | 'experienced') sets a hard mileage
    ceiling; Sunday is always rest; no doubles; nutrition guidance is
    adequacy-only. Take it to the athlete's school coach before running it.
    dry_run=True validates without publishing.
    """
    from plans.track_hs import build_track_plan

    plan, report = build_track_plan(
        event=event, goal_time=goal_time, weeks=weeks, start_date=start_date,
        current_pr=current_pr, current_weekly_miles=current_weekly_miles,
        training_age=training_age, meet_days=meet_days,
    )
    return _finish(plan, report, replace, dry_run, time)


@mcp.tool()
def list_marathons(min_weeks_out: int = 0, max_weeks_out: int = 104) -> list:
    """Marathons in the catalogue whose next running falls inside the window.
    Dates come from each race's scheduling rule — confirm with the organiser."""
    from plans.races import list_races

    return list_races(min_weeks_out, max_weeks_out)


@mcp.tool()
def pick_random_marathon(min_weeks_out: int = 16, max_weeks_out: int = 104,
                         seed: Optional[int] = None) -> dict:
    """Pick a marathon at random from the catalogue — the 'surprise me' path."""
    from plans.races import random_race

    return random_race(min_weeks_out, max_weeks_out, seed=seed) or {
        "error": "no races in that window"
    }


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def main() -> None:
    transport = os.environ.get("TRANSPORT", "stdio")
    if transport in ("sse", "streamable-http"):
        from .http_auth import serve_http

        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8000"))
        serve_http(mcp, transport, host, port, os.environ.get("MCP_AUTH_TOKEN"),
                   store=store())
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
