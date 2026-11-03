"""scripts/refresh_directory.py with made-up vendor lists (no network), and the weekly workflow that runs it."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import refresh_directory as refresh
from scripts.refresh_directory import clean_url, collapse_duplicates, is_listing, merge, readable_name, tidy, tidy_location

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "directory.yml").read_text()
NEXTGEN = "https://fhir.nextgen.com/nge/prod/fhir-api-r4/fhir/r4"


@pytest.mark.parametrize("name, url", [
    ("TESTemr5new", "https://fhir4.healow.com/fhir/r4/CDABDD"),
    ("TestPracticeELE01", NEXTGEN),
    ("Testtest", NEXTGEN),
    ("Test8080Erika Chavez", "https://fhir4.healow.com/fhir/r4/HBHHCD"),
    ("TestFacilityPiyush Dudhe", "https://fhir4.healow.com/fhir/r4/EGHJDD"),
    ("TestAPLA Health Wellness Center - Baldwin Hills", "https://fhir4.healow.com/fhir/r4/FDECCD"),
    ("Testkelvinlocal", "https://fhir4.healow.com/fhir/r4/FDJHCD"),
    ("Advocare LLCTEST1", "https://fhir4.healow.com/fhir/r4/FEDEBD"),
    ("Faiz NiazTEST Facility, MD", "https://fhir4.healow.com/fhir/r4/AFIFAA"),
    ("Vocational Instruction Project Community ServicesTest", "https://fhir4.healow.com/fhir/r4/ADHBCD"),
    ("Wellmed EMR - TEST2", "https://fhir4.healow.com/fhir/r4/DCHFAD"),
    ("TrainUnity Health Care Inc", "https://fhir4.healow.com/fhir/r4/DFDBCD"),
    ("QA Beth Israel", "https://fhir4.healow.com/fhir/r4/FGJBBA"),
    ("QA - Community Health Centers,Inc", "https://fhir4.healow.com/fhir/r4/CEEFBD"),
    ("Medhost General Hospital - Demo01", "https://fhir.yourcareuniverse.net/tenant/47b7af45"),
    ("ASP41 POD3", "https://fhir4.healow.com/fhir/r4/CBAFDD"),
    ("ASP43AZPOD18AMI", "https://fhir4.healow.com/fhir/r4/JDDBED"),
    ("ECWINDQAPOD1 RTREDE", "https://fhir4.healow.com/fhir/r4/JJJDCD"),
    ("RM POD ASP41AZPOD11 A-MI", "https://fhir4.healow.com/fhir/r4/IAGCED"),
    ("admin", "https://ophthpod05.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("POD01 Admin", "https://pod01admin.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Efpod01 Admin", "https://efpod01admin.ef.prod.fhir.ema-api.com/fhir/r4/"),
    ("Pod02", "https://pod02prod.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("ophthpod1", "https://ophthpod01.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("efpod03test04", "https://efpod03test04.ef.prod.fhir.ema-api.com/fhir/r4/"),
    ("Bellingham ENT", "https://bellinghamentsandbox.mmi.prod.fhir.ema-api.com/fhir/r4/"),       # a real name on a sandbox
    ("Louisiana Gastroenterology Associates, LLC", "https://lagastro-test.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Schweiger Dermatology Group", "https://testapi-sdg.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Default Practice", NEXTGEN),
    ("Default Practice 0002", NEXTGEN),
    ("Model Practice", NEXTGEN),
    ("ZDefault Practice Setup", NEXTGEN),
    ("ASC Payer Default Practice", NEXTGEN),
    ("PPTesting_1", NEXTGEN),
    ("RD59UD1_TestPractice", NEXTGEN),
    ("Prodtest", NEXTGEN),
    ("PFInternalTestUseOnly - Danny Wise", "https://api.patientfusion.com/fhir/r4/v1/1"),
    ("Veradigm_Test_Practice", "https://api.patientfusion.com/fhir/r4/v1/2"),
    ("MyEyeDr Encompass Staging", "https://myeyedrencstg.ef.prod.fhir.ema-api.com/fhir/r4/"),
    ("My Eye Dr QA2", "https://myeyedr-qa2.ef.prod.fhir.ema-api.com/fhir/r4/"),
    ("AdvancedMD SFTP", "https://adpdemo-sftp.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Z Retired Open MRI INC", NEXTGEN),
    ("zzzJordan and Associates Office", "https://zzzjordan.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("PRITHIVIRAJ DHARMARAJA, MD INACTIVE", "https://a1.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Internal SAM Environment", "https://fhir4.healow.com/fhir/r4/AAAAAC"),
    ("Internal - Labcorp", "https://fhir4.healow.com/fhir/r4/AAAAAD"),
    ("MSSQL-CPCI Environment SP3", "https://fhir4.healow.com/fhir/r4/AAAAAE"),
    ("MySQL - eCQMs 10e", "https://fhir4.healow.com/fhir/r4/AAAAAF"),
    ("Sales-ecwintl", "https://fhir4.healow.com/fhir/r4/AAAAAG"),
    ("Prod testing EMR - MSSQL", "https://fhir4.healow.com/fhir/r4/AAAAAH"),
    ("APU testing", "https://fhir4.healow.com/fhir/r4/AAAAAI"),
    ("Ambient Listening Testing", "https://a2.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("DEV-Family HealthCare Network", "https://fhir4.healow.com/fhir/r4/AAAAAJ"),
    ("Dry Run 2", NEXTGEN),
    ("WAVE 5 Dry Run", NEXTGEN),
    ("Enterprise Chart Family Practice", NEXTGEN),
    ("G2 - TIN A", NEXTGEN),
    ("Alpha Environment Practice 2", NEXTGEN),
    ("Alpha Enterprise Practice 1_1", NEXTGEN),
    ("NextGen Cardiology Practice", NEXTGEN),
    ("NextGen CHC Practice", NEXTGEN),
    ("NextGen UGM Presenters DB", NEXTGEN),
    ("FHIR Medical Practice", "https://a3.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Practice 4", NEXTGEN),
    ("Master Practice", NEXTGEN),
    ("CHC Practice", NEXTGEN),
    ("Optical Practice 1", NEXTGEN),
    ("Train-TALKIATRY MANAGEMENT SERVICES, LLC", "https://fhir4.healow.com/fhir/r4/AAAAAQ"),
    ("TrainUnity Health Care Inc", "https://fhir4.healow.com/fhir/r4/AAAAAR"),
    ("Train - Family Care", "https://fhir4.healow.com/fhir/r4/AAAAAS"),
    ("Z RetiredOsceola Radiology Associates PA", NEXTGEN),
    ("Z RetireGrand Itasca Clinic And Hospital", NEXTGEN),
    ("Z RetirNew Haven Radiology Associates PC", NEXTGEN),
    ("Z RetiWillowbend Diagnostic Imaging PLLC", NEXTGEN),
    ("Z RetEast Bay Medical Imaging Center LLC", NEXTGEN),
    ("Z ReSouthern Connecticut Imaging Centers", NEXTGEN),
    ("Advocare LLCTEST1", "https://fhir4.healow.com/fhir/r4/AAAAAT"),
    ("Faiz NiazTEST Facility, MD", "https://fhir4.healow.com/fhir/r4/AAAAAU"),
    ("HIEMRDEMOTEST Facility", "https://fhir4.healow.com/fhir/r4/AAAAAV"),
    ("TESTQA-Humana, Inc - Enterprise", NEXTGEN),
    ("TESTemr5new", NEXTGEN),
    ("TESTFAMILY HEALTHCARE", "https://fhir4.healow.com/fhir/r4/AAAAAW"),
    ("TESTPRACTICE 1", "https://fhir4.healow.com/fhir/r4/AAAAAX"),
    ("TEST1 CLINIC", "https://fhir4.healow.com/fhir/r4/AAAAAY"),
    ("TEST FAMILY MEDICINE", "https://fhir4.healow.com/fhir/r4/AAAAAZ"),
    ("TestFamily Healthcare", "https://fhir4.healow.com/fhir/r4/AAAABA"),
])
def test_test_systems_and_placeholders_are_not_listed(name, url):
    assert not is_listing(name, url)


@pytest.mark.parametrize("name, url", [
    ("Mark Testa, DO", "https://fhir4.healow.com/fhir/r4/AEFIBD"),
    ("DeBenedictis Testa DPM PA", "https://unioncitypodiatry.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Thomas O Connor Md, Pa Dba Testosteronology", "https://fhir4.healow.com/fhir/r4/AGCCDD"),
    ("NEUROTESTING GROUP, S.C", "https://fhir4.healow.com/fhir/r4/DEHDDD"),
    ("Whitestone Pediatrics", "https://fhir4.healow.com/fhir/r4/GECEBA"),
    ("Qazi Medical Group Inc - NAMM", "https://fhir4.healow.com/fhir/r4/CGJHBA"),
    ("Scituate Podiatry Group, Inc.", "https://fhir-myrecord.cerner.com/r4/sbZmqu"),
    ("Pontchartrain Orthopedics & Sports Medicine", "https://pbjc.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("American Medical Administrators, Inc", "https://fhir4.healow.com/fhir/r4/AFCFBD"),
    ("STOF Health Administration", NEXTGEN),
    ("Train Family Clinic", "https://fhir4.healow.com/fhir/r4/AAAAAA"),
    ("Greatest Care Clinic", "https://greatest.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Mass General Brigham", "https://ws-interconnect-fhir.partners.org/Interconnect-FHIR-MU-PRD/api/FHIR/R4"),
    ("EF Podiatry", "https://efpodiatry.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Ad Podiatry Associates", "https://adpodiatry.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Pod Family Medicine", "https://fhir4.healow.com/fhir/r4/AAAAAB"),
    ("Academic Dermatology", "https://academodermatology.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("John K. Wildemore, MD, LLC", "https://drwildemore.mmi.prod.fhir.ema-api.com/fhir/r4/"),
    ("Internal Medicine Associates", "https://fhir4.healow.com/fhir/r4/AAAAAK"),
    ("Dev Medical Association", "https://fhir4.healow.com/fhir/r4/AAAAAL"),
    ("Alpha Medical Center - TX", "https://fhir4.healow.com/fhir/r4/AAAAAM"),
    ("Allergy Testing Center", "https://fhir4.healow.com/fhir/r4/AAAAAN"),
    ("Sales Family Medicine", "https://fhir4.healow.com/fhir/r4/AAAAAO"),
    ("MedStar Health Hosted Environment", "https://fhir-myrecord.cerner.com/r4/abc"),
    ("Enterprise Family Practice", NEXTGEN),
    ("ZZ Podiatry LTD", "https://fhir4.healow.com/fhir/r4/AAAAAP"),
    ("Family Practice 2", NEXTGEN),
    ("TESTA FAMILY MEDICINE", "https://fhir4.healow.com/fhir/r4/AAAABB"),
    ("TESTER PEDIATRICS PC", "https://fhir4.healow.com/fhir/r4/AAAABC"),
    ("TESTOSTERONE CLINIC OF OHIO", "https://fhir4.healow.com/fhir/r4/AAAABD"),
    ("TESTING CENTER OF AMERICA", "https://fhir4.healow.com/fhir/r4/AAAABE"),
    ("FASTEST CARE CLINIC", "https://fhir4.healow.com/fhir/r4/AAAABF"),
    ("LATEST HEALTH", "https://fhir4.healow.com/fhir/r4/AAAABG"),
    ("GREATEST CARE", "https://fhir4.healow.com/fhir/r4/AAAABH"),
    ("CONTEST MEDICAL", "https://fhir4.healow.com/fhir/r4/AAAABI"),
    ("ATTEST HEALTH", "https://fhir4.healow.com/fhir/r4/AAAABJ"),
    ("Testa Family", "https://fhir4.healow.com/fhir/r4/AAAABK"),
    ("Z Reynolds Family Practice", NEXTGEN),
    ("Z Riverfield Family Health", NEXTGEN),
])
def test_real_practices_stay_listed(name, url):
    assert is_listing(name, url)


def test_every_data_file_ships_with_a_pip_install():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["app"]
    for path in (ROOT / "app" / "data").rglob("*"):
        if path.is_file():
            relative = path.relative_to(ROOT / "app")
            assert any(relative.match(pattern) for pattern in project), relative


def test_addresses_must_be_https_with_a_real_host():
    assert clean_url(" https://fhir.example.org/r4/ ") == "https://fhir.example.org/r4/"
    assert clean_url("https:// pod47.mmi.prod.fhir.ema-api.com/fhir/r4/") == "https://pod47.mmi.prod.fhir.ema-api.com/fhir/r4/"
    for bad in ("http://fhir.example.org/r4", "https://my vision care, pllc.ef.prod.fhir.ema-api.com/fhir/r4/", "fhir.example.org",
                "https://localhost/r4", "https://exa_mple.org/r4", "ftp://fhir.example.org", "https://", "https://user:pw@fhir.example.org/r4"):
        assert clean_url(bad) is None, bad


@pytest.mark.parametrize("given, want", [
    ("Lady Lake, Flori", "Lady Lake, FL"), ("Sun City, Arizo", "Sun City, AZ"), ("Saranac Lake, New Y", "Saranac Lake, NY"),
    ("Sugar Land, Texas", "Sugar Land, TX"), ("Anchorage, 99515", "Anchorage"), ("Manhattan Beach, NA", "Manhattan Beach"),
    ("CHICAGO, IL", "Chicago, IL"), ("san jose, CA", "San Jose, CA"), ("MCALLEN, TX", "McAllen, TX"), ("O'FALLON, MO", "O'Fallon, MO"),
    ("ST. LOUIS, MO", "St. Louis, MO"), ("WINSTON-SALEM, NC", "Winston-Salem, NC"), ("Nassau, Bahamas", "Nassau, Bahamas"),
    ("Hamilton, Bermuda", "Hamilton, Bermuda"), ("WA, OR, MT and more", "WA, OR, MT and more"), ("OH, FL", "OH, FL"), ("TX", "TX"),
    ("Texas", "TX"), ("McKinney, TX", "McKinney, TX"), (None, None), ("", None), ("San Juan, Puerto Rico", "San Juan, PR"),
    ("West Orange, NJ-Ne", "West Orange, NJ"), ("Baytown, TXTX", "Baytown, TX"), ("Key West, FLv", "Key West, FL"), ("Largo, v", "Largo"),
    ("Hartsville, Sc-So", "Hartsville, SC"), ("Aspen, C", "Aspen"), ("., NY", "NY"), ("St. Thomas, US Vi", "St. Thomas, VI"),
])
def test_locations_read_the_same_way_everywhere(given, want):
    assert tidy_location(given) == want
    assert tidy_location(want) == want      # once is enough


@pytest.mark.parametrize("given, want", [
    ("Physicians&#39; Group  of   Ohio", "Physicians' Group of Ohio"), ("  Mercy Clinic ,", "Mercy Clinic"), ('"Coastal Dermatology"', "Coastal Dermatology"),
    ("- Family Care -", "Family Care"), ("*Washington County Hospital", "Washington County Hospital"), ("Hope Clinic, Inc.", "Hope Clinic, Inc."),
    ("Acme Medical (Downtown)", "Acme Medical (Downtown)"), ("Smith &amp; Jones Medical Clinic", "Smith & Jones Medical Clinic"),
    ("mercy family clinic", "Mercy Family Clinic"), ("HCA HEALTHCARE", "HCA Healthcare"),
    ("ROUSE FAMILY MEDICAL CLINIC, P.A.", "Rouse Family Medical Clinic, P.A."), ("eClinicalWorks Practice", "eClinicalWorks Practice"),
])
def test_names_are_tidied(given, want):
    vocab = frozenset({"MERCY", "FAMILY", "CLINIC", "MEDICAL", "HEALTHCARE", "ROUSE"})
    assert refresh.clean_name(given, vocab) == want


def test_one_routine_writes_names_in_ordinary_case():
    vocab = refresh.vocabulary(["Saint Luke's Hospital", "Mercy Medical Center", "Mercy Clinic", "Medical Center",
                                "Family Practice", "Family Care", "Practice Partners", "Hospital of the Lakes"])
    cases = {"ST. LUKE'S HOSPITAL": "St. Luke's Hospital", "MERCY FAMILY PRACTICE, PLLC": "Mercy Family Practice, PLLC",
             "2ND FAMILY CARE OF NY, MD PA": "2nd Family Care of NY, MD PA", "CTMC ER": "CTMC ER", "AACI": "AACI",
             "GI MEDICAL CENTER": "GI Medical Center", "Mercy Clinic": "Mercy Clinic"}
    assert {name: readable_name(name, vocab) for name in cases} == cases
    assert not hasattr(refresh, "display_name")


def row(name, url, **fields):
    return {"name": name, "platform": "epic", "fhir_base_url": url, **fields}


def test_same_name_and_address_is_one_listing_and_saved_ids_still_resolve():
    url = "https://epic.ccf.org/FHIR/api/FHIR/R4"
    existing = [{"id": "epic-cleveland-clinic", **row("Cleveland Clinic", url)},
                {"id": "epic-cleveland-clinic-2", **row("Cleveland Clinic", url, location="OH, FL, PA, NV and more", featured=True)}]
    new = [row("Cleveland Clinic", url), row("CLEVELAND CLINIC", url + "/", location="OH, FL, PA, NV and more")]
    out = merge(existing, collapse_duplicates(new), "epic-")
    assert len(out) == 1
    [only] = out
    assert only["location"] == "OH, FL, PA, NV and more"
    assert {only["id"], *only["former_ids"]} == {"epic-cleveland-clinic", "epic-cleveland-clinic-2"} and only["featured"]


def test_a_practice_at_different_places_on_one_address_stays_separate():
    rows = [row("St Lukes", NEXTGEN, location="Tampa, FL"), row("St Lukes", NEXTGEN, location="Boise, ID"), row("St Lukes", NEXTGEN)]
    assert sorted(r.get("location", "") for r in collapse_duplicates(rows)) == ["Boise, ID", "Tampa, FL"]


def test_a_practice_with_two_sites_on_one_address_keeps_each_id_with_its_site():
    existing = [{"id": "nextgen-a", **row("St Lukes", NEXTGEN, location="Tampa, FL")}, {"id": "nextgen-b", **row("St Lukes", NEXTGEN, location="Boise, ID")}]
    for new in ([row("St Lukes", NEXTGEN, location="Boise, ID"), row("St Lukes", NEXTGEN, location="Tampa, FL")],
                [row("St Lukes", NEXTGEN, location="Tampa, FL"), row("St Lukes", NEXTGEN, location="Boise, ID")]):
        assert {e["id"]: e["location"] for e in merge(existing, new, "nextgen-")} == {"nextgen-a": "Tampa, FL", "nextgen-b": "Boise, ID"}


def test_two_systems_with_one_name_on_different_addresses_stay_separate():
    rows = [row("Memorial Health", "https://oh/R4"), row("Memorial Health", "https://ga/R4")]
    assert len(collapse_duplicates(rows)) == 2


def test_practices_sharing_an_address_are_not_mistaken_for_each_other(capsys):
    """NextGen lists 4,000 practices on one address, so an address says nothing about which practice it is."""
    existing = [{"id": f"nextgen-{n.lower()}", **row(n, NEXTGEN)} for n in ("Alpha", "Beta", "Gamma")]
    out = merge(existing, [row("Alpha", NEXTGEN), row("Delta", NEXTGEN)], "nextgen-")
    assert {e["name"]: e["id"] for e in out} == {"Alpha": "nextgen-alpha", "Delta": "nextgen-delta"}
    assert not out[0].get("former_ids") and not out[1].get("former_ids")
    err = capsys.readouterr().err
    assert "dropped (no longer published): Beta" in err and "dropped (no longer published): Gamma" in err


def test_a_renamed_practice_keeps_its_id_when_the_address_is_its_own():
    out = merge([{"id": "epic-old", **row("KP SoCal prod", "https://kp/R4")}], [row("Kaiser Permanente - Southern California", "https://kp/R4")], "epic-")
    assert [e["id"] for e in out] == ["epic-old"]


def test_a_closed_system_does_not_hand_its_id_to_another_with_the_same_name(capsys):
    existing = [{"id": "epic-oh", **row("Memorial Health", "https://oh/R4", location="OH")},
                {"id": "epic-ga", **row("Memorial Health", "https://ga/R4", location="GA")}]
    out = merge(existing, [row("Memorial Health", "https://oh/R4", location="OH")], "epic-")
    assert [e["id"] for e in out] == ["epic-oh"] and not out[0].get("former_ids")
    assert "dropped (no longer published): Memorial Health" in capsys.readouterr().err


def test_tidy_applies_every_rule_and_says_what_it_dropped(capsys):
    entries = [row("TestFamily Healthcare", "https://a.example.org/r4"), row("MERCY CLINIC", "https://b.example.org/r4", location="CHICAGO, IL"),
               row("Good Clinic", "http://c.example.org/r4"), row("Good Clinic", "https://my vision care, pllc.example.org/r4")]
    assert tidy(entries, frozenset({"MERCY", "CLINIC"})) == [row("Mercy Clinic", "https://b.example.org/r4", location="Chicago, IL")]
    err = capsys.readouterr().err
    assert "TestFamily Healthcare" in err and "http://c.example.org/r4" in err


def test_tidying_twice_changes_nothing():
    entries = [row("  MERCY  CLINIC ,", "https://b.example.org/r4 ", location="Lady Lake, Flori"), row("Hope&#39;s Place", "https://c.example.org/r4")]
    vocab = frozenset({"MERCY", "CLINIC"})
    once = tidy([dict(e) for e in entries], vocab)
    assert tidy([dict(e) for e in once], vocab) == once


# ---------------------------------------------------------------------------
# Epic's facilities
# ---------------------------------------------------------------------------

def epic_bundle():
    return {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "e1", "address": "https://mgb/R4"}},
        {"resource": {"resourceType": "Organization", "id": "b1", "name": "Mass General Brigham", "endpoint": [{"reference": "urn:uuid:e1"}]}},
        {"resource": {"resourceType": "Organization", "id": "f1", "name": "MASSACHUSETTS GENERAL HOSPITAL", "partOf": {"reference": "urn:uuid:b1"},
                      "address": [{"city": "BOSTON", "state": "Massachusetts"}]}},
        {"resource": {"resourceType": "Organization", "id": "f2", "name": "Brigham and Women's Hospital", "partOf": {"reference": "urn:uuid:b1"},
                      "address": [{"city": "Boston", "state": "MA"}]}},
        {"resource": {"resourceType": "Organization", "id": "f3", "name": "Mass General Brigham", "partOf": {"reference": "urn:uuid:b1"},
                      "address": [{"city": "Salem", "state": "MA"}]}},
        {"resource": {"resourceType": "Organization", "id": "f4", "name": "Brigham and Women's Hospital", "partOf": {"reference": "urn:uuid:b1"},
                      "address": [{"city": "Boston", "state": "MA"}]}},
    ]}


def test_epic_facility_names_are_kept_for_search():
    [brand] = refresh.epic_entries({"brands": epic_bundle(), "r4": {"entry": []}})
    # Each facility once, and the one named like its brand only for its town.
    assert brand["facilities"] == ["Brigham and Women's Hospital|Boston|MA", "MASSACHUSETTS GENERAL HOSPITAL|Boston|MA", "|Salem|MA"]
    assert brand["location"] == "Boston, MA" or brand["location"] == "MA"


def test_facilities_are_written_apart_from_the_listings(tmp_path):
    entries = refresh.epic_entries({"brands": epic_bundle(), "r4": {"entry": []}})
    [listing] = merge([], entries, "epic-")
    assert "facilities" not in listing      # API responses stay small
    path = tmp_path / "facilities.tsv"
    refresh.write_facilities(path, entries)
    lines = path.read_text().splitlines()
    assert lines[0] == "epic-mass-general-brigham\t\tSalem\tMA"
    assert lines[1] == "epic-mass-general-brigham\tBrigham and Women's Hospital\tBoston\tMA"


def test_a_refresh_of_one_vendor_keeps_the_others_facilities(tmp_path):
    path = tmp_path / "facilities.tsv"
    path.write_text("epic-old\tStale Site\t\t\noracle-x\tOracle Site\t\t\n")
    entries = refresh.epic_entries({"brands": epic_bundle(), "r4": {"entry": []}})
    merge([], entries, "epic-")
    refresh.write_facilities(path, entries, replace_prefix="epic-")
    text = path.read_text()
    assert "Stale Site" not in text and "Oracle Site" in text and "Brigham and Women" in text


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------

@pytest.fixture
def scratch_directory(tmp_path, monkeypatch):
    folder = tmp_path / "institutions"
    folder.mkdir()
    monkeypatch.setattr(refresh, "DIRECTORY", folder)
    monkeypatch.setattr(refresh, "FACILITIES", tmp_path / "facilities.tsv")
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    return folder


def medhost_rows(n):
    return [{"facilityName": f"County Hospital {i}", "serviceBaseUrl": f"https://fhir.example.org/tenant/{i}"} for i in range(n)]


def test_a_broken_vendor_does_not_stop_the_others_and_the_run_fails_at_the_end(scratch_directory, monkeypatch, tmp_path):
    def fetch(url):
        if url == refresh.GREENWAY_URL:
            raise OSError("connection reset")
        return medhost_rows(3)
    monkeypatch.setattr(refresh, "fetch", fetch)
    failures = tmp_path / "failed.txt"
    assert refresh.main(["greenway", "medhost", "--failures", str(failures)]) == 1
    assert len(json.loads((scratch_directory / "medhost.json").read_text())) == 3
    assert not (scratch_directory / "greenway.json").exists()
    assert "greenway" in failures.read_text() and "connection reset" in failures.read_text()


def test_a_list_that_shrinks_by_half_is_refused_and_the_old_file_stays(scratch_directory, monkeypatch):
    path = scratch_directory / "medhost.json"
    old = [{"id": f"medhost-{i}", "name": f"County Hospital {i}", "platform": "medhost", "fhir_base_url": f"https://fhir.example.org/tenant/{i}"}
           for i in range(100)]
    path.write_text(json.dumps(old))
    monkeypatch.setattr(refresh, "fetch", lambda url: medhost_rows(10))
    assert refresh.main(["medhost"]) == 1
    assert json.loads(path.read_text()) == old


def test_downloads_accept_a_list_served_as_a_file(monkeypatch):
    """MEDHOST sends its list as application/octet-stream, and refuses a request for JSON and nothing else."""
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a):
            return b"[]"

    def urlopen(req, timeout):
        seen.update(req.header_items())
        return Response()

    monkeypatch.setattr(refresh, "urlopen", urlopen)
    assert refresh.fetch("https://example.org/list.json") == []
    assert "*/*" in seen["Accept"]
    assert refresh.ECW_URL.endswith("/ecwopendev/global/practiceList")     # eClinicalWorks moved it from /external/


def test_written_files_can_be_read_by_the_app(tmp_path):
    path = tmp_path / "x.json"
    refresh.write_json(path, [1], indent=0)
    assert path.read_text() and (sys.platform == "win32" or path.stat().st_mode & 0o777 == 0o644)


def test_a_file_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_text("[1]")
    monkeypatch.setattr(refresh.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        refresh.write_json(path, [2], indent=0)
    assert path.read_text() == "[1]" and [p.name for p in tmp_path.iterdir()] == ["x.json"]


def repo(tmp_path, files):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)     # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.org")
    run("config", "user.name", "t")
    folder = tmp_path / "app" / "data" / "institutions"
    folder.mkdir(parents=True)
    for name, rows in files.items():
        (folder / name).write_text(json.dumps(rows))
    run("add", ".")
    run("commit", "-qm", "x")
    return folder


def test_a_changed_host_or_many_moves_wait_for_a_person(tmp_path):
    a = {"id": "a", "name": "A", "platform": "epic", "fhir_base_url": "https://a.example.org/r4"}
    b = {"id": "b", "name": "B", "platform": "epic", "fhir_base_url": "https://b.example.org/r4"}
    folder = repo(tmp_path, {"epic.json": [a, b]})

    same = refresh.summarize(tmp_path)
    assert same["review"] == [] and same["rows"] == [("epic.json", 2, 0, 0, 0)]

    (folder / "epic.json").write_text(json.dumps([{**a, "fhir_base_url": "https://attacker.example.net/r4"}, b]))
    moved = refresh.summarize(tmp_path)
    assert moved["rows"] == [("epic.json", 2, 0, 0, 1)]
    assert any("A" in why and re.search(r"\battacker\.example\.net\b", why) for why in moved["review"])

    (folder / "epic.json").write_text(json.dumps([{**a, "fhir_base_url": "https://a.example.org/r4/new"}, b]))
    assert refresh.summarize(tmp_path)["review"] == []      # the same server, a new path

    rows = [{"id": str(i), "name": str(i), "platform": "epic", "fhir_base_url": f"https://h.example.org/{i}"} for i in range(20)]
    (folder / "many.json").write_text(json.dumps(rows))
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "y"], cwd=tmp_path, check=True)
    (folder / "many.json").write_text(json.dumps([{**r, "fhir_base_url": r["fhir_base_url"] + "/v2"} for r in rows]))
    assert any("20" in why for why in refresh.summarize(tmp_path)["review"])

    (folder / "many.json").write_text(json.dumps(rows[1:]))
    assert any("no longer published" in why for why in refresh.summarize(tmp_path)["review"])


def test_the_pull_request_text_stays_under_githubs_limit_however_many_sign_ins_fail(tmp_path):
    # The first check of every eClinicalWorks practice found 1,074 error pages, and GitHub refused the text that listed them.
    repo(tmp_path, {"epic.json": [{"id": "a", "name": "A", "platform": "epic", "fhir_base_url": "https://a.example.org/r4"}]})
    report = tmp_path / "sign-in.json"
    failed = [{"status": "error_page", "names": [f"Practice {i:04}"], "platform": "ecw", "detail": "x" * 200} for i in range(1100)]
    failed += [{"status": "client_unknown", "names": [f"System {i}"], "platform": "epic", "detail": "OAuth2 Error"} for i in range(3)]
    report.write_text(json.dumps([*failed, {"status": "ok", "names": ["Fine"], "platform": "epic", "detail": ""}]))
    body, _ = refresh.summary_markdown(tmp_path, report)
    assert len(body) < 65536
    assert "1 of 1104 servers" in body
    assert body.count("`client_unknown`") == 3 and body.count("`error_page` Practice") == refresh.SIGN_INS_LISTED
    assert f"and {1100 - refresh.SIGN_INS_LISTED} more `error_page`" in body


def test_sign_in_check_age_is_reported(tmp_path, capsys):
    status = tmp_path / "s.json"
    status.write_text(json.dumps({"checked": "2026-10-03", "servers": {}}))
    assert refresh.check_fresh(status, 21, today="2026-10-20") == 0
    assert refresh.check_fresh(status, 21, today="2026-10-30") == 1
    out = capsys.readouterr().out
    assert "27 days" in out and "DIRECTORY_TOKEN" in out
    assert refresh.check_fresh(status, 21, today="2026-10-30", warn_only=True) == 0
    status.write_text("{}")
    assert refresh.check_fresh(status, 21, today="2026-10-30") == 1


def test_the_workflow_cannot_hide_a_failure():
    assert re.search(r"defaults:\s+run:\s+shell: bash", WORKFLOW)     # bash -eo pipefail, so `| tee` keeps a crash's status
    assert "|| true\n" not in WORKFLOW.split("Open or update")[0]
    assert "--failures" in WORKFLOW


def test_the_workflow_refreshes_without_a_token_and_says_what_is_missing():
    steps = WORKFLOW.split("steps:")[1]
    assert steps.index("Refresh") < steps.index("DIRECTORY_TOKEN isn't set")
    assert "actions/upload-artifact" in WORKFLOW and "Open or update the pull request" in WORKFLOW
    assert re.search(r"name: Open or update the pull request\n\s+if: .*HAS_TOKEN == 'true'", WORKFLOW)
    assert "Settings" in WORKFLOW.split("DIRECTORY_TOKEN isn't set")[1].split("\n")[0]


def test_auto_merge_waits_when_a_person_should_look():
    assert '"$REVIEW" = 0' in WORKFLOW
    assert '"$DROPPED"' not in WORKFLOW
    assert "app/data/facilities.tsv" in WORKFLOW
