"""Fuelling, hydration and recovery guidance, attached to the plan itself.

Two delivery paths, because a PDF nobody opens is not a nutrition plan:

  1. **Per session** — `fuel_note()` returns a one-line note that plan builders
     append to `WorkoutSpec.description`. HoodaRun shows the description with the
     scheduled workout on the iPhone and Watch, so the fuelling cue is right next
     to the workout it belongs to.
  2. **Per week** — `weekly_brief()` returns a structured block that the MCP
     tools hand back to the assistant alongside the schedule, so "what should I
     eat this week" is answerable from the plan instead of from the internet.

Two audiences, deliberately different:

  * `level="adult"` — ranges from mainstream sports-nutrition practice
    (carbohydrate per hour, protein per kg, the post-run window).
  * `level="hs"` — high-school athletes are minors and still growing. This
    module therefore emits **adequacy-only** guidance for them: eat enough, eat
    often, eat before and after. No calorie targets, no macro maths, no
    body-composition or weight language of any kind, and an explicit pointer to
    a parent, coach, or registered dietitian. Under-fuelling is the single most
    common way a teenage distance runner gets hurt, and a training tool that
    even implies restriction to a 15-year-old is doing harm. If you extend this
    module, keep that invariant.

None of this is medical or dietetic advice. It is general guidance of the kind a
coach gives a squad; individual needs vary, and anyone with a medical condition,
a history of disordered eating, or specific dietary requirements should be
working with a clinician or a registered dietitian rather than a Python module.
"""
from __future__ import annotations

from typing import Optional

ADULT = "adult"
HS = "hs"

DISCLAIMER = (
    "General guidance, not medical or dietetic advice — individual needs vary."
)
HS_DISCLAIMER = (
    "General guidance for a growing athlete: the goal is always eating ENOUGH. "
    "Run any questions past a parent, your coach, or a registered dietitian."
)


# --------------------------------------------------------------------------- #
# per-session notes (land in the Garmin workout description)
# --------------------------------------------------------------------------- #
def fuel_note(kind: str, minutes: float, level: str = ADULT) -> str:
    """One-line fuelling cue for a single session.

    kind: 'easy' | 'quality' | 'long' | 'race' | 'strength'
    minutes: approximate session duration.
    """
    if level == HS:
        return _fuel_note_hs(kind, minutes)
    return _fuel_note_adult(kind, minutes)


def _fuel_note_adult(kind: str, minutes: float) -> str:
    if kind == "strength":
        return ("Fuel: protein + carbs within ~60 min of finishing. Lifting on empty "
                "blunts the adaptation you just paid for.")
    if kind == "easy" and minutes <= 60:
        return "Fuel: no special prep needed. Water to thirst; eat normally after."
    if kind == "quality":
        return ("Fuel: 30-60g carbs 1-2h before. Sip fluid between reps. "
                "Carbs + ~20-30g protein within 60 min after.")
    if kind == "long":
        if minutes >= 90:
            return (f"Fuel: pre-run carbs 1-2h out. From 45 min on, 30-60g carbs/hour "
                    f"(~{max(1, int(minutes // 45))} gel-equivalents this run) and "
                    f"400-800ml fluid/hour with sodium. Practise race fuel today — "
                    f"the gut trains like everything else.")
        return "Fuel: pre-run carbs, fluid to thirst, carbs + protein within 60 min after."
    if kind == "race":
        return ("Fuel: nothing new today. Rehearsed breakfast 2-3h out, rehearsed "
                "carbs/hour, rehearsed fluid. Eat within 30-60 min of finishing.")
    return "Fuel: eat normally around this one; fluid to thirst."


def _fuel_note_hs(kind: str, minutes: float) -> str:
    if kind == "strength":
        return ("Fuel: a real snack with carbs + protein after lifting — chocolate milk, "
                "a sandwich, yoghurt and fruit. Don't skip it.")
    if kind == "race":
        return ("Fuel: nothing new on race day. The breakfast you've practised, 2-3h out. "
                "Sip water through the day. Eat properly after — you've earned it.")
    if kind == "long" or minutes >= 75:
        return ("Fuel: eat a carb snack an hour before. Carry water. Real meal within an "
                "hour after — long runs need refuelling, not skipped meals.")
    if kind == "quality":
        return ("Fuel: snack 60-90 min before practice so you're not running on an empty "
                "tank. Carbs + protein after.")
    return "Fuel: eat normally, drink water through the school day, don't skip breakfast."


def recovery_note(hard: bool, level: str = ADULT) -> str:
    if level == HS:
        return ("Recovery: 8-10 hours of sleep is training. Easy days stay easy; "
                "tell your coach if something hurts for more than a couple of days."
                if hard else
                "Recovery: easy means easy. Sleep is where the improvement happens.")
    return ("Recovery: prioritise sleep tonight (7-9h). Legs up, eat, hydrate. "
            "Tomorrow's easy run is genuinely easy."
            if hard else
            "Recovery: keep this one conversational. If it feels hard, it's too fast.")


def annotate(description: Optional[str], kind: str, minutes: float,
             level: str = ADULT, hard: bool = False) -> str:
    """Append fuelling + recovery lines to an existing workout description."""
    parts = [p for p in (description, fuel_note(kind, minutes, level),
                         recovery_note(hard, level)) if p]
    return " | ".join(parts)


# --------------------------------------------------------------------------- #
# per-week brief (returned by the MCP tools, not scheduled)
# --------------------------------------------------------------------------- #
def weekly_brief(phase: str, weekly_miles: float, level: str = ADULT,
                 down_week: bool = False, race_week: bool = False) -> dict:
    """Structured nutrition / hydration / recovery block for one training week."""
    if level == HS:
        return _weekly_brief_hs(phase, weekly_miles, down_week, race_week)
    return _weekly_brief_adult(phase, weekly_miles, down_week, race_week)


def _weekly_brief_adult(phase: str, weekly_miles: float, down_week: bool,
                        race_week: bool) -> dict:
    load = "race" if race_week else ("down" if down_week else phase)
    carbs = {
        "base": "5-7 g/kg bodyweight/day, weighted toward training days",
        "build": "6-8 g/kg/day; the day before a long run sits at the top of that range",
        "specific": "6-9 g/kg/day — marathon-pace work is a carbohydrate-limited session",
        "sharpen": "6-8 g/kg/day; quality is short and sharp, so timing matters more than volume",
        "taper": "keep carbs up even as mileage drops — this is how the tank fills",
        "down": "no need to cut back; a down week is recovery, and recovery is built from food",
        "race": "top up through the last 2-3 days; nothing new, nothing exotic",
    }.get(load, "5-7 g/kg/day")

    return {
        "disclaimer": DISCLAIMER,
        "phase": load,
        "weekly_miles": round(weekly_miles, 1),
        "nutrition": {
            "carbohydrate": carbs,
            "protein": "1.6-2.0 g/kg/day, spread across meals — supports the lifting too",
            "fat": "roughly 20-35% of intake; don't cut it to make room for carbs",
            "timing": "carbs before quality and long runs; carbs + 20-30g protein "
                      "within ~60 min after every hard session and both lifts",
            "iron_note": "Distance runners, and especially menstruating athletes, are a "
                         "high-risk group for low ferritin. If easy pace drifts and HR "
                         "climbs for no reason, ask a doctor for a ferritin test rather "
                         "than self-supplementing.",
        },
        "hydration": {
            "daily": "pale-straw urine is the practical check; add sodium in Houston-grade heat",
            "long_runs": "400-800 ml/hour with 300-600 mg sodium/litre; rehearse race-day fluid",
            "after": "replace ~125-150% of fluid lost over the following 2-4 hours",
        },
        "recovery": {
            "sleep": "7-9h nightly; sleep is the highest-yield recovery intervention there is",
            "easy_days": "run them slow enough that the hard days can be hard",
            "strength": "two sessions this week, both on quality days — never the day "
                        "before a long run",
            "load_watch": "flag a 10%+ jump in resting HR or a multi-day drop in HRV as a "
                          "reason to make an easy day a rest day",
            "pain_rule": "pain that changes your gait, or lasts past 48h, is a "
                         "physio conversation, not a taper-through problem",
        },
    }


def _weekly_brief_hs(phase: str, weekly_miles: float, down_week: bool,
                     race_week: bool) -> dict:
    return {
        "disclaimer": HS_DISCLAIMER,
        "phase": "race" if race_week else ("down" if down_week else phase),
        "weekly_miles": round(weekly_miles, 1),
        "nutrition": {
            "principle": "Eat enough, and eat often. A training week on top of a school day "
                         "needs more food than a rest week, not less.",
            "breakfast": "Never skip it. Practice is 6-8 hours after breakfast for most "
                         "school schedules, so it has to actually count.",
            "before_practice": "Carb snack 60-90 min out: banana, granola bar, toast, "
                               "pretzels, a bagel.",
            "after_practice": "Carbs + protein within about an hour: chocolate milk, a "
                              "sandwich, rice and chicken, yoghurt and fruit.",
            "through_the_day": "Three meals plus two or three snacks is normal for a "
                               "training high schooler.",
            "iron_and_calcium": "Growing runners need iron and calcium — meat, beans, "
                                "fortified cereal, dairy or fortified alternatives. "
                                "Persistent fatigue is worth a doctor's visit, not a "
                                "supplement guess.",
            "what_this_plan_will_not_do": "This plan gives no calorie targets, no macro "
                                          "maths and no weight guidance. Under-eating is "
                                          "the fastest route to stress fractures, illness "
                                          "and a season lost. If food or weight feels "
                                          "stressful, talk to a parent, your coach, or a "
                                          "doctor — that conversation matters more than "
                                          "any workout in here.",
        },
        "hydration": {
            "daily": "Carry a bottle through the school day; most of the day's drinking "
                     "should happen before practice, not during.",
            "practice": "Drink at every break. In heat, take a bottle to the track.",
            "after": "Keep drinking after practice — thirst lags behind what you lost.",
        },
        "recovery": {
            "sleep": "8-10 hours. This is the single biggest performance lever a high "
                     "schooler has, and it beats every workout in this plan.",
            "easy_days": "Easy days are supposed to feel slow. Racing the warm-up is how "
                         "athletes get hurt.",
            "strength": "Two short sessions this week — bodyweight and dumbbells. Form "
                        "before load, always, and a coach watching the first few sessions.",
            "rest_day": "At least one full day off every week, and more during exam weeks.",
            "pain_rule": "Bone pain in the shin, foot or hip that hurts when you hop on "
                         "one leg is a stop-running-and-see-someone signal, not a "
                         "run-through-it one.",
            "growth_note": "Growth spurts temporarily wreck coordination and raise injury "
                           "risk. Mileage holds steady through one; it doesn't climb.",
        },
    }
