"""Finding a health system by what a person calls it, against the shipped directory; and what happens when its files are damaged."""

import json
import shutil
import time
from pathlib import Path

import pytest

from app import directory
from app.core import config

CONNECTABLE = {"epic", "cerner", "healow", "athena"}


def find(q, limit=30, **kw):
    return directory.search(q, limit=limit, available=CONNECTABLE, **kw)


def names(q, limit=30, **kw):
    return [r["name"] for r in find(q, limit, **kw)["results"]]


@pytest.mark.parametrize("q, first", [
    ("kaiser", "Kaiser Permanente"),
    ("kiaser", "Kaiser Permanente"),                  # letters swapped
    ("kaisr permanente", "Kaiser Permanente"),        # one dropped
    ("kaiser permanante", "Kaiser Permanente"),
    ("cleaveland clinic", "Cleveland Clinic"),
    ("sutter helath", "Sutter Health"),
    ("mayo clinc", "Mayo Clinic"),
    ("mayo", "Mayo Clinic"),
    ("cleveland clinic", "Cleveland Clinic"),
    ("Cleveland-Clinic", "Cleveland Clinic"),
    ("mt sinai", "Mount Sinai"),
    ("mount sinai", "Mount Sinai"),
    ("ohsu", "Oregon Health"),
    ("oregon health and science", "Oregon Health"),
    ("nyp", "New York Presbyterian"),
    ("new york presbyterian", "New York Presbyterian"),
    ("newyork presbyterian", "New York Presbyterian"),
    ("ucsf", "UCSF"),
    ("mskcc", "Memorial Sloan"),
    ("chop", "CHOP"),
    ("children's hospital of philadelphia", "CHOP"),
    ("johns hopkins", "Johns Hopkins"),
])
def test_what_people_call_it_comes_first(q, first):
    assert any(first.lower() in n.lower() for n in names(q)[:3]), names(q)[:5]


@pytest.mark.parametrize("q, system", [
    ("massachusetts general", "Mass General Brigham"),
    ("mass general", "Mass General Brigham"),
    ("mgh", "Mass General Brigham"),
    ("brigham and women", "Mass General Brigham"),
    ("brigham and women's hospital", "Mass General Brigham"),
    ("dana farber", "Mass General Brigham"),
    ("ronald reagan", "UCLA"),
    ("lucile packard", "Stanford"),
])
def test_a_hospital_finds_the_system_it_belongs_to(q, system):
    assert any(system in n for n in names(q)[:5]), names(q)[:5]


def test_a_facility_match_says_which_facility():
    top = next(r for r in find("brigham and women")["results"] if "Mass General Brigham" in r["name"])
    assert top["includes"].startswith("Brigham and Women's")
    assert find("massachusetts general hospital")["results"][0]["includes"].startswith("Massachusetts General Hospital")
    plain = find("mass general brigham")["results"][0]
    assert "includes" not in plain      # its own name says it


def test_the_response_stays_small_for_a_big_system():
    system = find("brigham and women")["results"][0]
    assert "facilities" not in system
    assert len(json.dumps(system)) < 800


def test_saint_and_st_and_apostrophes_are_the_same():
    a, b, c = names("saint lukes", 60), names("st lukes", 60), names("st. luke's", 60)
    assert a == b == c
    assert sum(n.lower().startswith(("st luke", "st. luke", "saint luke")) for n in a[:8]) >= 4
    platforms = [r["platform"] for r in find("st lukes", 8)["results"]]
    assert platforms[0] in CONNECTABLE and "nextgen" not in platforms[:2]


def test_apostrophes_do_not_hide_a_listing():
    assert find("childrens hospital")["total"] == find("children's hospital")["total"] >= 26


def test_state_names_and_codes_are_the_same():
    assert find("dallas texas")["total"] == find("dallas tx")["total"] > 300
    assert find("new york ny")["total"] > 0
    assert find("buffalo new york")["total"] == find("buffalo ny")["total"] > 0


def test_a_short_word_is_a_whole_word():
    rows = find("portland or", 200)["results"]
    assert rows
    assert not [r["name"] for r in rows if (r.get("location") or "").endswith((", ME", ", TX"))]


def test_every_word_can_match_a_different_part_of_the_listing():
    for q in ("mychart cleveland", "mayo clinic mychart", "my chart mayo", "cleveland clinic epic", "mayo patient portal"):
        assert any(n in ("Cleveland Clinic", "Mayo Clinic") for n in names(q)[:3]), q


@pytest.mark.parametrize("q, platform, at_least", [
    ("healow", "healow", 15000), ("eclinicalworks", "healow", 15000), ("mycarecorner", "trubridge", 500), ("yourcare", "medhost", 100),
    ("your care", "medhost", 100), ("cerner", "cerner", 1000), ("mychart", "epic", 1000), ("my chart", "epic", 1000),
])
def test_the_portal_someone_logs_in_to_finds_its_listings(q, platform, at_least):
    res = directory.search(q, limit=5)
    assert res["total"] >= at_least
    assert sum(r["platform"] == platform for r in directory.search(q, limit=200)["results"]) >= 5


def test_nothing_like_it_is_nothing():
    assert find("zzqqxxyy")["total"] == 0
    assert "corrected" not in find("zzqqxxyy")


def test_a_correction_is_reported_and_a_correct_word_is_not_corrected():
    assert find("kiaser")["corrected"] == "kaiser"
    assert find("cleaveland clinic")["corrected"] == "cleveland clinic"
    for q in ("stanford", "mayo", "st lukes", "mays"):
        assert "corrected" not in find(q)
    assert "corrected" not in find("mayo boston")        # two real words that don't go together aren't a typo


def test_exact_comes_before_everything():
    assert names("cleveland clinic")[0] == "Cleveland Clinic"
    assert find("banner health")["results"][0]["name"] == "Banner Health"


def test_connectable_listings_come_first_within_a_band():
    for q in ("clinic", "family medicine", "internal medicine", "st lukes"):
        res = find(q, 30)["results"]
        assert any(r["platform"] in CONNECTABLE for r in res[:3]), q        # a listing named exactly that still comes first


def small_index(rows):
    from app.directory_search import Index
    return Index([{"id": str(i), "platform": "epic", "fhir_base_url": "https://a.example.org/r", **r} for i, r in enumerate(rows)], [], {}, {})


def ranked(index, q, later=None, troubled=frozenset()):
    res = index.search(q, keep=None, drop=set(), later=later, troubled=set(troubled), limit=20, offset=0, state=None)
    return [r["name"] for r in res["results"]]


def test_the_order_of_a_search():
    index = small_index([
        {"name": "Downtown Family Medicine Clinic"},
        {"name": "Family Medicine", "platform": "nextgen"},
        {"name": "Family Medicine Associates", "platform": "nextgen"},
        {"name": "Family Medicine Group"},
        {"name": "Medicine for the Family"},
        {"name": "Quiet Practice", "location": "Family, TX", "places": ["Medicine Hat TX"]},
    ])
    assert ranked(index, "family medicine", later={"nextgen"}) == [
        "Family Medicine",                     # exact, though it can't be connected yet
        "Family Medicine Group",               # starts with it, and can be connected
        "Family Medicine Associates",          # starts with it
        "Downtown Family Medicine Clinic",     # the phrase
        "Medicine for the Family",             # the words
        "Quiet Practice",                      # not in the name at all
    ]


def test_a_failing_sign_in_ranks_after_a_working_one_of_the_same_kind():
    index = small_index([{"name": "Clinic Alpha"}, {"name": "Clinic Beta"}])
    assert ranked(index, "clinic") == ["Clinic Alpha", "Clinic Beta"]
    assert ranked(index, "clinic", troubled={0}) == ["Clinic Beta", "Clinic Alpha"]


def test_featured_and_large_systems_win_ties():
    index = small_index([{"name": "Mercy Aaa"}, {"name": "Mercy Bbb", "featured": True}, {"name": "Mercy Ccc"}])
    assert ranked(index, "mercy")[0] == "Mercy Bbb"
    from app.directory_search import Index
    rows = [{"id": "a", "name": "Mercy Aaa", "platform": "epic"}, {"id": "b", "name": "Mercy Bbb", "platform": "epic"}]
    sized = Index(rows, [("b", f"Site {i}", "", "") for i in range(40)], {}, {})
    assert ranked(sized, "mercy")[0] == "Mercy Bbb"


def test_a_large_system_beats_a_small_one_all_else_being_equal():
    rows = find("trinity health", 10)["results"]
    assert rows[0]["name"].startswith("Trinity Health") and rows[0]["platform"] == "epic"


def test_long_lists_can_be_narrowed_and_paged():
    res = find("family medicine")
    assert res["total"] > 300 and res["states"]["TX"] > 0
    texas = find("family medicine", state="TX")
    assert 0 < texas["total"] < res["total"] and texas["states"]["CA"] == res["states"]["CA"]
    assert all("TX" in (r.get("location") or "") or r.get("places") for r in texas["results"])
    first, second = find("family medicine", 10)["results"], find("family medicine", 10, offset=10)["results"]
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert not find("mayo clinic", 30, state="ZZ")["results"]


def test_a_state_finds_a_system_by_any_of_its_sites():
    assert any(r["name"].startswith("Kaiser") for r in find("kaiser", state="HI")["results"])


def test_listings_of_a_platform_and_a_state_without_words():
    res = directory.search(None, "epic", 5, state="AK")
    assert res["total"] > 0 and all(r["platform"] == "epic" for r in res["results"])
    assert directory.search(None, "healow", 200)["results"][0]["platform"] == "healow"


def test_hidden_and_platform_filters_apply():
    assert not directory.search("smart demo", hidden=frozenset({"smart-health-it"}))["results"]
    assert all(r["platform"] == "epic" for r in directory.search("clinic", "epic", 50)["results"])
    assert all(r["platform"] != "healow" for r in directory.search("clinic", limit=50, hidden=frozenset({"healow"}))["results"])


def test_no_search_takes_long():
    for q in ("clinic", "a", "health", "medical center", "st", "kiaser", "cleaveland clinic", "family medicine tx", "mychart"):
        start = time.perf_counter()
        find(q)
        assert time.perf_counter() - start < 0.25, q        # about 40 ms on a laptop; this one has to hold on a slow CI machine


def test_listings_that_are_hard_to_tell_apart_carry_a_hint():
    same = [i for i in directory._institutions() if i["name"].lower() == "riverview health" and i["platform"] == "epic"]
    assert len(same) == 3
    unplaced = next(i for i in same if not i.get("location"))
    assert unplaced["hint"] and "mercy" in unplaced["hint"]
    tenet = [i for i in directory._institutions() if i["name"] == "Tenet HealthSystem Medical"]
    assert len(tenet) > 1 and len({i["hint"] for i in tenet}) == len(tenet)
    assert "hint" not in directory.get_institution("epic-cleveland-clinic")


# ---------------------------------------------------------------------------
# The shipped files
# ---------------------------------------------------------------------------

def test_every_shipped_file_is_valid():
    assert directory.problems() == []
    seen: set[str] = set()
    for path in sorted((config.APP_DIR / "data" / "institutions").glob("*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(rows, list) and rows, path.name
        for row in rows:
            assert row["id"] not in seen, row["id"]
            seen.add(row["id"])
            assert row["name"].strip() == row["name"] and "&#" not in row["name"] and "  " not in row["name"]
            assert row["platform"] in directory.PLATFORMS
            assert row["fhir_base_url"].startswith("https://") and " " not in row["fhir_base_url"]


def test_shipped_listings_have_been_through_the_rules():
    from scripts.refresh_directory import is_listing, tidy_location
    for row in directory._institutions():
        if row["platform"] not in ("athena", "smart-health-it"):
            assert is_listing(row["name"], row["fhir_base_url"]), row["name"]
            assert tidy_location(row.get("location")) == row.get("location"), row["location"]


def test_the_aliases_point_at_real_listings():
    aliases = json.loads((config.APP_DIR / "data" / "directory_aliases.json").read_text(encoding="utf-8"))
    assert aliases
    for inst_id in aliases:
        assert directory.get_institution(inst_id), inst_id


def test_every_warning_points_at_listings_that_exist():
    data = json.loads((config.APP_DIR / "data" / "sign_in_status.json").read_text(encoding="utf-8"))
    assert data["checked"] and data["servers"]
    for entry in data["servers"].values():
        for inst_id in entry.get("instead", ()):
            assert directory.get_institution(inst_id), inst_id


# ---------------------------------------------------------------------------
# Damage
# ---------------------------------------------------------------------------

@pytest.fixture
def data_copy(tmp_path, monkeypatch):
    """A scratch copy of app/data the directory reads instead, with only three vendors, to keep it quick."""
    folder = tmp_path / "app" / "data"
    (folder / "institutions").mkdir(parents=True)
    source = Path(config.APP_DIR) / "data"
    for name in ("curated.json", "medhost.json", "trubridge.json"):
        shutil.copy(source / "institutions" / name, folder / "institutions" / name)
    for name in ("sign_in_status.json", "facilities.tsv", "directory_aliases.json"):
        shutil.copy(source / name, folder / name)
    with monkeypatch.context() as patch:
        patch.setattr(directory, "_data_dir", lambda: folder)
        patch.setattr(directory, "EXPECTED_FILES", ("curated", "medhost", "trubridge"))
        directory.reload()
        yield folder
    directory.reload()


def test_problems_do_not_load_the_list(data_copy):
    directory.reload()
    assert directory.problems() == []
    assert not directory._loaded.cache_info().currsize


def test_a_truncated_file_loses_that_vendor_and_says_so(data_copy, caplog):
    path = data_copy / "institutions" / "medhost.json"
    path.write_text(path.read_text()[:500])
    directory.reload()
    assert directory.stats().get("medhost") is None and directory.stats()["trubridge"] > 100
    assert directory.search("hospital")["total"] > 0
    [problem] = directory.problems()
    assert "MEDHOST" in problem["text"] and problem["id"] == "directory-files"
    assert any("medhost.json" in r.getMessage() for r in caplog.records)


def test_a_missing_file_is_noticed(data_copy):
    (data_copy / "institutions" / "medhost.json").unlink()
    directory.reload()
    assert directory.stats().get("medhost") is None
    assert "MEDHOST" in directory.problems()[0]["text"]


def test_an_object_in_place_of_a_list_is_treated_as_damage(data_copy):
    (data_copy / "institutions" / "medhost.json").write_text('{"id": "x"}')
    directory.reload()
    assert directory.stats().get("medhost") is None and directory.problems()


def test_bad_entries_are_skipped_and_the_rest_stay(data_copy):
    path = data_copy / "institutions" / "medhost.json"
    rows = json.loads(path.read_text())
    good = len(rows)
    path.write_text(json.dumps([*rows, {"id": "no-name", "platform": "medhost", "fhir_base_url": "https://a.example.org/r"},
                                {"id": "no-platform", "name": "A", "fhir_base_url": "https://a.example.org/r"},
                                {"id": "unknown-platform", "name": "A", "platform": "nope", "fhir_base_url": "https://a.example.org/r"},
                                {"name": "No Id", "platform": "medhost", "fhir_base_url": "https://a.example.org/r"},
                                {"id": 5, "name": "Bad Id", "platform": "medhost", "fhir_base_url": "https://a.example.org/r"}, "text", None]))
    directory.reload()
    assert directory.stats()["medhost"] == good
    assert directory.search("hospital")["total"] > 0 and directory.featured() is not None
    assert "7" in directory.problems()[0]["text"]


@pytest.mark.parametrize("content", ['{"servers": ', "[]", '"text"', '{"servers": []}', '{"servers": {"https://a": "broken"}}'])
def test_a_damaged_status_file_means_no_warnings(data_copy, content):
    (data_copy / "sign_in_status.json").write_text(content)
    directory.reload()
    inst = directory.search("hospital", limit=1)["results"][0]
    assert directory.sign_in_problem(inst) is None
    assert directory.search("hospital")["total"] > 0


def test_a_damaged_facility_or_alias_file_only_loses_those_words(data_copy):
    (data_copy / "facilities.tsv").write_text("not a line of four columns\n")
    (data_copy / "directory_aliases.json").write_text("[1]")
    directory.reload()
    assert directory.search("hospital")["total"] > 0
    text = " ".join(p["text"] for p in directory.problems())
    assert text


def test_nothing_at_all_still_answers(data_copy, client):
    shutil.rmtree(data_copy / "institutions")
    directory.reload()
    assert directory.search("hospital") == {"total": 0, "results": [], "states": {}}
    assert directory.problems()
    featured = client.get("/api/directory/featured")
    assert featured.status_code == 200 and featured.json()["results"] == []
    assert featured.json()["problem"]
    assert client.get("/api/directory", params={"q": "x"}).status_code == 200
    assert client.get("/api/directory/anything").status_code == 404


def test_the_owner_hears_about_a_damaged_directory(data_copy, client):
    (data_copy / "institutions" / "medhost.json").unlink()
    directory.reload()
    assert any(p["id"] == "directory-files" for p in client.get("/api/health").json()["problems"])      # before anything reads it
    (data_copy / "institutions" / "trubridge.json").write_text("{")
    directory.reload()
    client.get("/api/directory/featured")
    problem = next(p for p in client.get("/api/status").json()["problems"] if p["id"] == "directory-files")
    assert "TruBridge" in problem["text"] and "MEDHOST" in problem["text"]
    assert client.post("/api/connections/ehr", json={"institution_id": "not-a-real-one"}).status_code == 400


# ---------------------------------------------------------------------------
# Sign-in warnings age
# ---------------------------------------------------------------------------

@pytest.fixture
def warned(monkeypatch):
    inst = next(i for i in directory._institutions() if i["platform"] == "epic" and i.get("fhir_base_url"))
    key = directory._status_key(inst)

    def set_status(status, checked, today, since="2026-09-01"):
        monkeypatch.setattr(directory, "_status_file", lambda: {"checked": checked, "servers": {key: {"status": status, "since": since}}})
        monkeypatch.setattr(directory, "_today", lambda: __import__("datetime").date.fromisoformat(today))
        directory._troubled_on.cache_clear()
        return directory.sign_in_problem(inst)

    yield inst, set_status
    directory._troubled_on.cache_clear()


def test_a_recent_warning_is_a_warning(warned):
    _, status = warned
    problem = status("portal_error", "2026-10-03", "2026-10-10")
    assert problem["stale"] is False and problem["checked"] == "2026-10-03" and problem["since"] == "2026-09-01"


def test_an_old_warning_says_it_may_have_been_fixed(warned):
    _, status = warned
    assert status("portal_error", "2026-10-03", "2026-11-01")["stale"] is False
    assert status("portal_error", "2026-10-03", "2026-11-03")["stale"] is True


def test_a_client_not_yet_recognized_ages_fastest(warned):
    _, status = warned
    assert status("client_unknown", "2026-10-03", "2026-10-10")["stale"] is False
    assert status("client_unknown", "2026-10-03", "2026-10-18")["stale"] is True


def test_an_old_warning_no_longer_lowers_a_listing(warned):
    inst, status = warned
    status("portal_error", "2026-10-03", "2026-10-10")
    assert any(directory._index().rows[r]["id"] == inst["id"] for r in directory._troubled_rows())
    status("portal_error", "2026-10-03", "2026-12-10")
    assert not directory._troubled_rows()


def test_a_warning_with_no_date_counts_as_old(warned):
    _, status = warned
    assert status("portal_error", None, "2026-10-10", since=None)["stale"] is True
    assert status("portal_error", "garbage", "2026-10-10", since=None)["stale"] is True


# ---------------------------------------------------------------------------
# What the picker gets, and says
# ---------------------------------------------------------------------------

def test_the_api_narrows_pages_and_reports_corrections(client):
    res = client.get("/api/directory", params={"q": "family medicine", "limit": 10}).json()
    assert res["total"] > 300 and len(res["results"]) == 10 and res["states"]["TX"] > 0 and res["problem"] is None
    texas = client.get("/api/directory", params={"q": "family medicine", "limit": 10, "state": "tx"}).json()
    assert 0 < texas["total"] < res["total"]
    next_page = client.get("/api/directory", params={"q": "family medicine", "limit": 10, "offset": 10}).json()
    assert not {r["id"] for r in res["results"]} & {r["id"] for r in next_page["results"]}
    assert client.get("/api/directory", params={"q": "kiaser"}).json()["corrected"] == "kaiser"
    assert "corrected" not in client.get("/api/directory", params={"q": "kaiser"}).json()
    assert client.get("/api/directory", params={"q": "x" * 201}).status_code == 422
    found = client.get("/api/directory", params={"q": "brigham and women"}).json()["results"][0]
    assert found["includes"] and found["portal"] == "MyChart"


def test_the_picker_counts_what_can_connect_and_offers_athenahealth(client):
    client.put("/api/settings/developer", json={"enabled": False})
    featured = client.get("/api/directory/featured").json()
    assert featured["total"] > 35000 and featured["connectable"] < featured["total"]
    assert featured["athena"]["id"] == "athenahealth" and featured["problem"] is None
    assert "smart-health-it" not in client.get("/api/directory", params={"q": "demo"}).json()["platforms"]


STATIC = Path(config.APP_DIR) / "static" / "js" / "views"


def test_the_records_request_says_what_the_rule_says_and_stays_on_the_page():
    letter = (STATIC / "records-request.js").read_text()
    for needed in ("45 CFR 164.524", "designated record set", "within 30 days", "reasonable, cost-based fee", "in writing"):
        assert needed in letter
    assert "post(" not in letter and "fetch(" not in letter and "localStorage" not in letter       # nothing saved or sent


def test_the_picker_keeps_its_other_ways_in_when_the_list_fails():
    picker = (STATIC / "sources.js").read_text()
    start = picker.index("async function openAddHealthSystem")
    body = picker[start:picker.index("function confirmInstitution")]
    assert "catch (err) { m.close()" not in body
    for action in ('data-action="records"', 'data-action="import"', 'data-action="custom"', 'data-id="athenahealth"'):
        assert action in body
    assert "No matches" not in body.split("const showPopular")[1].split("const showResults")[0]


# ---------------------------------------------------------------------------
# What a second pass over real searches found
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q, first", [
    ("ohio health", "OhioHealth"),
    ("peace health", "PeaceHealth"),
    ("metro health", "MetroHealth OH"),
    ("health partners", "HealthPartners"),
    ("st louis children", "BJC Healthcare & Washington University"),
    ("pennsylvania hospital", "Penn Medicine"),
    ("children's hospital la", "Children's Hospital Los Angeles"),
    ("mayo clinic mychart", "Mayo Clinic"),
    ("my chart mayo", "Mayo Clinic"),
])
def test_the_one_they_mean_comes_first(q, first):
    assert names(q)[0] == first, names(q)[:5]


def test_words_written_together_in_a_name_are_found_apart():
    index = small_index([{"name": "Peace Valley Health"}, {"name": "PeaceHealth"}, {"name": "PeaceHealth Southwest"}])
    assert ranked(index, "peace health")[:2] == ["PeaceHealth", "PeaceHealth Southwest"]


def test_the_network_legal_name_ranks_after_the_name():
    index = small_index([
        {"name": "St Thomas Community Health Center", "network": "Louisiana Children's Medical Center"},
        {"name": "St Louis Children's Clinic"},
    ])
    assert ranked(index, "st louis children") == ["St Louis Children's Clinic", "St Thomas Community Health Center"]
    only_network = small_index([{"name": "Quiet Practice", "network": "Lucile Packard Hospital"},
                                {"name": "Other Practice", "location": "Lucile, ID", "places": ["Packard ID"]}])
    assert ranked(only_network, "lucile packard") == ["Quiet Practice", "Other Practice"]


def test_a_new_york_row_is_not_new_in_its_name_and_york_in_its_town():
    index = small_index([{"name": "New Hope Clinic", "location": "York, PA"}, {"name": "New York Clinic"},
                         {"name": "Quiet Clinic", "location": "New York, NY"}])
    assert ranked(index, "new york")[:2] == ["New York Clinic", "Quiet Clinic"]


def test_a_facility_called_that_comes_before_loose_matches():
    from app.directory_search import Index
    rows = [{"id": "big", "name": "Big System", "platform": "epic", "fhir_base_url": "https://a.example.org/r"},
            {"id": "penn", "name": "Penn Medicine", "platform": "epic", "fhir_base_url": "https://b.example.org/r"}]
    sites = [("big", f"Big Hospital Unit {n} Pennsylvania Rd", "", "") for n in range(40)]
    sites.append(("penn", "Pennsylvania Hospital", "Philadelphia", "PA"))
    res = Index(rows, sites, {}, {}).search("pennsylvania hospital", keep=None, drop=set(), later=None, troubled=set(), limit=5, offset=0, state=None)
    assert [r["name"] for r in res["results"]] == ["Penn Medicine", "Big System"]
    assert res["results"][0]["includes"] == "Pennsylvania Hospital"


def test_a_portal_word_needs_no_explaining():
    top = find("mayo clinic mychart")["results"][0]
    assert top["name"] == "Mayo Clinic" and "includes" not in top


def test_short_names_for_big_cities():
    index = small_index([{"name": "Children's Hospital Los Angeles"}, {"name": "Quiet Clinic", "location": "San Francisco, CA"}])
    assert ranked(index, "childrens hospital la") == ["Children's Hospital Los Angeles"]
    assert ranked(index, "quiet sf") == ["Quiet Clinic"]


@pytest.mark.parametrize("bad", [
    {"location": 5}, {"network": ["not", "text"]}, {"places": "Boston MA"}, {"places": [5]}, {"former_ids": 5},
    {"former_ids": [["nested"]]}, {"fhir_base_url": 5},
])
def test_a_wrong_field_costs_that_entry_and_nothing_else(data_copy, bad):
    path = data_copy / "institutions" / "medhost.json"
    rows = json.loads(path.read_text())
    good = len(rows)
    path.write_text(json.dumps([*rows, {"id": "odd", "name": "Odd Entry", "platform": "medhost", "fhir_base_url": "https://a.example.org/r", **bad}]))
    directory.reload()
    assert directory.stats()["medhost"] == good
    assert directory.search("hospital")["total"] > 0
    assert directory.get_institution(rows[0]["id"])


def test_an_address_that_cannot_be_read_does_not_stop_the_list(data_copy):
    path = data_copy / "institutions" / "medhost.json"
    rows = json.loads(path.read_text())
    twin = {"name": "Twin Hospital", "platform": "medhost", "fhir_base_url": "https://[broken/r"}
    path.write_text(json.dumps([*rows, {"id": "t1", **twin}, {"id": "t2", **twin}]))
    directory.reload()
    assert directory.search("twin hospital")["total"] == 2


@pytest.mark.parametrize("instead", [5, "text", [["nested"]], [None, 5]])
def test_a_warning_with_odd_alternatives_is_still_a_warning(data_copy, instead):
    row = json.loads((data_copy / "institutions" / "medhost.json").read_text())[0]
    (data_copy / "sign_in_status.json").write_text(json.dumps({"checked": "2026-10-03", "servers": {
        directory._status_key(row): {"status": "portal_error", "since": "2026-10-03", "instead": instead}}}))
    directory.reload()
    problem = directory.sign_in_problem(row)
    assert problem["status"] == "portal_error" and problem["instead"] == []


def test_a_printed_letter_does_not_stay_behind_for_the_next_print():
    letter = (STATIC / "records-request.js").read_text()
    picker = (STATIC / "sources.js").read_text()
    assert "export function clearPrintedLetter" in letter
    assert "onClose: clearPrintedLetter" in picker
