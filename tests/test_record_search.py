"""Searching clinical records: every word, anywhere in a record, with the same meaning with or without the index."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app import mcp_server
from app.core import config, data_fixes
from app.core.db import db
from app.store import profiles, records, search
from tests.conftest import connect_institution
from tests.test_accounts import _join, _person, _signed_in_owner


def _build_index():
    data_fixes.run_pending()
    assert search.ready()


def _add(record_id, title, *, narrative=None, details=None, category="notes", profile=None, code=None, value=None, connection="con_s"):
    profile = profile or profiles.default_profile()["id"]
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, metadata_json) "
                     "VALUES (?, ?, 'ehr', 'epic', 'Mercy Hospital', 'live', 'active', 1, 1, '{}')", (connection, profile))
    records.upsert_records(profile, connection, "Mercy Hospital", [{
        "resource_type": "DocumentReference", "resource_id": record_id, "category": category, "title": title, "narrative": narrative,
        "details": details or {}, "code": code, "value_text": value, "effective_at": "2026-01-01"}])
    return records.record_id(connection, "DocumentReference", record_id)


def _titles(query, profile=None, **kw):
    return sorted(r["title"] for r in records.list_records(profile or profiles.default_profile()["id"], q=query, **kw)["items"])


@pytest.fixture(params=["index", "no index"])
def searchable(request, client, monkeypatch):
    """Both ways of searching have to mean the same thing."""
    if request.param == "index":
        _build_index()
    else:
        monkeypatch.setattr(search, "ready", lambda: False)
    _add("a", "Essential hypertension", narrative="Blood pressure well controlled on lisinopril.")
    _add("b", "Discharge summary", details={"full_text": "Patient was seen after a fall. Follow up with orthopedics in two weeks.",
                                            "authors": ["Dr. María Núñez"]})
    _add("c", "Hemoglobin A1c", category="labs", code="4548-4", value="6.1 %")
    _add("d", "Lisinopril 10 mg tablet", category="medications", details={"prescriber": "Dr. Gregory House"})
    _add("e", "Imaging report", details={"attachments": [{"url": "https://example.org/secretword/1", "title": "scan"}],
                                         "result_refs": ["Observation/hiddenref"]})
    return client


def test_every_word_in_any_order(searchable):
    assert _titles("hypertension essential") == ["Essential hypertension"]
    assert _titles("essential hypertension") == ["Essential hypertension"]
    assert _titles("blood lisinopril") == ["Essential hypertension"]
    assert _titles("hypertension lisinopril") == ["Essential hypertension"]
    assert _titles("hypertension orthopedics") == []


def test_finds_note_text_past_the_preview(searchable):
    assert _titles("orthopedics") == ["Discharge summary"]


def test_finds_clinicians(searchable):
    assert _titles("gregory house") == ["Lisinopril 10 mg tablet"]
    assert _titles("house") == ["Lisinopril 10 mg tablet"]


def test_codes_and_values(searchable):
    assert _titles("4548-4") == ["Hemoglobin A1c"]
    assert _titles("6.1") == ["Hemoglobin A1c"]


def test_case_plurals_and_small_words(searchable):
    assert _titles("HYPERTENSION") == ["Essential hypertension"]
    assert _titles("tablets") == ["Lisinopril 10 mg tablet"]
    assert _titles("the discharge summary of the fall") == ["Discharge summary"]


def test_links_and_references_arent_searched(searchable):
    assert _titles("secretword") == []
    assert _titles("hiddenref") == []
    assert _titles("attachments") == []


def test_punctuation_cant_break_a_search(searchable):
    for query in ('"', "a AND", "NOT", "title:x", "(", "*", "---", "NEAR(a b)", "o'brien", "50%", "a_b", '"; DROP TABLE x;--'):
        records.list_records(profiles.default_profile()["id"], q=query)
    assert _titles("---") == []


def test_test_names_a_lab_titled_differently(searchable):
    assert _titles("HbA1c") == ["Hemoglobin A1c"]


def test_only_this_persons_records(client, searchable):
    other = profiles.create_profile("Sam", relationship="child")["id"]
    _add("sam", "Hypertension clinic visit", profile=other, connection="con_sam")
    assert _titles("hypertension") == ["Essential hypertension"]
    assert _titles("hypertension", profile=other) == ["Hypertension clinic visit"]


def test_accents_dont_have_to_match_with_the_index(client):
    _build_index()
    _add("n", "Visit", details={"authors": ["Dr. María Núñez"]})
    assert _titles("maria nunez") == ["Visit"]
    assert _titles("María") == ["Visit"]


def test_the_records_page_and_timeline_search_the_same_way(client):
    _build_index()
    _add("z", "Discharge summary", details={"full_text": "Follow up with orthopedics."})
    assert client.get("/api/records", params={"q": "orthopedics discharge"}).json()["total"] == 1
    profile = profiles.default_profile()["id"]
    assert [r["title"] for r in records.timeline(profile, q="follow orthopedics")] == ["Discharge summary"]


# ---------------------------------------------------------------------------
# Keeping the index right
# ---------------------------------------------------------------------------

def _documents():
    conn = sqlite3.connect(str(config.db_path()))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {search._TABLE}").fetchone()[0], conn.execute("SELECT COUNT(*) FROM record_search_ids").fetchone()[0]
    finally:
        conn.close()


def test_records_from_before_the_index_are_found_once_it_is_built(client):
    data_fixes.stop()
    with db() as conn:
        conn.execute("DELETE FROM data_fixes")
    existing = sqlite3.connect(str(config.db_path()))
    for t in search._TRIGGERS:
        existing.execute(f"DROP TRIGGER IF EXISTS {t}")
    existing.execute(f"DROP TABLE IF EXISTS {search._TABLE}")
    existing.commit()
    existing.close()
    for i in range(30):
        _add(f"old{i}", f"Old note {i}", details={"full_text": f"Written by Dr. Oldtimer, number{i}"})
    # Searching works before the index is there.
    assert not search.ready() and len(_titles("oldtimer")) == 30
    data_fixes._stop.clear()
    _build_index()
    assert len(_titles("oldtimer")) == 30 and _titles("number7 oldtimer") == ["Old note 7"]
    assert _documents() == (30, 30)


def test_building_resumes_where_it_stopped(client):
    profile = profiles.default_profile()["id"]
    with db() as conn:
        search._create_index(conn)
    for i in range(10):
        _add(f"re{i}", f"Resume {i}", profile=profile)
    with db() as conn:
        # As if it stopped partway: these were never indexed.
        conn.execute(f"DELETE FROM {search._TABLE}")
        conn.execute("DELETE FROM record_search_ids")
        cursor = search._build(conn, "", 4)         # not None: carrying on, so nothing is reset
        assert cursor
        again = search._build(conn, "", 4)          # the same step twice changes nothing
        assert again == cursor
        while cursor:
            cursor = search._build(conn, cursor, 4)
    assert _documents() == (10, 10)


def test_writes_keep_the_index_right(client):
    _build_index()
    rid = _add("w", "First title", details={"full_text": "alphaword"})
    assert _titles("alphaword") == ["First title"]
    assert _documents() == (1, 1)
    _add("w", "Second title", details={"full_text": "betaword"})
    assert _titles("alphaword") == [] and _titles("betaword") == ["Second title"]
    assert _documents() == (1, 1)
    records.delete_record(rid)
    assert _titles("betaword") == [] and _documents() == (0, 0)


def test_text_is_gone_from_the_index_when_a_source_or_person_is_deleted(raw_client):
    client = _signed_in_owner(raw_client)
    _build_index()
    assert client.put("/api/settings/developer", json={"enabled": True}).status_code == 200
    assert client.put("/api/settings/platforms/epic", json={"mode": "simulated"}).status_code == 200
    linked = connect_institution(client, "epic-stanford-health-care")
    assert _documents()[0] > 0
    # A foreign key cascade takes the records with it, and a trigger has to see that.
    with db() as conn:
        conn.execute("DELETE FROM connections WHERE id = ?", (linked["connection_id"],))
    assert _documents() == (0, 0)

    sam = _person(client, "Sam", "child")
    _add("sam", "Note about Sam", profile=sam, connection="con_sam")
    assert _documents() == (1, 1)
    assert client.delete(f"/api/profiles/{sam}").status_code == 200
    assert _documents() == (0, 0)


def test_deleting_all_of_someones_data_empties_their_part(client):
    _build_index()
    profile = profiles.default_profile()["id"]
    _add("p", "Private note", profile=profile)
    assert _documents() == (1, 1)
    assert client.delete(f"/api/profiles/{profile}/data").status_code == 200
    assert _documents() == (0, 0)


def test_a_bad_details_value_cant_stop_a_record_being_written(client):
    _build_index()
    profile = profiles.default_profile()["id"]
    with db() as conn:
        conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, metadata_json) "
                     "VALUES ('con_b', ?, 'ehr', 'epic', 'B', 'live', 'active', 1, 1, '{}')", (profile,))
        conn.execute("INSERT INTO clinical_records(id, profile_id, connection_id, resource_type, resource_id, category, title, details_json, "
                     "first_seen_at, updated_at) VALUES ('bad', ?, 'con_b', 'X', 'x', 'notes', 'Bad details', 'not json', 1, 1)", (profile,))
    assert _titles("bad details") == ["Bad details"]


def test_a_database_moved_to_a_build_without_fts5_keeps_working(client, monkeypatch):
    _build_index()
    _add("m", "Moved note")
    with monkeypatch.context() as without:
        without.setattr(search, "fts5_available", lambda: False)
        search.at_start_up()
        conn = sqlite3.connect(str(config.db_path()))
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'record_search%'").fetchall()
        conn.close()
        assert not search.ready()
        assert _titles("moved") == ["Moved note"]
        _add("m2", "Moved again")
        assert _titles("moved again") == ["Moved again"]

    # Back on a build that has it, everything is indexed again, what came in between included.
    search.at_start_up()
    _build_index()
    assert _titles("moved") == ["Moved again", "Moved note"]
    assert _documents() == (2, 2)


def test_a_build_that_gains_fts5_starts_using_it(client, monkeypatch):
    with monkeypatch.context() as without:
        without.setattr(search, "fts5_available", lambda: False)
        with db() as conn:
            conn.execute("DELETE FROM data_fixes")
        for trigger in search._TRIGGERS:
            with db() as conn:
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        data_fixes.run_pending()
        assert not search.ready()
    search.at_start_up()
    _add("g", "Gained index")
    _build_index()
    assert _titles("gained") == ["Gained index"]


def test_a_build_without_fts5_finishes_the_fix_and_searches_anyway(client, monkeypatch):
    monkeypatch.setattr(search, "fts5_available", lambda: False)
    with db() as conn:
        assert search._build(conn, None, 10) is None
    _add("x", "Plain search")
    assert _titles("search plain") == ["Plain search"]


def test_the_index_isnt_read_through_someone_elses_id_after_a_move(client):
    # The index says which records match; who may see them comes from the record itself.
    _build_index()
    first = profiles.default_profile()["id"]
    other = profiles.create_profile("Sam", relationship="child")["id"]
    rid = _add("moved", "Moved record", profile=first)
    with db() as conn:
        conn.execute("UPDATE clinical_records SET profile_id = ? WHERE id = ?", (other, rid))
    assert _titles("moved record", profile=first) == []
    assert _titles("moved record", profile=other) == ["Moved record"]


# ---------------------------------------------------------------------------
# Through the tool
# ---------------------------------------------------------------------------

def _tool(**arguments):
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_records", "arguments": arguments}})
    return json.loads(reply["result"]["content"][0]["text"])


def test_results_show_where_the_words_are(client):
    _build_index()
    _add("s", "Discharge summary", narrative="n" * 700, details={"full_text": "A long report. " * 200 + "Referred to orthopedics for the knee."})
    row = _tool(query="orthopedics")["records"][0]
    assert "orthopedics" in row["match"] and "narrative" in row and "orthopedics" not in row["narrative"]


def test_no_match_on_several_words_says_which_words_were_there(client):
    _build_index()
    _add("s", "Blood pressure check")
    result = _tool(query="blood pressure medication")
    assert result["total_matches"] == 0
    assert result["matches_per_word"] == {"blood": 1, "pressure": 1, "medication": 0} and result["hint"]


def test_a_search_for_someone_elses_note_finds_nothing(raw_client):
    client = _signed_in_owner(raw_client)
    _build_index()
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    _add("mine", "Note with oncology word", profile=profiles.default_profile()["id"])
    token = maria_client.post("/api/agents/tokens", json={"name": "Claude"}).json()["token"]
    r = maria_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "search_records", "arguments": {"query": "oncology"}}},
                          headers={"Authorization": f"Bearer {token}"})
    assert json.loads(r.json()["result"]["content"][0]["text"])["total_matches"] == 0


# ---------------------------------------------------------------------------
# A word inside a longer word, and text that isn't quite words
# ---------------------------------------------------------------------------

def test_a_word_inside_a_longer_word_is_found(searchable):
    _add("p", "Prediabetes", category="conditions")
    _add("h", "Hyperlipidemia", category="conditions")
    _add("s", "Atorvastatin 20 MG Oral Tablet", category="medications")
    _add("l", "HbA1c-like thing", category="labs")
    assert _titles("diabetes") == ["Prediabetes"]
    assert _titles("lipidemia") == ["Hyperlipidemia"]
    assert _titles("statin") == ["Atorvastatin 20 MG Oral Tablet"]
    assert _titles("statins") == ["Atorvastatin 20 MG Oral Tablet"]
    assert _titles("a1c-like") == ["HbA1c-like thing"]
    assert _titles("tension lisinopril") == ["Essential hypertension"]


def test_results_dont_change_when_the_index_finishes(client, monkeypatch):
    _add("p", "Prediabetes", narrative="Discussed metformin and hemoglobin", category="conditions")
    _add("m", "O'Brien follow-up", details={"authors": ["Dr. Smith-Jones"]})
    _add("c", "Blood count", code="718-7", value="13.2 g/dL", category="labs")
    queries = ("diabetes", "formin", "globin met", "brien", "o'brien", "smith-jones", "ith-jon", "718-7", "18-7", "3.2", "nothing here")
    with monkeypatch.context() as scan:
        scan.setattr(search, "ready", lambda: False)
        expected = {q: _titles(q) for q in queries}
    _build_index()
    assert {q: _titles(q) for q in queries} == expected
    assert expected["diabetes"] == ["Prediabetes"] and expected["ith-jon"] == ["O'Brien follow-up"]


def test_a_real_looking_chart_answers_the_same_with_and_without_the_index(client, monkeypatch):
    connect_institution(client, "epic-stanford-health-care")
    _build_index()
    person = profiles.default_profile()["id"]
    conditions = [r["title"] for r in records.list_records(person, "conditions", limit=100)["items"]]
    assert "Prediabetes" in conditions and "Hyperlipidemia" in conditions

    def answer(query):
        return json.loads(mcp_server.call_tool("search_records", {"query": query, "limit": 100})[0])["total_matches"]
    queries = ("diabetes", "lipidemia", "statin", "glycemia", "tension", "pressure", "metformin", "a1c", "nothingatall")
    indexed = {q: answer(q) for q in queries}
    monkeypatch.setattr(search, "ready", lambda: False)
    assert {q: answer(q) for q in queries} == indexed
    assert indexed["diabetes"] and indexed["lipidemia"] and not indexed["nothingatall"]


def test_a_word_inside_a_longer_word_shows_where_it_matched(client):
    _build_index()
    _add("p", "Visit", narrative="Long history of prediabetes managed with diet", category="notes")
    found = search.snippets("diabetes", [records.record_id("con_s", "DocumentReference", "p")])
    assert "prediabetes" in next(iter(found.values()))


def test_punctuation_around_a_word_doesnt_hide_it(searchable):
    assert _titles("hypertension?") == ["Essential hypertension"]
    assert _titles("(hypertension)") == ["Essential hypertension"]
    assert _titles("essential, hypertension.") == ["Essential hypertension"]


def test_accents_inside_a_longer_word(client):
    _build_index()
    _add("n", "Visit", details={"authors": ["Dr. Peñaloza"]})
    assert _titles("peñal") == ["Visit"]
    assert _titles("naloza") == ["Visit"]


def test_a_word_inside_too_many_longer_words_still_gets_its_answer(client, monkeypatch):
    _build_index()
    _add("t", "Visit", narrative="abcde abcdf abcdg abcdh")
    _add("u", "Other")
    monkeypatch.setattr(search, "MAX_TERMS", 2)
    assert _titles("abcd") == ["Visit"]
    assert search.snippets("abcd", [records.record_id("con_s", "DocumentReference", "t")]) == {}


def test_a_lab_name_still_counts_when_no_text_holds_the_word(client):
    _build_index()
    _add("c", "Glycated hemoglobin", category="labs", code="4548-4", value="6.1 %")
    assert _titles("HbA1c") == ["Glycated hemoglobin"]


@pytest.mark.parametrize("query", ["a\x00b", "\x00", "hypertension\x00", "essential\x00hypertension"])
def test_a_nul_character_cant_break_a_search(searchable, query):
    client = searchable
    assert client.get("/api/records", params={"q": query}).status_code == 200
    assert client.get("/api/timeline", params={"q": query}).status_code == 200


def test_a_nul_character_separates_words(searchable):
    assert _titles("essential\x00hypertension") == ["Essential hypertension"]


def test_a_nul_character_over_mcp_isnt_called_busy(client):
    _build_index()
    _add("a", "Essential hypertension")
    text, failed = mcp_server.call_tool("search_records", {"query": "a\x00hypertension"})
    assert failed is False and json.loads(text)["total_matches"] == 1


def test_only_a_really_busy_database_is_called_busy(client, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError('fts5: syntax error near ""')
    monkeypatch.setattr(records, "list_records", broken)
    text, failed = mcp_server.call_tool("search_records", {"query": "x"})
    assert failed and json.loads(text)["code"] == "internal_error"

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(records, "list_records", locked)
    text, failed = mcp_server.call_tool("search_records", {"query": "x"})
    assert failed and json.loads(text)["code"] == "busy"

