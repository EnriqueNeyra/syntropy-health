"""
Published limits behind what insights say about extreme readings, each with its source. Nothing here is a diagnosis:
these only decide when to suggest talking to someone, and which tone a change gets. Change a value only against a
published source, and bump LIMITS_VERSION so a reader can tell which table an answer came from.

Sources:
- NEWS2: Royal College of Physicians, National Early Warning Score 2 (2017). A score of 3 on one measure asks for urgent
  review; 0 is the range it treats as expected. https://www.rcp.ac.uk/improving-care/resources/national-early-warning-score-news-2/
- AHA: American Heart Association, "Understanding Blood Pressure Readings": above 180 and/or above 120 mmHg, measure again
  after a few minutes, then contact a clinician straight away, or emergency services with symptoms.
  https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings
- ADA: American Diabetes Association, Standards of Care, "Glycemic Goals and Hypoglycemia": level 2 hypoglycemia is below
  54 mg/dL. https://diabetesjournals.org/care/issue/48/Supplement_1
- StayWell: The StayWell Company patient education, "Diabetes: High blood sugar" (reviewed 2025): contact your doctor right
  away if blood sugar is over 300 mg/dL. https://livinghealthy.hawaiipacifichealth.org/diabetes/management/diabetes-high-blood-sugar
- Mayo: Mayo Clinic, "Fever": adults call at 103 °F (39.4 °C) or higher. https://www.mayoclinic.org/diseases-conditions/fever/symptoms-causes/syc-20352759
- NSF: National Sleep Foundation duration recommendations (Hirshkowitz et al., Sleep Health, 2015): 7-9 hours recommended for
  adults, 11 or more not recommended.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

LIMITS_VERSION = 1


class Limit(NamedTuple):
    metric: str
    side: str               # "low" or "high"
    at: float
    inclusive: bool         # whether a reading exactly at the limit counts
    reading: str            # which of the day's figures to look at: "value" (its typical value), "min" or "max"
    own_usual_counts: bool  # a number this person has every day isn't news, where some people's normal really is there


URGENT = [
    Limit("oxygen_saturation", "low", 91, True, "value", True),         # NEWS2 scale 1: 91% or lower scores 3
    Limit("resting_heart_rate", "low", 40, True, "value", True),        # NEWS2: 40 or lower scores 3; trained people can sit there
    Limit("resting_heart_rate", "high", 131, True, "value", False),     # NEWS2: 131 or higher scores 3
    Limit("respiratory_rate", "low", 8, True, "value", True),           # NEWS2: 8 or lower scores 3
    Limit("respiratory_rate", "high", 25, True, "value", True),         # NEWS2: 25 or higher scores 3
    Limit("body_temperature", "high", 39.4, True, "max", False),        # Mayo
    Limit("body_temperature", "low", 35.0, True, "value", False),       # NEWS2: 35.0 or lower scores 3
    Limit("blood_pressure_systolic", "high", 180, False, "max", False),    # AHA
    Limit("blood_pressure_diastolic", "high", 120, False, "max", False),   # AHA
    Limit("blood_pressure_systolic", "low", 90, True, "value", True),   # NEWS2: 90 or lower scores 3
    Limit("blood_glucose", "low", 54, False, "min", False),             # ADA
    Limit("blood_glucose", "high", 300, False, "value", False),         # StayWell
]

# Outside these, a figure is a glitch or a wrong unit (a temperature in °F, a glucose in mmol/L) and says nothing about
# the person. Set wide on purpose: the broadest a living person's reading could be, not a judgement of what's healthy.
PLAUSIBLE = {
    "oxygen_saturation": (50, 100), "resting_heart_rate": (20, 250), "respiratory_rate": (3, 70),
    "body_temperature": (25, 45), "blood_pressure_systolic": (40, 300), "blood_pressure_diastolic": (20, 200),
    "blood_glucose": (15, 1500),
}


class ToneBand(NamedTuple):
    good: tuple[Optional[float], Optional[float]]     # past these, a move in the "better" direction isn't good news any more
    concern: tuple[Optional[float], Optional[float]]  # past these, it's worth mentioning to a clinician


# NEWS2's score 0 band is the "good" one, and a score of 2 or more the "concern" one, so a move that only reaches a score
# of 1 (a resting heart rate of 45, which plenty of fit people have) stays neutral. Sleep follows NSF.
TONE_BANDS = {
    "resting_heart_rate": ToneBand((51, 90), (40, 111)),
    "respiratory_rate": ToneBand((12, 20), (8, 21)),
    "oxygen_saturation": ToneBand((96, None), (93, None)),
    "sleep_duration": ToneBand((None, 9), (None, 11)),
}

# Not a clinical limit: only how far a move has to go before "good" stops being a safe thing to imply about it. A third of
# the usual value is well past what training or habits shift these measures by in a week.
LARGE_MOVE = 0.3


def tone_for_move(metric: str, usual: float, now: float, better: Optional[str]) -> tuple[str, bool]:
    """The tone for a move from ``usual`` to ``now``, and whether it's worth suggesting a clinician hears about it."""
    if better is None:
        return "info", False
    up = now > usual
    band = TONE_BANDS.get(metric)
    if band:
        low, high = band.concern
        if (low is not None and now <= low and not up) or (high is not None and now >= high and up):
            return "attention", True
    if (better == "up") != up:
        large = abs(now - usual) >= LARGE_MOVE * abs(usual)
        return "attention", bool(band and large)
    if band:
        low, high = band.good
        if (low is not None and now < low and not up) or (high is not None and now > high and up):
            return "info", False
    if abs(now - usual) >= LARGE_MOVE * abs(usual):
        return "info", False
    return "good", False


def is_plausible(metric: str, value: float) -> bool:
    low, high = PLAUSIBLE.get(metric, (float("-inf"), float("inf")))
    return low <= value <= high


def breaches(limit: Limit, value: float) -> bool:
    if limit.side == "low":
        return value <= limit.at if limit.inclusive else value < limit.at
    return value >= limit.at if limit.inclusive else value > limit.at
