"""What a record's own view adds: a medication's history, its renewals, and the kinds of resource with no fields of their own here."""

from __future__ import annotations

from app.connectors import fhir_normalize as fn
from app.services import exports, sync
from app.store import profiles, records


def _order(rid, when, status="active"):
    return {"resourceType": "MedicationRequest", "id": rid, "status": status, "intent": "order", "authoredOn": when,
            "medicationCodeableConcept": {"coding": [{"system": fn.RXNORM, "code": "197361"}], "text": "Amlodipine"}}


def _pid():
    return profiles.list_profiles()[0]["id"]


def test_a_medication_says_when_its_orders_began_and_last_came(client):
    pid = _pid()
    sync.import_fhir_resources(pid, "Clinic", [_order("m1", "2023-01-05"), _order("m2", "2024-02-06"), _order("m3", "2025-03-07")])
    newest = records.list_records(pid, "medications")["items"][0]
    body = client.get(f"/api/records/{newest['id']}").json()
    assert body["history"] == {"first": "2023-01-05", "last_ordered": "2025-03-07", "orders": 3, "reported_taking": False}


def test_one_order_has_no_history_to_add(client):
    pid = _pid()
    sync.import_fhir_resources(pid, "Clinic", [_order("m1", "2023-01-05")])
    newest = records.list_records(pid, "medications")["items"][0]
    assert client.get(f"/api/records/{newest['id']}").json()["history"] is None


def test_renewals_from_the_same_source_are_shown_and_other_sources_stay_where_they_were(client):
    pid = _pid()
    sync.import_fhir_resources(pid, "Clinic", [_order("m1", "2023-01-05"), _order("m2", "2024-02-06")])
    sync.import_fhir_resources(pid, "Hospital", [_order("h1", "2022-01-01")])
    newest = next(r for r in records.list_records(pid, "medications", dedupe=False)["items"] if r["resource_id"] == "m2")
    body = client.get(f"/api/records/{newest['id']}").json()
    assert [r["effective_at"] for r in body["renewals"]] == ["2023-01-05"]
    assert [r["effective_at"] for r in body["duplicates"]] == ["2022-01-01"]


FAMILY = {"resourceType": "FamilyMemberHistory", "id": "f1", "status": "completed", "date": "2024-03-01", "relationship": {"text": "Mother"},
          "text": {"div": "<div>Mother: heart disease</div>"}}


def test_a_kind_of_resource_with_no_fields_here_is_kept_and_can_be_read(client):
    pid = _pid()
    res = sync.import_fhir_resources(pid, "File", [FAMILY, {"resourceType": "ServiceRequest", "id": "s1", "status": "active", "authoredOn": "2025-01-01",
                                                            "code": {"text": "Referral to cardiology"}}])
    assert res["inserted"] == 2 and not res["not_read"]
    rows = {r["resource_type"]: r for r in records.list_records(pid, "other")["items"]}
    assert rows["FamilyMemberHistory"]["title"] == "Family member history: Mother"
    assert rows["FamilyMemberHistory"]["effective_at"] == "2024-03-01" and "heart disease" in rows["FamilyMemberHistory"]["narrative"]
    assert rows["ServiceRequest"]["title"] == "Service request: Referral to cardiology"
    body = client.get(f"/api/records/{rows['FamilyMemberHistory']['id']}").json()
    assert body["raw"]["relationship"]["text"] == "Mother"
    assert any(c["key"] == "other" and c["count"] == 2 for c in client.get(f"/api/categories?profile={pid}").json()["categories"])
    assert "FamilyMemberHistory" in b"".join(exports.bundle_stream(pid)).decode()


def test_what_isnt_about_the_person_is_still_left_out():
    outcome = {"resourceType": "OperationOutcome", "id": "o", "issue": []}
    medication = {"resourceType": "Medication", "id": "m", "code": {"text": "X"}}
    junk = {"resourceType": "not a type", "id": "j"}
    _, recs, problems = fn.normalize_report([outcome, medication, junk, {"resourceType": "Provenance", "id": "p"}])
    assert not recs and not problems["skipped"] and not problems["failed"]


def test_one_the_source_took_back_is_not_kept():
    _, recs, _ = fn.normalize_report([dict(FAMILY, status="entered-in-error")])
    assert not recs
