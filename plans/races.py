"""Marathon calendar: pick a race — any race, any distance out — and get a date
the plan generator can build backwards from.

Dates here are **computed from a scheduling rule**, not hard-coded from a list
someone typed in 2025. Boston is the third Monday in April because Massachusetts
law says Patriots' Day is; Berlin is the last Sunday in September because it has
been for decades. That means this module stays roughly right as years roll over
instead of quietly going stale — but "roughly right" is the honest claim.

Every entry carries a `certainty`:

  * ``"statutory"`` — fixed by law or by a holiday (Boston, Marine Corps).
  * ``"established"`` — the same weekend every year for many years (Berlin, NYC).
  * ``"typical"``     — the usual weekend, but the organiser moves it (London,
    Tokyo, Paris).

**Always confirm the real date with the organiser before entering.** The MCP
tools surface `certainty` and this caveat with every result, and any tool that
builds a plan accepts an explicit `race_date` that overrides the computed one.

Custom races are first-class: `custom_race("Some Local Marathon", "2027-04-18")`
gets exactly the same treatment as a major.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@dataclass(frozen=True)
class Race:
    key: str
    name: str
    location: str
    month: int
    weekday: int
    nth: int                      # 1..4 = nth weekday of month, -1 = last
    certainty: str                # statutory | established | typical
    notes: str = ""

    def occurrence(self, year: int) -> date:
        return _nth_weekday(year, self.month, self.weekday, self.nth)

    def next_after(self, after: date) -> date:
        d = self.occurrence(after.year)
        return d if d > after else self.occurrence(after.year + 1)

    def as_dict(self, after: Optional[date] = None) -> dict:
        after = after or date.today()
        when = self.next_after(after)
        return {
            "key": self.key,
            "name": self.name,
            "location": self.location,
            "date": when.isoformat(),
            "weeks_out": (when - after).days // 7,
            "date_certainty": self.certainty,
            "notes": self.notes,
            "caveat": "Date computed from the race's usual scheduling rule — "
                      "confirm with the organiser before you enter or book.",
        }


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    if nth == -1:
        nxt = date(year + (month == 12), (month % 12) + 1, 1)
        last = nxt - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


# --------------------------------------------------------------------------- #
# the catalogue
# --------------------------------------------------------------------------- #
CATALOG: list[Race] = [
    Race("boston", "Boston Marathon", "Boston, MA, USA", 4, MON, 3, "statutory",
         "Patriots' Day. Qualifying standards apply; net-downhill point-to-point "
         "course that eats quads — the plan adds downhill-specific long runs."),
    Race("london", "TCS London Marathon", "London, UK", 4, SUN, 4, "typical",
         "Ballot entry. Flat and fast; usually cool."),
    Race("berlin", "BMW Berlin Marathon", "Berlin, Germany", 9, SUN, -1, "established",
         "The world-record course. Flat, cool, and the reason autumn goal races exist."),
    Race("chicago", "Bank of America Chicago Marathon", "Chicago, IL, USA", 10, SUN, 2,
         "established", "Flat, fast, big crowds. Weather is the variable."),
    Race("nyc", "TCS New York City Marathon", "New York, NY, USA", 11, SUN, 1,
         "established", "Five boroughs, bridges, and a rolling second half. Not a PR course."),
    Race("tokyo", "Tokyo Marathon", "Tokyo, Japan", 3, SUN, 1, "typical",
         "Lottery entry. Flat and cool; a winter build in the northern hemisphere."),
    Race("houston", "Chevron Houston Marathon", "Houston, TX, USA", 1, SUN, 3, "established",
         "Flat, cool, famously fast. Mid-January means the build runs through a Gulf "
         "Coast autumn — expect a heat allowance on early paces."),
    Race("marine_corps", "Marine Corps Marathon", "Washington, DC, USA", 10, SUN, -1,
         "established", "No qualifying time. Rolling first half through DC."),
    Race("cim", "California International Marathon", "Sacramento, CA, USA", 12, SUN, 1,
         "established", "Net downhill, December, a classic BQ-hunting course."),
    Race("twin_cities", "Twin Cities Marathon", "Minneapolis-St Paul, MN, USA", 10, SUN, 1,
         "established", "Scenic and rolling, with a climb in the last 6 miles."),
    Race("philadelphia", "Philadelphia Marathon", "Philadelphia, PA, USA", 11, SUN, 3,
         "established", "Flat, cold, and an easy one to get into."),
    Race("grandmas", "Grandma's Marathon", "Duluth, MN, USA", 6, SAT, 3, "established",
         "Point-to-point along Lake Superior. Summer date, so the build runs in spring."),
    Race("big_sur", "Big Sur International Marathon", "Big Sur, CA, USA", 4, SUN, -1,
         "typical", "Hilly and spectacular. Run this one for the course, not the clock."),
    Race("paris", "Schneider Electric Marathon de Paris", "Paris, France", 4, SUN, 2,
         "typical", "Flat, large, early April."),
    Race("valencia", "Valencia Marathon", "Valencia, Spain", 12, SUN, 1, "established",
         "Arguably the fastest big-city course in the world."),
    Race("amsterdam", "TCS Amsterdam Marathon", "Amsterdam, Netherlands", 10, SUN, 3,
         "typical", "Flat, fast, finishes in the Olympic Stadium."),
    Race("sydney", "Sydney Marathon", "Sydney, Australia", 8, SUN, -1, "typical",
         "Southern-hemisphere winter build; a northern-hemisphere summer one."),
    Race("austin", "Austin Marathon", "Austin, TX, USA", 2, SUN, 3, "established",
         "Hilly for a February road marathon. Strength work earns its keep here."),
    Race("dallas", "BMW Dallas Marathon", "Dallas, TX, USA", 12, SUN, 2, "established",
         "December, rolling, and a reasonable drive from most of Texas."),
    Race("boulderthon", "Boulderthon", "Boulder, CO, USA", 10, SUN, 2, "typical",
         "Altitude. Adjust goal pace, not effort — roughly 2-4% slower at 5,300ft "
         "unless you arrive acclimatised."),
]

BY_KEY = {r.key: r for r in CATALOG}


# --------------------------------------------------------------------------- #
# lookup / selection
# --------------------------------------------------------------------------- #
def list_races(min_weeks_out: int = 0, max_weeks_out: int = 104,
               after: Optional[date] = None) -> list[dict]:
    """Every catalogued race whose next running falls in the window."""
    after = after or date.today()
    out = []
    for r in CATALOG:
        row = r.as_dict(after)
        if min_weeks_out <= row["weeks_out"] <= max_weeks_out:
            out.append(row)
    return sorted(out, key=lambda r: r["date"])


def find_race(query: str, after: Optional[date] = None) -> Optional[dict]:
    """Match a race by key, or by a fragment of its name or location."""
    after = after or date.today()
    q = query.strip().lower()
    if q in BY_KEY:
        return BY_KEY[q].as_dict(after)
    for r in CATALOG:
        if q in r.name.lower() or q in r.location.lower() or q in r.key:
            return r.as_dict(after)
    return None


def random_race(min_weeks_out: int = 16, max_weeks_out: int = 104,
                after: Optional[date] = None, seed: Optional[int] = None) -> Optional[dict]:
    """Pick a race at random from anywhere in the window — the 'surprise me,
    any distance out' path. The default floor of 16 weeks keeps the draw to
    races there is still time to train properly for."""
    pool = list_races(min_weeks_out, max_weeks_out, after)
    if not pool:
        return None
    return random.Random(seed).choice(pool)


def custom_race(name: str, race_date: str, location: str = "",
                notes: str = "") -> dict:
    """A race that isn't in the catalogue. Same shape, no guessing."""
    when = date.fromisoformat(race_date)
    return {
        "key": "custom",
        "name": name,
        "location": location,
        "date": when.isoformat(),
        "weeks_out": (when - date.today()).days // 7,
        "date_certainty": "user_supplied",
        "notes": notes,
        "caveat": "Date supplied by you.",
    }
