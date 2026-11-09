"""iCalendar (RFC 5545) view of the workout feed, for Apple Calendar.

The same `workouts.json` HoodaRun reads becomes a subscribable calendar:

    GET /calendar/<CALENDAR_TOKEN>.ics   (secret-in-URL, read-only — for
                                          Apple Calendar subscriptions, which
                                          can't send a bearer header)
    GET /calendar.ics                    (behind the normal bearer gate)

Subscribe once in Calendar (File › New Calendar Subscription, or on iPhone
Settings › Calendar › Accounts › Add Subscribed Calendar) with the
`webcal://` form of the URL. Every workout Claude or ChatGPT publishes then
appears as an event whose notes list the steps and paces.

Events use the workout id as UID, so re-publishing a plan updates events in
place. Times are floating local times — 06:00 means 06:00 wherever the
calendar is viewed, which is what a training plan means. A workout with no
time is an all-day event.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

PRODID = "-//Yash Hooda//apple-workouts-mcp//EN"
_METERS_PER_MILE = 1609.344


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545 §3.1 without splitting a UTF-8 character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur, size = cur + ch, size + n
    out.append(cur)
    return "\r\n".join(out)


def _stamp(iso: Optional[str]) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pace(mps: float) -> str:
    secs = round(_METERS_PER_MILE / mps)
    return f"{secs // 60}:{secs % 60:02d}"


def _goal(goal: dict) -> str:
    kind = goal.get("type")
    if kind == "time":
        s = round(goal.get("seconds") or 0)
        return f"{s // 60}:{s % 60:02d}" if s % 60 else f"{s // 60} min"
    if kind == "distance":
        m = goal.get("meters") or 0
        miles = m / _METERS_PER_MILE
        if abs(miles - round(miles, 2)) < 0.005 and miles >= 0.25:
            return f"{round(miles, 2):g} mi"
        return f"{round(m):d} m"
    return "open"


def _alert(alert: Optional[dict]) -> str:
    if not alert:
        return ""
    if alert.get("type") == "hr_zone":
        return f" @ HR zone {alert.get('zone')}"
    if alert.get("type") == "speed" and alert.get("min_mps") and alert.get("max_mps"):
        # Faster speed = lower pace, so max speed gives the quick end of the band.
        return f" @ {_pace(alert['max_mps'])}–{_pace(alert['min_mps'])}/mi"
    return ""


def _step(label: str, step: dict) -> str:
    if step.get("display_name"):
        return f"{label}: {step['display_name']}"
    return f"{label}: {_goal(step.get('goal') or {})}{_alert(step.get('alert'))}"


def describe(item: dict) -> str:
    """Plain-text step list for the event notes."""
    w = item.get("workout") or {}
    lines = []
    if item.get("plan"):
        lines.append(f"Plan: {item['plan']}")
    if item.get("description"):
        lines.append(item["description"])
    if w.get("warmup"):
        lines.append(_step("Warm-up", w["warmup"]))
    for block in w.get("blocks") or []:
        reps = block.get("iterations") or 1
        steps = block.get("steps") or []
        if reps > 1:
            lines.append(f"{reps}×")
        for s in steps:
            label = "Work" if s.get("purpose") == "work" else "Recover"
            lines.append(("  " if reps > 1 else "") + _step(label, s.get("step") or {}))
    if w.get("cooldown"):
        lines.append(_step("Cool-down", w["cooldown"]))
    lines.append("Scheduled on Apple Watch by HoodaRun.")
    return "\n".join(lines)


def _event(item: dict) -> list[str]:
    d = date.fromisoformat(item["date"])
    est = (item.get("workout") or {}).get("estimated_seconds") or 3600
    lines = ["BEGIN:VEVENT",
             f"UID:{item['id']}@hoodarun",
             f"DTSTAMP:{_stamp(item.get('updated_at'))}",
             f"LAST-MODIFIED:{_stamp(item.get('updated_at'))}"]
    if item.get("time"):
        hh, mm = (int(x) for x in item["time"].split(":"))
        start = datetime(d.year, d.month, d.day, hh, mm)
        end = start + timedelta(seconds=int(est))
        lines += [f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}"]
    else:
        lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  "TRANSP:TRANSPARENT"]
    emoji = "🏋️" if item.get("sport") == "strength" else "🏃"
    lines += [f"SUMMARY:{_escape(f'{emoji} ' + item['name'])}",
              f"DESCRIPTION:{_escape(describe(item))}",
              f"CATEGORIES:{_escape(item.get('sport') or 'running')}",
              "END:VEVENT"]
    return lines


def build_calendar(workouts: Iterable[dict], name: str = "HoodaRun Training",
                   since: Optional[str] = None) -> str:
    """Render feed items as a VCALENDAR. `since` (YYYY-MM-DD) drops older items."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_escape(name)}",
             "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for item in workouts:
        if since and item["date"] < since:
            continue
        lines += _event(item)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
