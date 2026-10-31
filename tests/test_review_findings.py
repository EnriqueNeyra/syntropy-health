"""What the last review of the merged branch found: each of these failed before its fix."""

from __future__ import annotations

import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path

from app.core import config, db, maintenance
from app.services import imports, sync
from app.store import records
from tests.test_ccda_import import CCD


def _lab(value, low, high, day):
    return {"category": "labs", "unit": "%", "unit_norm": "%", "value_num": value, "value_text": None, "effective_at": f"{day}T00:00:00Z",
            "id": f"r{day}", "ref_low": low, "ref_high": high, "details": {}, "interpretation": None}


def test_a_trend_range_is_one_labs_range():
    series = records._unit_series([_lab(5.4, 4.0, 5.6, "2024-01-01"), _lab(5.5, None, 5.7, "2024-06-01")])
    assert (series["ref_low"], series["ref_high"]) == (None, 5.7)
    series = records._unit_series([_lab(5.4, 4.0, 5.6, "2024-01-01"), _lab(5.5, None, None, "2024-06-01")])
    assert (series["ref_low"], series["ref_high"]) == (4.0, 5.6)


def _person(client, name, born=None):
    return client.post("/api/profiles", json={"name": name, "relationship": "child", "birth_date": born, "sex": "female"}).json()["id"]


def _import(client, pid):
    res = client.post("/api/imports/fhir", files={"file": ("ccd.xml", CCD, "text/xml")}, data={"profile_id": pid})
    assert res.status_code == 200, res.text
    return res.json()


def test_a_file_born_on_another_day_than_the_person_is_called_out(client):
    pid = _person(client, "Maya", "2015-05-05")
    result = _import(client, pid)
    assert any("Testa Example (born Feb 3, 1970)" in w and "Maya (born May 5, 2015)" in w for w in result["warnings"])
    conflict = client.get("/api/report", params={"profile": pid}).json()["identity_conflict"]
    assert [p["full_name"] for p in conflict] == ["Testa Example", "Maya"]
    assert all(p["sources"] for p in conflict)      # the bell lists where each one comes from


def test_a_file_that_agrees_or_a_person_with_no_birth_date_is_not(client):
    for born in ("1970-02-03", None):
        pid = _person(client, f"Testa {born}", born)
        assert not any("more than one person" in w for w in _import(client, pid).get("warnings", []))
        assert client.get("/api/report", params={"profile": pid}).json()["identity_conflict"] == []


def test_a_less_exact_birth_date_is_not_a_different_person(client):
    pid = _person(client, "Testa Example", "1970-02-03")
    for said in ("1970", "1970-02", "1970-02-03T00:00:00Z"):
        assert records.conflicting_people(pid, [{"full_name": "Testa Example", "birth_date": said, "sources": ["Clinic"]}]) == []
    assert records.conflicting_people(pid, [{"full_name": "Testa Example", "birth_date": "1971-02-03", "sources": ["Clinic"]}])
    same = [{"source_name": s, "full_name": "Testa Example", "birth_date": b} for s, b in (("A", "1970-02-03"), ("B", "1970"), ("C", "1970-02-03T00:00:00Z"))]
    assert len(records.distinct_people(same)) == 1
    assert len(records.distinct_people([*same, {"source_name": "D", "full_name": "Testa Example", "birth_date": "1971-02-03"}])) == 2


def _condition(name):
    return {"resourceType": "Condition", "id": name, "code": {"text": f"{name} Patientconfidentialnote"}, "clinicalStatus": {"coding": [{"code": "active"}]}}


def test_folding_older_imports_keeps_search_and_files_right(client):
    pid = _person(client, "Fold")
    older = sync.import_fhir_resources(pid, "Older", [_condition("only-old"), _condition("both")])
    newer = sync.import_fhir_resources(pid, "Newer", [_condition("both")])
    with db.db() as conn:
        for c in (older, newer):
            conn.execute("UPDATE connections SET kind = 'import', provider = 'apple_health_records' WHERE id = ?", (c["connection_id"],))
        conn.execute("UPDATE connections SET created_at = created_at - 100 WHERE id = ?", (older["connection_id"],))
        conn.execute("INSERT INTO record_files (id, profile_id, connection_id, resource_type, resource_id, content_type, size, state, stored_at) "
                     "VALUES ('f1', ?, ?, 'Condition', 'only-old', 'application/pdf', 1, 'kept', 1)", (pid, older["connection_id"]))
        cursor = None
        for _ in range(5):
            cursor = imports._fold_apple_health_records(conn, cursor, 100)
            if cursor is None:
                break
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM connections WHERE id = ?", (older["connection_id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM clinical_records WHERE profile_id = ?", (pid,)).fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM clinical_records WHERE id NOT IN (SELECT record_id FROM record_search_ids)").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM record_search_ids WHERE record_id NOT IN (SELECT id FROM clinical_records)").fetchone()[0] == 0
        assert conn.execute("SELECT connection_id FROM record_files WHERE id = 'f1'").fetchone()[0] == newer["connection_id"]


def test_a_deleted_persons_words_leave_the_search_index(client):
    stays = _person(client, "Stays")
    goes = _person(client, "Goes")
    sync.import_fhir_resources(stays, "Keeps", [{**_condition("kept"), "code": {"text": "Ordinary"}}])
    sync.import_fhir_resources(goes, "Source", [_condition("gone")])
    assert client.delete(f"/api/profiles/{goes}").status_code == 200
    maintenance.reclaim(config.db_path())
    db.close_readers()
    conn = sqlite3.connect(config.db_path())
    blocks = b"".join(bytes(r[0]) for r in conn.execute("SELECT block FROM record_search_fts_data WHERE block IS NOT NULL"))
    conn.close()
    assert b"patientconfidentialnote" not in blocks and b"ordinary" in blocks
    assert b"patientconfidentialnote" not in config.db_path().read_bytes()


def _wait(job_id):
    for _ in range(200):
        job = imports.get_job(job_id)
        if job["finished_at"]:
            return job
        time.sleep(0.05)
    raise AssertionError("import didn't finish")


def test_an_archive_that_would_fill_the_memory_is_refused(client):
    pid = _person(client, "Bomb")
    for part in ("fhir.json", "samples.ndjson", "goals.json"):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({"format": "syntropy-person-archive", "version": 1}))
            z.writestr(part, b" " * (16 * 1024 * 1024))
        res = client.post("/api/imports/archive", files={"file": ("a.zip", out.getvalue(), "application/zip")}, data={"profile_id": pid})
        assert res.status_code == 200, res.text
        job = _wait(res.json()["job_id"])
        assert job["status"] != "completed" and "too large" in json.dumps(job), job


def test_a_persons_own_archive_of_repeated_letters_comes_back(client):
    """Years of reminders from one template compress at over 200 to 1. The app made this archive, so it has to read it."""
    pid = _person(client, "Letters")
    letter = "Your appointment reminder from the clinic. " * 450
    sync.import_fhir_resources(pid, "Clinic", [
        {"resourceType": "DocumentReference", "id": f"letter-{i}", "status": "current", "description": letter, "date": f"20{10 + i // 12}-{i % 12 + 1:02d}-01"}
        for i in range(144)])
    data = client.get("/api/export/archive", params={"profile": pid}).content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("fhir.json")
        assert info.file_size > info.compress_size * 200       # past the limit a stranger's Apple Health file gets
    other = _person(client, "Letters again")
    res = client.post("/api/imports/archive", files={"file": ("a.zip", data, "application/zip")}, data={"profile_id": other})
    assert res.status_code == 200, res.text
    job = _wait(res.json()["job_id"])
    assert job["status"] == "completed", job


def test_what_was_asked_about_stays_out_of_the_access_log():
    from app.main import _SECRET_PARAMS

    line = "/api/records?profile=prf_1&note_type=psychiatry&author=Dr+Sensitive&title=HIV-1+viral+load&category=labs&measure=x&a=1&b=2&type=y&metric=z"
    cleaned = _SECRET_PARAMS.sub(r"\1REDACTED", line)
    assert "profile=prf_1" in cleaned
    for word in ("psychiatry", "Sensitive", "HIV", "labs"):
        assert word not in cleaned
    assert _SECRET_PARAMS.sub(r"\1REDACTED", "/x?data=keep") == "/x?data=keep"


def test_the_docker_smoke_test_can_take_the_backup_it_checks_for():
    """docker exec without -it has no terminal: the backup would stop to ask for a passphrase, on a server with nothing to back up."""
    root = Path(__file__).parent.parent
    smoke = (root / "scripts" / "docker_smoke.sh").read_text()
    assert smoke.index("/api/setup") < smoke.index("syntropy-health backup /data/backups --no-encrypt")
    assert "docker exec -it syntropy-health syntropy-health backup" in (root / "README.md").read_text()


def test_a_flag_with_no_range_on_record_is_worded_as_the_labs_flag(client):
    from app.services import insights
    from tests.test_insight_findings import _ago, _lab, _prof, _source

    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 96, "normal", code="13457-7", title="LDL", unit="mg/dL")
    _lab(pid, src, 2, _ago(5), 106, "high", code="13457-7", title="LDL", unit="mg/dL")
    words = " ".join(n["text"] for n in insights._lab_notes(pid))
    assert "flagged high by the lab" in words and "reference range" not in words
