"""
What a reading could possibly be, and the standard ranges behind the tone an insight takes. Nothing here says what a
reading means for someone's health. Change a range only against a published source.

Sources:
- NEWS2: Royal College of Physicians, National Early Warning Score 2 (2017): the range it scores 0.
  https://www.rcp.ac.uk/improving-care/resources/national-early-warning-score-news-2/
- NSF: National Sleep Foundation duration recommendations (Hirshkowitz et al., Sleep Health, 2015): the top of the 7-9
  hours for adults.
"""

from __future__ import annotations

from typing import Optional

# Outside these, a figure is a glitch or a wrong unit (a temperature in °F, a glucose in mmol/L) and says nothing about
# the person. Set wide on purpose: the broadest a living person's reading could be, not a judgement of what's healthy.
PLAUSIBLE = {
    "oxygen_saturation": (50, 100), "resting_heart_rate": (20, 250), "respiratory_rate": (3, 70),
    "body_temperature": (25, 45), "blood_pressure_systolic": (40, 300), "blood_pressure_diastolic": (20, 200),
    "blood_glucose": (15, 1500),
}

# A move out past these takes no colour either way: it isn't good news, and it isn't an alarm.
STANDARD_RANGES: dict[str, tuple[Optional[float], Optional[float]]] = {
    "resting_heart_rate": (51, 90),     # NEWS2, so a resting heart rate of 50 isn't called an improvement
    "respiratory_rate": (12, 20),       # NEWS2
    "oxygen_saturation": (96, None),    # NEWS2
    "sleep_duration": (None, 9),        # NSF
}

# Not a clinical limit: only how far a move has to go before any colour stops being a fair thing to imply about it. A
# third of the usual value is well past what training or habits shift these measures by in a week.
LARGE_MOVE = 0.3


def tone_for_move(metric: str, usual: float, now: float, better: Optional[str]) -> str:
    """The tone for a move from ``usual`` to ``now``. Anything extreme stays neutral."""
    if better is None or abs(now - usual) >= LARGE_MOVE * abs(usual):
        return "info"
    up = now > usual
    low, high = STANDARD_RANGES.get(metric, (None, None))
    if (low is not None and now < low and not up) or (high is not None and now > high and up):
        return "info"
    return "good" if (better == "up") == up else "attention"


def is_plausible(metric: str, value: float) -> bool:
    low, high = PLAUSIBLE.get(metric, (float("-inf"), float("inf")))
    return low <= value <= high
