"""What a reading could possibly be, so a glitch or a wrong unit isn't taken for a change in the person. Nothing here says
what a reading means for someone's health."""

from __future__ import annotations

# Outside these, a figure is a glitch or a wrong unit (a temperature in °F, a glucose in mmol/L) and says nothing about
# the person. Set wide on purpose: the broadest a living person's reading could be, not a judgement of what's healthy.
PLAUSIBLE = {
    "oxygen_saturation": (50, 100), "resting_heart_rate": (20, 250), "respiratory_rate": (3, 70),
    "body_temperature": (25, 45), "blood_pressure_systolic": (40, 300), "blood_pressure_diastolic": (20, 200),
    "blood_glucose": (15, 1500),
}


def is_plausible(metric: str, value: float) -> bool:
    low, high = PLAUSIBLE.get(metric, (float("-inf"), float("inf")))
    return low <= value <= high
