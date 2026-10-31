"""Syntropy Health organizes records and doesn't give medical advice: it shows facts, and never says what a number means
for someone's health, what to do about it, or when to seek care."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from app import mcp_server
from app.main import app as fastapi_app
from app.services import ai, insights, reading_bounds
from app.store import activity, goals, profiles
from tests.test_insight_findings import _ago, _checkins, _daily, _lab, _med, _noisy, _noon, _prof, _sleep_daily, _source
from tests.test_training_load import _w

APP = Path(__file__).resolve().parent.parent / "app"

ADVICE = re.compile(
    r"\b(clinician|doctor|physician|care team|provider|emergency|911|urgent|talk to|talk it over|contact your|call your|seek|see a|"
    r"get checked|worth (mentioning|discussing|following|raising)|follow(ing)? up|go over|should|consider|recommend|you need|"
    r"try to|common approach|gradually|tends to|often makes|fitness|good for|bad for|healthy|unhealthy|dangerous|safe|risk|"
    r"diagnos\w*|injury|overtrain\w*|whoever prescribed)\b", re.I)


def _everything_a_generator_can_say(pid):
    """A record that sets off every kind of insight there is."""
    src = _source(pid)
    _lab(pid, src, 1, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", low=3.5, high=5.0, ref_text="3.5–5.0")
    _lab(pid, src, 2, _ago(100), 7.5, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    _lab(pid, src, 3, _ago(45), 6.0, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    _lab(pid, src, 4, _ago(100), 4.0, "normal", code="3016-3", title="TSH", low=0.5, high=5.0)
    _lab(pid, src, 5, _ago(10), 8.0, "high", code="3016-3", title="TSH", low=0.5, high=5.0)
    _lab(pid, src, 6, _ago(100), 190, "high", code="2093-3", title="Cholesterol", unit="mg/dL", high=150)
    _lab(pid, src, 7, _ago(12), 165, "high", code="2093-3", title="Cholesterol", unit="mg/dL", high=150)
    _lab(pid, src, 8, _ago(100), 7.0, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    _lab(pid, src, 9, _ago(11), 5.0, "normal", code="4548-4", title="A1c", unit="%", high=5.6)
    _daily(pid, "resting_heart_rate", _noisy(66, 0.8, 60) + _noisy(60, 0.8, 7), unit="bpm")
    _daily(pid, "hrv_rmssd", _noisy(50, 1.5, 60) + _noisy(25, 1.5, 7), unit="ms")
    _daily(pid, "oxygen_saturation", _noisy(97, 0.2, 60) + _noisy(90, 0.2, 7), unit="%")
    _daily(pid, "respiratory_rate", _noisy(15, 0.3, 60) + _noisy(8, 0.2, 7), unit="br/min")
    _med(pid, _source(pid, "Clinic"), "m1", _ago(45), title="Metformin 500 mg", key="med|860975")
    _daily(pid, "blood_pressure_systolic", _noisy(130, 1, 45) + _noisy(118, 1, 45, seed=8), unit="mmHg")
    activity.insert_workouts(pid, None, [_w(i, 7 + 7 * i, minutes=60) for i in range(4)] + [_w(9, 1, minutes=200)])
    rng = random.Random(3)
    hours = [rng.gauss(7, 1.5) for _ in range(53)] + [4.5, 9.5, 5, 9, 5.5, 8.5, 7]
    _sleep_daily(pid, hours)
    _checkins(pid, [max(-1, min(1, (h - 7) * 0.5 + rng.gauss(0, 0.1))) for h in hours])
    short = [i for i, h in enumerate(hours) if h < 6.5]
    activity.insert_events(pid, None, [{"id": f"h{i}", "event_type": "headache", "category": "symptoms", "name": "Headache",
                                        "start": _noon(len(hours) - 1 - i).isoformat(), "end": _noon(len(hours) - 1 - i).isoformat(),
                                        "source_name": "Syntropy"} for i in short])
    _daily(pid, "step_count", [5000, 500, 500, 500, 500, 500, 500])
    goals.set_goal(pid, "step_count", 1000)
    goals.set_goal(pid, "resting_heart_rate", 80, "max")
    return insights.insights(pid, 100)


def test_generated_insights_never_advise(client):
    found = _everything_a_generator_can_say(_prof())
    assert {i["kind"] for i in found} >= {"critical", "lab", "change", "training", "sleep", "medication", "link", "goal"}
    for i in found:
        assert not ADVICE.search(f"{i['title']} {i['text']}"), i
        assert "note" not in i["data"]
    assert found[0]["kind"] == "critical" and found[0]["text"].endswith("The lab marked this critical.")


def test_the_phrase_check_would_catch_what_used_to_be_said():
    for said in ("Talk to your care team, or call emergency services if you feel unwell.", "A change this size is worth mentioning to your clinician.",
                 "Increasing gradually is a common approach.", "A steadier schedule tends to make sleep more restful.",
                 "it's worth discussing with whoever prescribed it", "so go over it with your care team"):
        assert ADVICE.search(said), said


@pytest.mark.parametrize("path", [
    "services/insights.py", "services/ai.py", "mcp_server.py", "static/js/alerts.js", "static/js/onboarding.js", "static/js/views/ask.js",
    "static/js/views/overview.js", "static/js/views/report.js", "static/js/views/sleep-view.js", "static/js/views/trends.js",
    "static/js/views/workouts.js", "static/js/views/record-detail.js", "store/record_overrides.py", "services/ccda.py",
])
def test_no_advice_is_written_into_the_apps_text(path):
    text = (APP / path).read_text(encoding="utf-8").lower()
    for phrase in ("emergency services", "your care team", "the person's care team", "worth mentioning to", "worth discussing", "worth following",
                   "worth raising", "check with your", "talk it over", "common approach", "how fitness grows", "good for recovery",
                   "more restful", "tends to help", "a prompt to pay attention", "coach", "urgent"):
        assert phrase not in text, phrase


def test_what_a_correction_says_only_repeats_what_the_person_said():
    from app.store import record_overrides

    for words in record_overrides.WORDS.values():
        assert not ADVICE.search(words), words


def test_the_urgent_route_is_gone():
    assert "/api/insights/urgent" not in {getattr(r, "path", None) for r in fastapi_app.routes}


@pytest.mark.parametrize("snapshot", ["", "out_of_range_recent_labs: []"])
def test_the_assistant_states_the_record_and_is_not_told_to_triage(client, snapshot):
    prompt = ai.system_prompt(profiles.default_profile(), snapshot=snapshot)
    assert "state what's in the record" in prompt and "don't diagnose" in prompt and "medical advice" in prompt
    for word in ("emergency", "care team", "contacting", "coaching", "next steps", "urgent", "medications_to_check"):
        assert word not in prompt.lower(), word


def test_the_assistant_is_not_forbidden_from_mentioning_help(client):
    # A ban aimed at someone describing an emergency would be worse than saying nothing.
    prompt = ai.system_prompt(profiles.default_profile()).lower()
    for phrase in ("refuse", "never mention", "don't mention", "do not mention", "don't tell", "emergenc"):
        assert phrase not in prompt, phrase


def test_ai_apps_get_the_same_rules(client):
    assert "don't diagnose or give medical advice" in mcp_server.INSTRUCTIONS and "care team" not in mcp_server.INSTRUCTIONS
    for _, description, text in mcp_server.PROMPTS.values():
        for phrase in ("care team", "worth raising", "realistic changes", "mean, in plain language"):
            assert phrase not in f"{description} {text}", phrase


def test_medications_that_may_be_out_of_date_say_so_to_ai_apps(client):
    summary = mcp_server.t_health_summary({})
    assert "medications_may_be_out_of_date" in summary and "medications_to_check" not in summary


@pytest.mark.parametrize("path", ["static/js/views/report.js", "static/js/views/ask.js", "static/js/views/settings.js", "static/js/onboarding.js"])
def test_the_disclaimer_is_where_people_see_it(path):
    assert "medical advice" in (APP / path).read_text(encoding="utf-8")


def test_a_reading_no_living_person_could_have_is_not_a_change(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(60, 0.5, 60) + [400] * 7, unit="bpm")
    assert insights._changes(pid) == []
    assert reading_bounds.is_plausible("resting_heart_rate", 60) and not reading_bounds.is_plausible("resting_heart_rate", 400)
