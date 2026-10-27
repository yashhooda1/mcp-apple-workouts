"""Simple plant-forward fuel plans and consolidated grocery lists.

No shopping account access, prices, calorie budgets, or meal-order automation.
Recipes favor minimal preparation for an adult endurance runner.
"""
from __future__ import annotations

from collections import Counter
from typing import Literal

Diet = Literal["vegan", "lacto_vegetarian", "vegetarian"]
Intensity = Literal["moderate", "high", "race_week"]

# Groceries are normalized to shopping units rather than false-precision macros.
MEALS = {
    "oats": {
        "name": "Overnight oats + banana + chia", "minutes": 5,
        "items": ["rolled oats", "bananas", "chia seeds", "fortified soy milk"],
        "prep": "Mix oats and milk in a jar the night before.",
    },
    "toast": {
        "name": "Peanut butter banana toast", "minutes": 5,
        "items": ["whole grain bread", "peanut butter", "bananas"],
        "prep": "Toast bread and slice banana.",
    },
    "smoothie": {
        "name": "Berry banana protein smoothie", "minutes": 5,
        "items": ["frozen berries", "bananas", "fortified soy milk", "soy yogurt"],
        "prep": "Blend everything; add oats for longer-run days.",
    },
    "burrito": {
        "name": "Black bean rice burrito bowl", "minutes": 12,
        "items": ["microwave rice", "black beans", "frozen peppers", "salsa", "avocados"],
        "prep": "Microwave rice and peppers, rinse beans, add salsa.",
    },
    "pasta": {
        "name": "Lentil pasta with marinara and spinach", "minutes": 15,
        "items": ["lentil pasta", "jarred marinara", "baby spinach", "olive oil"],
        "prep": "Boil pasta, heat sauce, wilt spinach.",
    },
    "wrap": {
        "name": "Hummus chickpea veggie wrap", "minutes": 8,
        "items": ["tortillas", "hummus", "canned chickpeas", "baby spinach", "cucumbers"],
        "prep": "Rinse chickpeas and roll in tortilla.",
    },
    "tofu": {
        "name": "Tofu stir-fry and instant rice", "minutes": 15,
        "items": ["firm tofu", "frozen stir-fry vegetables", "microwave rice", "soy sauce"],
        "prep": "Pan-sear tofu, heat vegetables, serve over rice.",
    },
    "potato": {
        "name": "Microwave potato + chili beans", "minutes": 12,
        "items": ["potatoes", "canned kidney beans", "canned tomatoes", "frozen corn"],
        "prep": "Microwave potato; heat beans, tomatoes and corn.",
    },
    "cereal": {
        "name": "Fortified cereal, milk and fruit", "minutes": 3,
        "items": ["fortified cereal", "fortified soy milk", "oranges"],
        "prep": "Pour and eat; add toast if hungry.",
    },
    "bagel": {
        "name": "Bagel, peanut butter and berries", "minutes": 5,
        "items": ["bagels", "peanut butter", "frozen berries"],
        "prep": "Toast bagel; thaw berries.",
    },
    "quesadilla": {
        "name": "Bean and spinach quesadilla", "minutes": 10,
        "items": ["tortillas", "black beans", "baby spinach", "salsa", "vegan cheese"],
        "prep": "Fill tortilla, pan-toast, serve with salsa.",
    },
}
SNACKS = [
    ("Apple + trail mix", ["apples", "trail mix"]),
    ("Hummus + pretzels", ["hummus", "pretzels"]),
    ("Soy yogurt + granola", ["soy yogurt", "granola"]),
    ("Banana + peanut butter", ["bananas", "peanut butter"]),
    ("Toast + jam", ["whole grain bread", "jam"]),
    ("Orange + roasted chickpeas", ["oranges", "roasted chickpeas"]),
    ("Smoothie", ["frozen berries", "fortified soy milk"]),
]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def meal_plan(diet: Diet = "lacto_vegetarian",
              training_load: Intensity = "high",
              servings: int = 1,
              long_run_day: str = "Sunday") -> dict:
    """Return seven repeatable menus and a deduplicated shopping list.

    Grocery quantities are pragmatic shopping estimates for whole packages,
    not a nutrition-analysis claim. Use individual requirements and hunger to
    adjust portions; no weight loss/restriction guidance.
    """
    if diet not in ("vegan", "lacto_vegetarian", "vegetarian"):
        raise ValueError("diet must be vegan, lacto_vegetarian or vegetarian")
    if training_load not in ("moderate", "high", "race_week"):
        raise ValueError("training_load must be moderate, high or race_week")
    if not 1 <= servings <= 8:
        raise ValueError("servings must be 1..8")
    long_run = long_run_day.strip().capitalize()
    if long_run not in DAY_NAMES:
        raise ValueError("long_run_day must be a weekday")

    breakfast = ["oats", "toast", "smoothie", "oats", "bagel", "cereal", "toast"]
    lunch = ["burrito", "wrap", "burrito", "tofu", "wrap", "potato", "pasta"]
    dinner = ["pasta", "tofu", "quesadilla", "burrito", "pasta", "tofu", "potato"]
    groceries: Counter[str] = Counter()
    days = []
    for i, day in enumerate(DAY_NAMES):
        slots = [MEALS[breakfast[i]], MEALS[lunch[i]], MEALS[dinner[i]]]
        snack, snack_ingredients = SNACKS[i]
        for meal in slots:
            groceries.update(meal["items"])
        groceries.update(snack_ingredients)
        day_plan = {
            "day": day,
            "breakfast": slots[0]["name"], "lunch": slots[1]["name"],
            "dinner": slots[2]["name"], "snack": snack,
            "max_active_cook_minutes": max(m["minutes"] for m in slots),
        }
        if day == long_run:
            day_plan["run_fueling"] = (
                "Use familiar carbohydrate-rich food before the run. For runs "
                "longer than ~90 minutes, practice roughly 30-60g carbohydrate/hour "
                "and replace fluids according to conditions and sweat losses."
            )
        days.append(day_plan)

    # Existing recipes start vegan. Swap in familiar dairy options only when
    # preferred, never silently add egg or dairy to a vegan diet.
    swaps = (
        ["Swap soy yogurt for dairy Greek yogurt if preferred.",
         "Add paneer or cheese to burrito bowls and quesadillas if desired."]
        if diet != "vegan" else
        ["Check fortified plant milk for calcium, vitamin D and B12.",
         "Use tofu, lentils, beans and soy yogurt for distributed protein."]
    )
    if diet != "vegan":
        groceries["plain Greek yogurt"] += 2
        groceries["paneer or shredded cheese"] += 2
    categories = {
        "produce": {"bananas", "avocados", "baby spinach", "cucumbers", "apples",
                    "oranges", "potatoes"},
        "refrigerated": {"firm tofu", "hummus", "soy yogurt", "fortified soy milk",
                        "plain Greek yogurt", "paneer or shredded cheese", "vegan cheese"},
        "frozen": {"frozen berries", "frozen peppers", "frozen stir-fry vegetables",
                   "frozen corn"},
    }
    shopping: dict[str, list[dict]] = {"produce": [], "refrigerated": [],
                                      "frozen": [], "pantry": []}
    for item in sorted(groceries):
        category = next((name for name, group in categories.items() if item in group),
                        "pantry")
        shopping[category].append({
            "item": item, "used_in_meals": groceries[item],
            "shopping_note": f"Buy for {servings} serving(s) per meal; choose package size "
                             "and portions based on appetite.",
        })
    return {
        "diet": diet, "training_load": training_load, "servings_per_meal": servings,
        "days": days, "shopping_list": shopping, "swaps": swaps,
        "prep_once": [
            "Wash produce; refrigerate cut vegetables safely.",
            "Stock cooked rice packs, canned beans and frozen vegetables.",
            "Prep two overnight-oat jars; freeze extra portions of cooked meals.",
            "Keep bagels, pretzels and fruit accessible around long workouts.",
        ],
        "nutrition": {
            "principle": "Fuel training rather than restricting food. Adjust portions to "
                         "training duration, appetite and individual energy needs.",
            "protein": "Include beans, tofu, soy foods, lentils, dairy if chosen, or "
                       "other protein at each meal.",
            "race_week": "Prioritize familiar lower-fiber choices close to racing; "
                         "avoid experimenting on race morning.",
            "micronutrients": "Plant-based runners should plan reliable B12 sources; "
                              "also consider iron, calcium and vitamin D. Ask a "
                              "qualified professional about persistent fatigue.",
            "hydration": "Drink to thirst for routine runs; plan fluids/electrolytes "
                         "for long/hot sessions and avoid excessive water intake.",
        },
        "caution": "Meal ideas and grocery quantities are not individualized medical "
                   "advice or a validated nutrition prescription. No items have been "
                   "purchased or added to a retailer cart.",
    }
