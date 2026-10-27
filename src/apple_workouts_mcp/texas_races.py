"""Curated Texas road race discovery with organizer/source links.

Only organizer-confirmed event dates are included; unlike recurring rules, a
past date is never silently projected into next year. Review before travel.
Last source review: 2026-10-27.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

CHECKED_ON = "2026-10-27"
RACES = [
    {"key": "run_for_the_water", "name": "Run for the Water", "area": "Austin",
     "city": "Austin", "distances": ["5K", "10 mile"], "date": "2026-11-08",
     "url": "https://www.gazellefoundation.org/run-for-the-water",
     "notes": "Competitive 5K and hilly 10-miler; Gazelle Foundation fundraiser."},
    {"key": "thundercloud_trot", "name": "ThunderCloud Subs Turkey Trot",
     "area": "Austin", "city": "Austin", "distances": ["5 mile"], "date": "2026-11-26",
     "url": "https://thundercloud.com/turkeytrot/",
     "notes": "Thanksgiving 5-miler based at the Long Center."},
    {"key": "decker", "name": "Decker Challenge", "area": "Austin", "city": "Austin",
     "distances": ["5K", "half marathon"], "date": "2026-12-13",
     "url": "https://austinrunners.org/distance-challenge/",
     "notes": "December race; half-marathon course is a useful hill-strength test."},
    {"key": "austin_international_half", "name": "Spurs Austin International Half",
     "area": "Austin", "city": "Austin", "distances": ["half marathon"],
     "date": "2027-01-17", "url": "https://downhilltodowntown.com/",
     "notes": "Formerly the 3M Half Marathon; net downhill into downtown Austin."},
    {"key": "austin_marathon", "name": "Ascension Seton Austin Marathon weekend",
     "area": "Austin", "city": "Austin",
     "distances": ["5K", "half marathon", "marathon"], "date": "2027-02-14",
     "url": "https://youraustinmarathon.com/schedule/",
     "notes": "Downtown Austin; hilly road marathon; confirmed 2027 weekend."},
    {"key": "cap10k", "name": "Statesman Capitol 10,000", "area": "Austin",
     "city": "Austin", "distances": ["10K"], "date": "2027-04-11",
     "url": "https://www.cap10k.com/",
     "notes": "Major Austin spring road race; organizer says details subject to change."},
    {"key": "dallas_trot", "name": "Dallas YMCA Turkey Trot", "area": "Dallas",
     "city": "Dallas", "distances": ["5K", "8 mile"], "date": "2026-11-26",
     "url": "https://ymcadallas.org/turkeytrot",
     "notes": "Thanksgiving morning race in downtown Dallas."},
    {"key": "dallas_marathon", "name": "BMW Dallas Marathon weekend", "area": "Dallas",
     "city": "Dallas", "distances": ["half marathon", "marathon"],
     "date": "2026-12-13", "url": "https://dallasmarathon.com/",
     "notes": "December downtown Dallas race weekend; verify exact event-day distances."},
    {"key": "cowtown_short", "name": "The Cowtown 5K & 10K", "area": "Dallas",
     "city": "Fort Worth", "distances": ["5K", "10K"], "date": "2027-02-27",
     "url": "https://cowtownmarathon.org/races/",
     "notes": "Fort Worth/DFW; short-distance race day is Saturday."},
    {"key": "cowtown", "name": "The Cowtown Half & Marathon", "area": "Dallas",
     "city": "Fort Worth", "distances": ["half marathon", "marathon", "ultra"],
     "date": "2027-02-28", "url": "https://cowtownmarathon.org/races/",
     "notes": "Fort Worth/DFW; half/full/ultra races on Sunday."},
    {"key": "we_are_houston_5k", "name": "We Are Houston 5K", "area": "Houston",
     "city": "Houston", "distances": ["5K"], "date": "2027-01-16",
     "url": "https://www.chevronhoustonmarathon.com/registration/",
     "notes": "Saturday warm-up race before the Houston Marathon weekend."},
    {"key": "aramco_half", "name": "Aramco Houston Half Marathon", "area": "Houston",
     "city": "Houston", "distances": ["half marathon"], "date": "2027-01-17",
     "url": "https://www.chevronhoustonmarathon.com/registration/",
     "notes": "Fast Houston road half; verify entry availability before booking."},
    {"key": "houston_marathon", "name": "Chevron Houston Marathon", "area": "Houston",
     "city": "Houston", "distances": ["marathon"], "date": "2027-01-17",
     "url": "https://www.chevronhoustonmarathon.com/registration/",
     "notes": "Flat major Texas marathon in January."},
    {"key": "run_houston_series", "name": "Run Houston Race Series", "area": "Houston",
     "city": "Houston metro", "distances": ["5K", "10K", "half marathon"],
     "date": None, "url": "https://txruns.com/runhoustonraceseries",
     "notes": "Multiple 2027 events; individual dates vary, check organizer calendar."},
]


def texas_race_calendar(area: Optional[str] = None,
                        distance: Optional[str] = None,
                        after: Optional[str] = None,
                        include_unscheduled: bool = True) -> dict:
    """Return confirmed future races plus optionally undated race series.

    Date-based results use currently published dates, not guessed recurrences.
    The 'area' Dallas filter includes Fort Worth/DFW.
    """
    if area and area.strip().lower() not in ("austin", "dallas", "houston"):
        raise ValueError("area must be Austin, Dallas, Houston or omitted")
    from_date = date.fromisoformat(after) if after else date.today()
    requested_area = area.strip().lower() if area else None
    q = distance.strip().lower() if distance else None
    rows = []
    for item in RACES:
        if requested_area and item["area"].lower() != requested_area:
            continue
        if q and not any(q == d.lower() for d in item["distances"]):
            continue
        if item["date"]:
            if date.fromisoformat(item["date"]) < from_date:
                continue
        elif not include_unscheduled:
            continue
        rows.append(dict(item, date_status="organizer_published"
                         if item["date"] else "check_organizer"))
    rows.sort(key=lambda row: (row["date"] is None, row["date"] or "", row["name"]))
    return {
        "checked_on": CHECKED_ON, "after": from_date.isoformat(),
        "area": area, "distance": distance, "count": len(rows), "races": rows,
        "caveat": "Static curated calendar, not a live event feed. Dates and offerings "
                  "may change; open each race's official site before registration "
                  "or travel. Old events are omitted, never rolled forward.",
    }
