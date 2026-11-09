"""
Rebuilds the vendor directories in ``app/data/institutions/`` from each vendor's published endpoint list.

    ./run.sh directory                    # refresh every vendor below
    ./run.sh directory epic oracle        # just these
    python scripts/refresh_directory.py epic --source brands=brands.json --source r4=r4.json   # from saved copies
    python scripts/refresh_directory.py --tidy-only        # apply the rules below to the files as they are, downloading nothing
    python scripts/refresh_directory.py --summarize        # what changed since the last commit, as Markdown (weekly job)
    python scripts/refresh_directory.py --check-fresh 21   # fail when the sign-in check is older than this many days

| Vendor | File | Source (public, no sign-in) |
|---|---|---|
| Epic | ``epic.json`` | **Brands bundle** (open.epic.com/Endpoints/Brands, ~95 MB): each patient-facing brand with its endpoint, and every facility (``partOf`` the brand) with its city and state. It is Epic's current list; the **legacy R4 list** (open.epic.com/Endpoints/R4) only fills in endpoints no brand uses yet. |
| Oracle Health | ``oracle.json`` | Millennium patient R4 endpoints (github.com/oracle-samples/ignite-endpoints). |
| eClinicalWorks | ``ecw.json`` | fhir.eclinicalworks.com/ecwopendev/global/practiceList. Its addresses use the provider host; patient apps must use ``fhir4.healow.com`` (the other host answers 403 before any sign-in page), so the host is rewritten. Test and training databases are dropped. |
| TruBridge | ``trubridge.json`` | TruBridge's production endpoint directory (thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint): each facility with its endpoint, referenced by ``urn:uuid`` full URL. Training facilities are dropped. |
| Greenway | ``greenway.json`` | Greenway's service base URL bundle: each practice that turned on patient API access. Test systems are dropped. |
| ModMed | ``modmed.json`` | ModMed's Endpoint searches (EMA and gGastro), paged, with each endpoint's managing practice. One endpoint (and sign-in) per practice location. Slow: about 50 pages. |
| NextGen | ``nextgen.json`` | NextGen Enterprise's service base bundle: every practice shares the national patient endpoint, so the practices are only search entries. Placeholder and test practices are dropped. NextGen Office (fhir.meditouchehr.com) registers separately and isn't included. |
| Practice Fusion | ``practicefusion.json`` | Practice Fusion's service base URLs: each practice's *Patient Access* endpoint (api.patientfusion.com, or the FollowMyHealth-backed …/fhir/fmh/r4/v1/), not its provider endpoint. |
| MEDHOST | ``medhost.json`` | MEDHOST's JSON list of facility name, NPI and service base URL. |

athenahealth (one national endpoint) and the SMART demo are hand-maintained in ``curated.json``.

Existing ids are kept (matched by name, then address) so saved connections still resolve; an id whose entry merged into
another stays resolvable through ``former_ids``. ``featured`` flags carry over. An address alone only identifies a listing
when no other listing shares it: 4,000 NextGen practices sit on one address, so there it takes a similar name as well.

Every vendor's list then goes through the same rules (``tidy``): test systems, vendor pods and placeholders are left out,
addresses must be https with a real host, names and places are written one way, and two rows with one name and address
become one. Epic's facility names and towns go to ``app/data/facilities.tsv``, which only search reads, so that someone
can find Mass General Brigham by typing Brigham and Women's.

A vendor whose download fails, or whose list shrinks by half, keeps its current file: the others carry on, and the run
exits 1 at the end. Only the standard library is used, so this runs before anything is installed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = REPO_ROOT / "app" / "data" / "institutions"
FACILITIES = REPO_ROOT / "app" / "data" / "facilities.tsv"
STATUS_FILE = REPO_ROOT / "app" / "data" / "sign_in_status.json"
MAX_PLACES = 8
MIN_KEPT = 0.5          # a list that shrinks more than this is a broken feed, not a wave of closures
MOVES_FOR_REVIEW = 10   # same host, new path: routine in ones and twos, odd in bulk
SIGN_INS_LISTED = 40    # of each kind of failed sign-in: GitHub refuses a pull request's text over 65,536 characters

EPIC_BRANDS_URL = "https://open.epic.com/Endpoints/Brands"
EPIC_R4_URL = "https://open.epic.com/Endpoints/R4"
ORACLE_URL = ("https://raw.githubusercontent.com/oracle-samples/ignite-endpoints/refs/heads/main/"
              "oracle_health_fhir_endpoints/millennium_patient_r4_endpoints.json")
ECW_URL = "https://fhir.eclinicalworks.com/ecwopendev/global/practiceList"
ECW_PROVIDER_HOST, ECW_PATIENT_HOST = "fhir4.eclinicalworks.com", "fhir4.healow.com"
TRUBRIDGE_URL = "https://thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint"
GREENWAY_URL = "https://fhir-servicebaseurl.fhirhlprod.greenwayhealth.com/servicebundle.json"
MODMED_URL = ("https://public-api.mmi.prod.fhir.ema-api.com/fhir/r4/Endpoint"
              "?_include=Endpoint:managingOrganization&connection-type=hl7-fhir-rest")
MODMED_GASTRO_URL = ("https://public-api.gastro.prod.fhir.ema-api.com/fhir/r4/Endpoint"
                     "?_include=Endpoint:managingOrganization&connection-type=hl7-fhir-rest")
NEXTGEN_URL = "https://services.fhir.nextgen.com/FhirServiceBaseBundle-R4"
PRACTICE_FUSION_URL = "https://www.practicefusion.com/assets/static_files/ServiceBaseURLs.json"
MEDHOST_URL = "https://api.mhdi10xasayd.com/medhost-developer-composition/v1/fhir-base-urls.json"
# NextGen's bundle carries placeholders ("A003", "002", "Person") and demo practices ("*NextGen Family Practice", "ZZ…"),
# and each enterprise has its "Default Practice" and "Model Practice".
NEXTGEN_PLACEHOLDER = re.compile(r"^(?:[A-Z]?\d+|person|\*.*|zz.*|.*\bzee\b.*|.*\bdefault\b.*|z ?default.*|model practice(?: \d+)?)$", re.I)
# Whole words in any case. Then what test systems look like when run together with a name, in the capitals that tell
# them apart from real ones: TestFamily and LLCTEST1, but not Testa, Testosterone or Neurotesting. A name all in capitals
# hides those capitals, and a row left out here is never seen again, so only what can't be a word counts: TESTQA and
# DEMOTEST, but not TESTA or FASTEST.
TEST_NAME = re.compile(
    r"\b(?i:test|tests|tst|sandbox|staging)\b|\b(?i:demo)\b|\bTRAIN\b|\b(?i:train)$|\b(?i:training)\b|(?i:test ?db)|(?i:-test\b)|----"
    r"|\b(?i:test|demo)\d+\b|^[Tt]est(?=[A-Z0-9_])|^TEST(?=[a-z0-9_])|[a-z]TEST(?![A-Z])|[a-z]Test\b|_(?i:test)|(?i:testing)[_\d]"
    r"|^TEST(?:QA|EMR|EHR|DB|SITE|PRACTICE|FACILITY|FAMILY|CLINIC|HOSPITAL|MEDICAL|HEALTH|LAB|PROVIDER|USER|CLIENT|ACCOUNT|LOCAL|PROD|DEPT)"
    r"|[A-Z]TEST\d|(?:DEMO|QA|UAT|DEV|PROD|EMR|EHR|LLC|INC)TEST\b"
    r"|^Test\w*local$|^(?i:testtest|prodtest|automatedtesting|qaproduction)$|InternalTest|^Train(?=[A-Z]|\s*-)"
    r"|^QA\b|\bQA\d*$|\b(?i:z?default)\b")
# The pods vendors run their own software on: "ASP41 POD3", "ECWINDQAPOD1 RTREDE", ModMed's "pod 10 admin" and "ophthpod1".
VENDOR_POD = re.compile(
    r"\bASP\d{2}|QAPOD\d"
    r"|^(?:(?:ef|ea|ad|me|vw|ophth) ?)?pod ?\d+\b.*$|^(?:(?:ef|ea|ad|me|vw|ophth) ?)?pod ?\d+(?:admin|test|qa|ophth)\w*$"
    r"|^pod ?\d*(?:ophth|qa)\w*$|^(?:ef|ea|ad|me|vw|ophth)?pod$|^(?:ef|ea|ad|me|vw|ophth)pod\d*\w*$"
    r"|^(?:application )?administrator$|^admin(?: firm)?$|\bSFTP\b"
    # The vendors' own systems, listed at their head offices.
    r"|^internal(?: -| sam\b)|^(?:ms|my)sql\b|^sales-|^dev-|^prod testing\b|^apu testing$|^ambient listening testing$|^fhir medical practice$"
    r"|\bdry run\b|^enterprise chart\b|^g2 - tin\b|^alpha (?:enterprise|environment) practice\b"
    r"|^nextgen (?:ambulatory|behavioral|cardiology|ophthalmology|specialty|chc|ugm)\b"
    r"|^(?:practice \d+|master practice|standard practice|chc practice|optical practice \d+)$", re.I)
# Retired, the way vendors mark it: "Z Retired Open MRI", "zzzJordan and Associates Office", "CVS - ZZZNJ22052", "... MD INACTIVE".
# NextGen's names stop at 40 characters, so a long one loses letters from the word: "Z RetEast Bay Medical Imaging Center LLC".
RETIRED_NAME = re.compile(r"^z+ ?retired|^(?-i:Z Re(?:t|ti|tir|tire|tired)?(?=[A-Z]))|^zzz|\bzzz[a-z]{2}\d|\binactive$", re.I)
# In an address's first label, where a name can't be judged: bellinghamentsandbox, efpod03test04, lagastro-test, testapi-sdg.
TEST_HOST = re.compile(r"sandbox|(?<!la)(?<!grea)(?<!fas)(?<!con)(?<!at)(?<!pro)(?<!de)(?<!brigh)(?<!smar)test\d*$|^test|-test|test-|testpod"
                       r"|\d+admin$|^appadmin$|demo\d*$|^demo|-demo|-qa\d*$")

# Intermediate certificates a vendor's server leaves out of its handshake. Browsers fetch a missing one themselves and
# Python doesn't, so the download fails with "unable to get local issuer certificate". Each is public, and is only a link
# in the chain: a server's certificate is still checked all the way to a root the system already trusts.
# - open.epic.com, from November 2026: DigiCert Global G2 TLS RSA SHA256 2020 CA1, issued by DigiCert Global Root G2,
#   valid until 29 March 2031 (http://cacerts.digicert.com/DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt).
MISSING_INTERMEDIATES = """\
-----BEGIN CERTIFICATE-----
MIIEyDCCA7CgAwIBAgIQDPW9BitWAvR6uFAsI8zwZjANBgkqhkiG9w0BAQsFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBH
MjAeFw0yMTAzMzAwMDAwMDBaFw0zMTAzMjkyMzU5NTlaMFkxCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxMzAxBgNVBAMTKkRpZ2lDZXJ0IEdsb2Jh
bCBHMiBUTFMgUlNBIFNIQTI1NiAyMDIwIENBMTCCASIwDQYJKoZIhvcNAQEBBQAD
ggEPADCCAQoCggEBAMz3EGJPprtjb+2QUlbFbSd7ehJWivH0+dbn4Y+9lavyYEEV
cNsSAPonCrVXOFt9slGTcZUOakGUWzUb+nv6u8W+JDD+Vu/E832X4xT1FE3LpxDy
FuqrIvAxIhFhaZAmunjZlx/jfWardUSVc8is/+9dCopZQ+GssjoP80j812s3wWPc
3kbW20X+fSP9kOhRBx5Ro1/tSUZUfyyIxfQTnJcVPAPooTncaQwywa8WV0yUR0J8
osicfebUTVSvQpmowQTCd5zWSOTOEeAqgJnwQ3DPP3Zr0UxJqyRewg2C/Uaoq2yT
zGJSQnWS+Jr6Xl6ysGHlHx+5fwmY6D36g39HaaECAwEAAaOCAYIwggF+MBIGA1Ud
EwEB/wQIMAYBAf8CAQAwHQYDVR0OBBYEFHSFgMBmx9833s+9KTeqAx2+7c0XMB8G
A1UdIwQYMBaAFE4iVCAYlebjbuYP+vq5Eu0GF485MA4GA1UdDwEB/wQEAwIBhjAd
BgNVHSUEFjAUBggrBgEFBQcDAQYIKwYBBQUHAwIwdgYIKwYBBQUHAQEEajBoMCQG
CCsGAQUFBzABhhhodHRwOi8vb2NzcC5kaWdpY2VydC5jb20wQAYIKwYBBQUHMAKG
NGh0dHA6Ly9jYWNlcnRzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydEdsb2JhbFJvb3RH
Mi5jcnQwQgYDVR0fBDswOTA3oDWgM4YxaHR0cDovL2NybDMuZGlnaWNlcnQuY29t
L0RpZ2lDZXJ0R2xvYmFsUm9vdEcyLmNybDA9BgNVHSAENjA0MAsGCWCGSAGG/WwC
ATAHBgVngQwBATAIBgZngQwBAgEwCAYGZ4EMAQICMAgGBmeBDAECAzANBgkqhkiG
9w0BAQsFAAOCAQEAkPFwyyiXaZd8dP3A+iZ7U6utzWX9upwGnIrXWkOH7U1MVl+t
wcW1BSAuWdH/SvWgKtiwla3JLko716f2b4gp/DA/JIS7w7d7kwcsr4drdjPtAFVS
slme5LnQ89/nD/7d+MS5EHKBCQRfz5eeLjJ1js+aWNJXMX43AYGyZm0pGrFmCW3R
bpD0ufovARTFXFZkAdl9h6g4U5+LXUZtXMYnhIHUfoyMo5tS58aI7Dd8KvvwVVo4
chDYABPPTHPbqjc1qCmBaZx2vN4Ye5DUys/vZwP9BFohFrH/6j/f3IL16/RZkiMN
JCqVJUzKoZHm1Lesh3Sz8W2jmdv51b2EQJ8HmA==
-----END CERTIFICATE-----
"""


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=MISSING_INTERMEDIATES)
    # Python 3.13 and later would otherwise stop at an intermediate it's given, as if it were a root.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return context


TLS = tls_context()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> Any:
    """The document at ``url``; a paged FHIR search (ModMed's) is followed to its last page and returned as one Bundle."""
    print(f"Downloading {url} …", file=sys.stderr)
    first: Any = None
    next_url: Optional[str] = url
    while next_url:
        # MEDHOST serves its list as a download and answers a plain "application/json" with an error.
        req = Request(next_url, headers={"Accept": "application/json, */*;q=0.5", "User-Agent": "SyntropyHealth directory refresh"})
        with urlopen(req, timeout=600, context=TLS) as resp:  # noqa: S310 - https URLs from the vendor's own list
            page = json.load(resp)
        if first is None:
            first = page
        elif page.get("entry"):
            first.setdefault("entry", []).extend(page["entry"])
        next_url = (next((l.get("url") for l in page.get("link") or [] if l.get("relation") == "next"), None)
                    if isinstance(page, dict) and page.get("resourceType") == "Bundle" else None)
    return first


def address_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def name_key(name: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ")))


def similarity(name: str, entry: dict[str, Any]) -> float:
    """Word overlap between an old entry's name and a new one (its own name or its network's)."""
    a = name_key(name)
    return max(len(a & name_key(n)) / (len(a | name_key(n)) or 1) for n in (entry["name"], entry.get("network") or ""))


def slug(prefix: str, name: str) -> str:
    return prefix + "-".join(re.findall(r"[a-z0-9]+", name.lower()))


STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "guam": "GU",
    "virgin islands": "VI", "us virgin islands": "VI", "american samoa": "AS", "northern mariana islands": "MP",
}
CODES = frozenset(STATE_CODES.values())


def state_code(text: str) -> Optional[str]:
    """TX for 'TX', 'Texas' and what vendors' fixed-width fields leave of a long name ('Flori', 'New Y')."""
    text = " ".join(text.split()).lower()
    if text.upper() in CODES:
        return text.upper()
    if len(text) >= 3:
        found = {code for name, code in STATE_CODES.items() if name.startswith(text)}
        if len(found) == 1:
            return found.pop()
    return None


def city_case(city: str) -> str:
    """Cities that came all in capitals or lower case, written normally. Any other mix is the vendor's own."""
    city = " ".join(city.split()).strip(" ,")
    if city != city.upper() and city != city.lower():
        return city

    def word(m: re.Match[str]) -> str:
        w = m.group(0).lower()
        if w in ("of", "the") and m.start():
            return w
        return "Mc" + w[2:].capitalize() if w.startswith("mc") and len(w) > 3 else w.capitalize()

    return re.sub(r"[^\W\d_]+", word, city)


def location(places: Counter[tuple[str, str]]) -> Optional[str]:
    """'San Jose, CA' for a single-city organization, otherwise its states by size ('WA, OR, MT')."""
    if not places:
        return None
    states: Counter[str] = Counter()
    for (_, state), n in places.items():
        states[state] += n
    (city, state), top = places.most_common(1)[0]
    if len(states) == 1 and top >= 0.6 * sum(places.values()):
        return f"{city}, {state}" if city else state
    return ", ".join(s for s, _ in states.most_common(4)) + (" and more" if len(states) > 4 else "")


def tidy_location(loc: Optional[str]) -> Optional[str]:
    """'Lady Lake, Flori' → 'Lady Lake, FL'; a tail that names no place ('Anchorage, 99515') goes; other countries stay."""
    if not loc or not loc.strip():
        return None
    loc = " ".join(loc.split())
    head, comma, tail = loc.rpartition(", ")
    if not comma:
        return state_code(loc) or city_case(loc)
    if all(p.removesuffix(" and more") in CODES for p in loc.split(", ")):
        return loc                                              # states, by size
    city = city_case(head.strip(". "))
    code = state_code(tail)
    if not code and (doubled := re.match(r"^([A-Za-z]{2})(?:-[A-Za-z]{2}|[A-Z]{2}|[a-z])$", tail)) and doubled[1].upper() in CODES:
        code = doubled[1].upper()                               # 'NJ-Ne', 'TXTX', 'FLv'
    if code:
        return f"{city}, {code}" if city else code
    if tail.isdigit() or len(tail) == 1 or tail.upper() in ("NA", "N/A", "XX", "UNKNOWN", "US", "USA"):
        return city or None
    return f"{city}, {tail}"


def first_place(org: dict[str, Any]) -> Counter[tuple[str, str]]:
    for addr in org.get("address") or []:
        if addr.get("state"):
            state = addr["state"].strip()
            return Counter({(city_case(addr.get("city") or ""), state_code(state) or state): 1})
    return Counter()


def resources(bundle: dict[str, Any], rtype: str) -> list[dict[str, Any]]:
    return [e["resource"] for e in bundle.get("entry", []) if e.get("resource", {}).get("resourceType") == rtype]


def ref_id(ref: str) -> str:
    return re.split(r"[:/]", ref)[-1]


def by_full_url(bundle: dict[str, Any], rtype: str) -> dict[str, dict[str, Any]]:
    """Resources keyed by every way a reference may name them: ``urn:uuid:…`` full URL, ``Type/id`` and bare id."""
    out: dict[str, dict[str, Any]] = {}
    for e in bundle.get("entry", []):
        r = e.get("resource") or {}
        if r.get("resourceType") == rtype:
            for key in (e.get("fullUrl"), f"{rtype}/{r.get('id')}", r.get("id")):
                if key:
                    out[key] = r
    return out


# Some vendors list practices in capitals ("BANNER HEALTH", "ROUSE FAMILY MEDICAL CLINIC, P.A.") or in lower case. They're
# written the way the rest of the directory writes names, keeping what reads as letters: business suffixes and credentials
# (LLC, MD), initials (GI, CTMC) and names with no ordinary word in them (AACI, CHCO NAPA), which are left as they are.
_WORD = re.compile(r"[A-Za-z]+")
_KEEP_CAPS = {"LLC", "PLLC", "PLC", "LLP", "PC", "PA", "PSC", "SC", "MD", "DO", "DDS", "DMD", "DPM", "OD", "NP", "FNP", "APRN",
              "DPT", "MPH", "RN", "CNM", "ENT", "OB", "GYN", "OBGYN", "HIV", "ADHD", "ICU", "USA", "MRI", "DBA", "II", "III", "IV", "IT",
              "HCA", "UPMC", "RHC", "FQHC", "LP", "US",
              # states whose codes aren't also words
              "AZ", "AR", "CA", "CT", "DC", "FL", "GA", "IA", "IL", "KS", "KY", "MI", "MN", "MO", "MS", "NC", "ND", "NE", "NH",
              "NJ", "NM", "NV", "NY", "RI", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY"}
_ABBREVIATIONS = {"LTD": "Ltd", "JR": "Jr", "SR": "Sr", "DR": "Dr", "ST": "St", "MT": "Mt", "FT": "Ft"}
_SMALL_WORDS = {"of", "and", "the", "at", "for", "in", "on", "by", "to", "de", "la", "del", "y", "w"}
# Enough to recognize a name when no directory is at hand; the directory's own words add the rest.
_ORDINARY_WORDS = frozenset({"HOSPITAL", "MEDICAL", "MEMORIAL", "CLINIC", "HEALTH", "CENTER", "COUNTY", "COMMUNITY", "REGIONAL", "FAMILY",
                             "PRACTICE", "CARE", "HEALTHCARE", "GENERAL", "CHILDREN", "PEDIATRICS", "SERVICES", "SYSTEM", "ASSOCIATES"})


def vocabulary(names: Iterable[str]) -> frozenset[str]:
    """Words the directory writes in ordinary case somewhere ("Medical", "Clinic", "Bay"), in capitals."""
    seen: Counter[str] = Counter()
    for name in names:
        if name != name.upper():
            seen.update(w.upper() for w in _WORD.findall(name) if w[0].isupper() and w[1:].islower())
    return frozenset(w for w, n in seen.items() if n >= 2) | _ORDINARY_WORDS


def readable_name(name: str, vocab: frozenset[str] = _ORDINARY_WORDS) -> str:
    """``name`` as the directory shows it: one written all in capitals or all in lower case in title case, when it has
    ordinary words. Any other name is left as the vendor wrote it."""
    if name != name.lower():
        if name != name.upper():
            return name
    caps = name.upper()
    if not any(len(w) >= 4 and w in vocab for w in _WORD.findall(caps)):
        return name
    first = True

    def word(m: re.Match) -> str:
        nonlocal first
        w, at_start = m.group(0), first
        first = False
        if m.start() and caps[m.start() - 1].isdigit() and w in ("ST", "ND", "RD", "TH"):
            return w.lower()                                    # 2ND
        if m.start() and caps[m.start() - 1] == "'" and w == "S":
            return "s"                                          # CHILDREN'S
        if w in _ABBREVIATIONS:
            return _ABBREVIATIONS[w]
        if w.lower() in _SMALL_WORDS and not at_start:
            return w.lower()
        if w in _KEEP_CAPS or (len(w) <= 2 and w not in vocab) or not re.search(r"[AEIOUY]", w):
            return w                                            # initials (GI, CTMC, DPC)
        return w.capitalize()

    return _WORD.sub(word, caps)


_STRAY_START = " ,;:.-–—/\\|*_'\"~"
_STRAY_END = " ,;:-–—/\\|*_\"~"


def clean_name(name: str, vocab: frozenset[str] = _ORDINARY_WORDS) -> str:
    """Entities unescaped, spaces collapsed, stray punctuation trimmed, and ordinary case."""
    name = " ".join(html.unescape(name).split()).replace(" ,", ",")
    name = name.lstrip(_STRAY_START).rstrip(_STRAY_END)
    if name.endswith("(") or name.startswith(")"):
        name = name.strip("() ")
    return readable_name(name, vocab)


_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.I)


def clean_url(url: str) -> Optional[str]:
    """The address when it's https with a real host, else None. A vendor's error can't send someone's sign-in to plain
    http or a malformed host, and a space right after the scheme is only a typo."""
    url = re.sub(r"^(https?://)\s+", r"\1", (url or "").strip(), flags=re.I)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host or not port_ok or parts.username or not _HOST.match(parts.netloc.split(":")[0]):
        return None
    return url


def is_listing(name: str, url: str) -> bool:
    """False for test databases, vendor pods and placeholders, judged by name and by the address's first label."""
    if (TEST_NAME.search(name) or VENDOR_POD.search(name) or RETIRED_NAME.search(name) or len(name) < 3
            or name.lower() in ("client", "person", "mmbatchload")):
        return False
    if re.search(r"^model practice\b", name, re.I):
        return False
    label = (urlsplit(url.strip().replace(" ", "")).hostname or "").split(".")[0].lower()
    return not TEST_HOST.search(label)


def _better(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Of two rows for one listing, the one that says more: where it is, then how many towns and facilities."""
    def rank(e: dict[str, Any]) -> tuple:
        return bool(e.get("location")), len(e.get("facilities") or ()), len(e.get("places") or ()), bool(e.get("network"))
    return b if rank(b) > rank(a) else a


def collapse_duplicates(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per name and address. A row with no location folds into one that has it; rows naming different places
    stay apart, since on a shared address (NextGen's) the place is what tells two practices apart."""
    groups: dict[tuple[frozenset[str], str], list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        groups[name_key(e["name"]), address_key(e["fhir_base_url"])].append(e)
    out = []
    for rows in groups.values():
        placed: dict[str, dict[str, Any]] = {}
        unplaced = None
        for e in rows:
            where = (e.get("location") or "").lower()
            if where:
                placed[where] = _better(placed[where], e) if where in placed else e
            else:
                unplaced = _better(unplaced, e) if unplaced else e
        out.extend(placed.values() or [unplaced])
    return out


def tidy(entries: list[dict[str, Any]], vocab: frozenset[str] = _ORDINARY_WORDS) -> list[dict[str, Any]]:
    """The rules every vendor's list goes through. It may run again on its own output and change nothing."""
    kept = []
    for e in entries:
        name = clean_name(e["name"], vocab)
        url = clean_url(e["fhir_base_url"])
        if not url:
            print(f"  left out (not an https address with a real host): {e['name']}: {e['fhir_base_url']}", file=sys.stderr)
        elif not is_listing(name, url):
            print(f"  left out (a test system or placeholder): {e['name']}", file=sys.stderr)
        else:
            e.update(name=name, fhir_base_url=url)
            if "location" in e:
                e["location"] = tidy_location(e["location"])
                if not e["location"]:
                    del e["location"]
            if e.get("places"):
                e["places"] = _tidy_places(e["places"])
            kept.append(e)
    return collapse_duplicates(kept)


def _tidy_places(places: list[str]) -> list[str]:
    """'CHICAGO IL' and 'Chicago IL' are one town."""
    seen = {}
    for place in places:
        city, _, state = place.rpartition(" ")
        tidied = f"{city_case(city)} {state}" if city else place
        seen.setdefault(tidied, None)
    return list(seen)


def directory_entries(bundle: dict[str, Any], platform: str,
                      use: Callable[[dict[str, Any]], bool] = lambda endpoint: True) -> list[dict[str, Any]]:
    """Organizations and their endpoints from a vendor's FHIR endpoint directory; ``use`` picks among an
    organization's endpoints (Practice Fusion lists a provider and a patient one)."""
    endpoints = by_full_url(bundle, "Endpoint")
    out = []
    for org in resources(bundle, "Organization"):
        name = (org.get("name") or "").strip().lstrip("*").strip()
        endpoint = next((ep for e in org.get("endpoint") or [] if (ep := endpoints.get(e.get("reference", ""))) and use(ep)),
                        None)
        if (not endpoint or not endpoint.get("address") or endpoint.get("status", "active") != "active"
                or not org.get("active", True) or not name or TEST_NAME.search(name)):
            continue
        out.append({"name": readable_name(name), "location": location(first_place(org)), "platform": platform,
                    "fhir_base_url": endpoint["address"].strip()})
    return out


# ---------------------------------------------------------------------------
# Vendors: each returns entries without ids
# ---------------------------------------------------------------------------

def epic_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = sources["brands"]
    endpoints = {r["id"]: r for r in resources(bundle, "Endpoint") if r.get("status", "active") == "active"}
    orgs = [r for r in resources(bundle, "Organization") if r.get("active", True)]
    places: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    facilities: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for org in orgs:
        if org.get("partOf"):
            brand = ref_id(org["partOf"]["reference"])
            where = first_place(org)
            places[brand].update(where)
            city, state = next(iter(where), ("", ""))
            name = " ".join(html.unescape(org.get("name") or "").split())
            if name and not TEST_NAME.search(name) and not RETIRED_NAME.search(name):
                facilities[brand].setdefault(name, (city, state))
    brands = []
    for org in orgs:
        if org.get("partOf"):
            continue
        endpoint = next((endpoints.get(ref_id(e["reference"])) for e in org.get("endpoint") or []), None)
        if not endpoint or not endpoint.get("address"):
            continue
        brand_places = places.get(org["id"], Counter())
        cities: Counter[str] = Counter()
        for (city, state), n in brand_places.items():
            if city:
                cities[f"{city} {state}"] += n
        brands.append({
            "name": org["name"].strip(), "location": location(brand_places), "platform": "epic",
            "fhir_base_url": endpoint["address"].strip(),
            "places": [c for c, _ in cities.most_common(MAX_PLACES)],
            "network": ((endpoint.get("managingOrganization") or {}).get("display") or "").strip() or None,
            "facilities": _facility_lines(org["name"], facilities.get(org["id"], {}), brand_places),
        })

    # Legacy endpoints only where no brand already covers the address or the name.
    brand_addrs = {address_key(b["fhir_base_url"]) for b in brands}
    brand_names = {name_key(b["name"]) for b in brands}
    legacy = [{"name": r["name"].strip(), "platform": "epic", "fhir_base_url": r["address"].strip()}
              for r in resources(sources["r4"], "Endpoint") if r.get("address") and r.get("status", "active") == "active"]
    return brands + [l for l in legacy
                     if address_key(l["fhir_base_url"]) not in brand_addrs and name_key(l["name"]) not in brand_names]


def _facility_lines(brand: str, facilities: dict[str, tuple[str, str]], places: Counter[tuple[str, str]]) -> list[str]:
    """``name|city|state`` for each facility and, with no name, each town no facility line mentions. A facility named just
    like its brand says nothing more than its town does."""
    lines = {}
    towns = set()
    same = name_key(brand)
    for name, (city, state) in sorted(facilities.items(), key=lambda kv: kv[0].lower()):
        if name_key(name) != same:
            lines[f"{name}|{city}|{state}"] = None
            towns.add((city, state))
    for (city, state), _ in sorted(places.items()):
        if city and (city, state) not in towns:
            lines[f"|{city}|{state}"] = None
    return list(lines)


def oracle_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = sources["endpoints"]
    endpoints = {r["id"]: r for r in resources(bundle, "Endpoint") if r.get("status", "active") == "active"}
    out = []
    for org in resources(bundle, "Organization"):
        endpoint = next((endpoints.get(ref_id(e["reference"])) for e in org.get("endpoint") or []), None)
        if endpoint and endpoint.get("address") and org.get("name"):
            out.append({"name": org["name"].strip(), "location": location(first_place(org)), "platform": "cerner",
                        "fhir_base_url": endpoint["address"].strip()})
    return out


def ecw_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = sources["practices"]
    endpoints = {r["id"]: r for r in resources(bundle, "Endpoint")}
    out = []
    for org in resources(bundle, "Organization"):
        endpoint = next((endpoints.get(ref_id(e["reference"])) for e in org.get("endpoint") or []), None)
        name = (org.get("name") or "").strip()
        if not endpoint or not endpoint.get("address") or not name or TEST_NAME.search(name):
            continue
        out.append({"name": name, "location": location(first_place(org)), "platform": "healow",
                    "fhir_base_url": endpoint["address"].strip().replace(f"//{ECW_PROVIDER_HOST}/", f"//{ECW_PATIENT_HOST}/"),
                    "code": endpoint["id"]})
    return out


def trubridge_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    return directory_entries(sources["facilities"], "trubridge")


def greenway_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    return directory_entries(sources["practices"], "greenway")


def modmed_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    """ModMed lists endpoints, each managed by a practice (one practice can have several locations)."""
    out, seen = [], set()
    for bundle in (sources["endpoints"], sources["gastro"]):
        orgs = by_full_url(bundle, "Organization")
        for endpoint in resources(bundle, "Endpoint"):
            org = orgs.get((endpoint.get("managingOrganization") or {}).get("reference", "")) or {}
            name = ((endpoint.get("name") or org.get("name") or "")).strip().strip('"').strip()
            address = (endpoint.get("address") or "").strip()
            # gGastro lists unconfigured practices as "Client", with no address or NPI.
            if (not address or address_key(address) in seen or endpoint.get("status", "active") != "active"
                    or len(name) < 3 or name.lower() == "client" or TEST_NAME.search(name)):
                continue
            seen.add(address_key(address))
            out.append({"name": readable_name(name), "location": location(first_place(org)), "platform": "modmed",
                        "fhir_base_url": address})
    return out


def nextgen_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = sources["practices"]
    real = {"entry": [e for e in bundle.get("entry", []) if (e.get("resource") or {}).get("resourceType") != "Organization"
                      or not NEXTGEN_PLACEHOLDER.match((e["resource"].get("name") or "").strip())]}
    named = directory_entries(real, "nextgen")
    # The same practice can be listed more than once; one entry per name and place.
    return list({(e["name"].lower(), e.get("location")): e for e in named}.values())


def practicefusion_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    return directory_entries(sources["practices"], "practicefusion",
                             use=lambda ep: (ep.get("name") or "").lower() == "patient access")


def medhost_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": readable_name(f["facilityName"].strip()), "platform": "medhost",
             "fhir_base_url": f["serviceBaseUrl"].strip()}
            for f in sources["facilities"]
            if f.get("facilityName") and f.get("serviceBaseUrl") and not TEST_NAME.search(f["facilityName"])]


# name: (file, id prefix, sources {key: url}, builder)
VENDORS: dict[str, tuple[str, str, dict[str, str], Callable[[dict[str, Any]], list[dict[str, Any]]]]] = {
    "epic": ("epic.json", "epic-", {"brands": EPIC_BRANDS_URL, "r4": EPIC_R4_URL}, epic_entries),
    "oracle": ("oracle.json", "oracle-", {"endpoints": ORACLE_URL}, oracle_entries),
    "ecw": ("ecw.json", "ecw-", {"practices": ECW_URL}, ecw_entries),
    "trubridge": ("trubridge.json", "trubridge-", {"facilities": TRUBRIDGE_URL}, trubridge_entries),
    "greenway": ("greenway.json", "greenway-", {"practices": GREENWAY_URL}, greenway_entries),
    "modmed": ("modmed.json", "modmed-", {"endpoints": MODMED_URL, "gastro": MODMED_GASTRO_URL}, modmed_entries),
    "nextgen": ("nextgen.json", "nextgen-", {"practices": NEXTGEN_URL}, nextgen_entries),
    "practicefusion": ("practicefusion.json", "practicefusion-", {"practices": PRACTICE_FUSION_URL},
                       practicefusion_entries),
    "medhost": ("medhost.json", "medhost-", {"facilities": MEDHOST_URL}, medhost_entries),
}


# ---------------------------------------------------------------------------
# Merge with the current file
# ---------------------------------------------------------------------------

def merge(existing: list[dict[str, Any]], entries: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    by_name: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    by_addr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_name[name_key(e["name"])].append(e)
        by_addr[address_key(e["fhir_base_url"])].append(e)

    was_at: Counter[str] = Counter(address_key(old.get("fhir_base_url") or "") for old in existing)
    named: Counter[frozenset[str]] = Counter(name_key(old["name"]) for old in existing)

    # Addresses of old entries may predate a host rewrite; compare paths too.
    def at_address(old: dict[str, Any]) -> list[dict[str, Any]]:
        key = address_key(old.get("fhir_base_url") or "")
        return by_addr.get(key, []) or by_addr.get(key.replace(ECW_PROVIDER_HOST, ECW_PATIENT_HOST), [])

    # An address shared with others (NextGen's one for 4,000 practices, an Epic host's for its clients) can't say which
    # listing this was, so it takes a similar name too. An address of its own is the same place, whatever it's called now.
    def addr_matches(old: dict[str, Any]) -> list[dict[str, Any]]:
        found = at_address(old)
        if len(found) <= 1 and was_at[address_key(old.get("fhir_base_url") or "")] <= 1:
            return found
        return [c for c in found if similarity(old["name"], c) >= 0.5]

    # Name and address together first (two brands can share a name: Baptist Health in Alabama and in Arkansas), then
    # names (a shared endpoint shouldn't hand one brand's id to another), then addresses.
    def same_entry(old: dict[str, Any]) -> list[dict[str, Any]]:
        return [c for c in by_name.get(name_key(old["name"]), []) if c in at_address(old)]

    # The same name elsewhere can be another health system, unless nothing says otherwise.
    def same_name_there(old: dict[str, Any]) -> list[dict[str, Any]]:
        return [c for c in by_name.get(name_key(old["name"]), [])
                if c in at_address(old) or not old.get("location") or not c.get("location")
                or tidy_location(old["location"]) == c["location"]]

    # The only one by that name, then and now, has moved; its towns may have changed along the way.
    def same_name(old: dict[str, Any]) -> list[dict[str, Any]]:
        found = by_name.get(name_key(old["name"]), [])
        return found if len(found) == 1 and named[name_key(old["name"])] == 1 else same_name_there(old)

    kept, pending = 0, list(existing)
    for lookup in (same_entry, same_name, addr_matches):
        rest = []
        for old in pending:
            # Twins on one address (a practice with two sites on NextGen's) each keep their own id, by where they are.
            match = max((c for c in lookup(old) if "id" not in c), default=None,
                        key=lambda c: (similarity(old["name"], c), c.get("location") == tidy_location(old.get("location"))))
            if match:
                match["id"], match["featured"] = old["id"], bool(old.get("featured"))
                match["former_ids"] = list(old.get("former_ids") or [])
                kept += 1
            else:
                rest.append(old)
        pending = rest
    for old in pending:
        target = next(iter(same_name_there(old) + addr_matches(old)), None)
        if target:
            target.setdefault("former_ids", []).extend([old["id"], *(old.get("former_ids") or [])])
            target["featured"] = target.get("featured") or bool(old.get("featured"))
        else:
            print(f"  dropped (no longer published): {old['name']}", file=sys.stderr)

    used = {e["id"] for e in entries if "id" in e}
    for e in entries:
        if "id" not in e:
            base = new_id = prefix + e["code"].lower() if e.get("code") else slug(prefix, e["name"])
            n = 2
            while new_id in used:
                new_id, n = f"{base}-{n}", n + 1
            e["id"] = new_id
            used.add(new_id)
    print(f"  {len(entries)} entries, {kept} existing ids kept", file=sys.stderr)

    # Only non-empty optional fields are written; the portal name comes from the platform preset.
    order = ["id", "name", "location", "platform", "fhir_base_url", "featured", "network", "places", "former_ids"]
    out = []
    for e in sorted(entries, key=lambda e: (e["name"].lower(), e["id"])):
        if e.get("network") == e["name"]:
            e.pop("network")
        out.append({k: e[k] for k in order if k in ("id", "name", "platform", "fhir_base_url") or e.get(k)})
    return out


def write_text(path: Path, text: str) -> None:
    """Replaces ``path`` whole or not at all: a run stopped halfway can't leave half a file for the app to read."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)        # mkstemp's private default would keep the app's own user from reading it
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any, **dump: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, **dump) + "\n")


def write_facilities(path: Path, entries: list[dict[str, Any]], replace_prefix: str = "epic-") -> None:
    """What each listing includes, for search: id, facility, city and state on a line, sorted, so that a week's diff shows
    only what changed and the app can read it a line at a time."""
    try:
        kept = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(replace_prefix)]
    except OSError:
        kept = []
    fresh = [f"{e['id']}\t" + "\t".join(line.rsplit("|", 2)) for e in entries for line in e.get("facilities") or ()]
    write_text(path, "".join(f"{line}\n" for line in sorted([*kept, *fresh])))


def _rows(text: str) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for i in json.loads(text)}


def summarize(root: Path) -> dict[str, Any]:
    """What the files under ``root`` changed since its last commit: a row per file, and why a person should look before
    this reaches anyone, if they should."""
    review: list[str] = []
    rows = []
    moved_same_host = 0
    for path in sorted((root / "app" / "data" / "institutions").glob("*.json")):
        if path.name == "curated.json":
            continue
        shown = subprocess.run(["git", "show", f"HEAD:{path.relative_to(root).as_posix()}"], cwd=root, capture_output=True, text=True)
        old = _rows(shown.stdout) if shown.returncode == 0 else {}
        new = _rows(path.read_text(encoding="utf-8"))
        resolvable = set(new) | {f for i in new.values() for f in i.get("former_ids", ())}
        gone = set(old) - resolvable
        moved = [k for k in old if k in new and address_key(old[k]["fhir_base_url"]) != address_key(new[k]["fhir_base_url"])]
        rows.append((path.name, len(new), len(set(new) - set(old)), len(gone), len(moved)))
        if gone:
            review.append(f"{len(gone)} institutions in {path.name} are no longer published. A saved connection to one keeps "
                          "its address, but the directory can't find it by id any more.")
        for k in moved:
            before, after = urlsplit(old[k]["fhir_base_url"]).hostname, urlsplit(new[k]["fhir_base_url"]).hostname
            if before != after:
                review.append(f"{new[k]['name']} moved from {before} to {after}: new connections will sign in there.")
            else:
                moved_same_host += 1
    if moved_same_host > MOVES_FOR_REVIEW:
        review.append(f"{moved_same_host} listings have a new address on the same host, more than a usual week.")
    return {"rows": rows, "review": review}


def summary_markdown(root: Path, sign_in_report: Optional[Path] = None, failures: Optional[Path] = None) -> tuple[str, int]:
    """The pull request's text, and how many things in it should wait for a person."""
    info = summarize(root)
    lines = ["Rebuilt from each vendor's published endpoint list by the weekly Directory refresh workflow.", "",
             "| File | Institutions | Added | Dropped | New address |", "|---|---|---|---|---|"]
    lines += [f"| {name} | {total} | {added} | {gone} | {moved} |" for name, total, added, gone, moved in info["rows"]]
    if failures and failures.exists() and failures.read_text().strip():
        lines += ["", "**These vendors couldn't be refreshed and keep their last list:**", ""]
        lines += [f"- {line}" for line in failures.read_text().splitlines()]
    if sign_in_report and sign_in_report.exists():
        results = json.loads(sign_in_report.read_text())
        failing = [r for r in results if r["status"] not in ("ok", "blocked", "unclear")]
        lines += ["", f"**Sign-in check:** {len(results) - len(failing)} of {len(results)} servers reach their sign-in "
                      "or couldn't be judged.", ""]
        shown: Counter[str] = Counter()
        for r in sorted(failing, key=lambda r: (r["status"], r["names"][0])):
            shown[r["status"]] += 1
            if shown[r["status"]] <= SIGN_INS_LISTED:
                more = f" (+{len(r['names']) - 1})" if len(r["names"]) > 1 else ""
                lines.append(f"- `{r['status']}` {r['names'][0]}{more} ({r['platform']}): {r['detail'][:160]}")
            elif shown[r["status"]] == SIGN_INS_LISTED + 1:
                rest = sum(1 for f in failing if f["status"] == r["status"]) - SIGN_INS_LISTED
                lines.append(f"- and {rest} more `{r['status']}`, all in sign-in.json in this run's `directory` artifact")
    if info["review"]:
        lines += ["", "**This waits for review before merging:**", ""] + [f"- {why}" for why in info["review"][:50]]
        if len(info["review"]) > 50:
            lines.append(f"- and {len(info['review']) - 50} more")
    return "\n".join(lines) + "\n", len(info["review"])


def check_fresh(path: Path, days: int, today: Optional[str] = None, warn_only: bool = False) -> int:
    """Whether the sign-in warnings people see come from a recent check. 1 when they don't, unless ``warn_only``."""
    try:
        checked = json.loads(path.read_text(encoding="utf-8")).get("checked")
        age = (date.fromisoformat(today or date.today().isoformat()) - date.fromisoformat(checked)).days
    except (OSError, ValueError, TypeError, AttributeError):
        checked, age = None, None
    if age is not None and age <= days:
        print(f"The sign-in check ran {age} days ago.")
        return 0
    when = f"{age} days ago, on {checked}" if age is not None else "at an unknown date"
    level = "warning" if warn_only else "error"
    print(f"::{level}::The sign-in check last ran {when}, so the warnings people see are out of date. Add the DIRECTORY_TOKEN "
          "repository secret if it's missing and run the Directory refresh workflow, then merge its pull request.")
    return 0 if warn_only else 1


def refresh_vendor(vendor: str, saved: dict[str, str], vocab: frozenset[str]) -> None:
    file, prefix, urls, build = VENDORS[vendor]
    print(f"{vendor}:", file=sys.stderr)
    sources = {}
    for key, url in urls.items():
        if key in saved:
            with open(saved[key], encoding="utf-8") as fh:
                sources[key] = json.load(fh)
        else:
            sources[key] = fetch(url)
    path = DIRECTORY / file
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries = tidy(build(sources), vocab)
    if len(entries) < MIN_KEPT * len(existing):
        raise ValueError(f"the list shrank from {len(existing)} to {len(entries)}, so its current file stays")
    merged = merge(existing, entries, prefix)
    if vendor == "epic":
        for e in entries:
            if e.get("facilities"):
                e["facilities"] = _tidy_facilities(e["facilities"], vocab)
        write_facilities(FACILITIES, entries)
    write_json(path, merged, indent=0)
    print(f"  wrote {len(merged)} institutions to {path.relative_to(REPO_ROOT)}", file=sys.stderr)


def _tidy_facilities(lines: list[str], vocab: frozenset[str]) -> list[str]:
    out = {}
    for line in lines:
        name, city, state = line.rsplit("|", 2)
        out[f"{clean_name(name, vocab)}|{city}|{state}"] = None
    return list(out)


def tidy_only(vendors: list[str], vocab: frozenset[str]) -> None:
    """The rules above, on the files as they are."""
    for vendor in vendors:
        file, prefix, _, _ = VENDORS[vendor]
        path = DIRECTORY / file
        existing = json.loads(path.read_text(encoding="utf-8"))
        again = [{k: v for k, v in e.items() if k not in ("id", "featured", "former_ids")} for e in existing]
        merged = merge(existing, tidy(again, vocab), prefix)
        write_json(path, merged, indent=0)
        print(f"{vendor}: {len(existing)} rows became {len(merged)}", file=sys.stderr)


def directory_names() -> list[str]:
    return [i["name"] for path in sorted(DIRECTORY.glob("*.json")) for i in json.loads(path.read_text(encoding="utf-8"))
            if isinstance(i, dict) and i.get("name")]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vendors", nargs="*", help=f"vendors to refresh: {', '.join(VENDORS)} (default: all)")
    parser.add_argument("--source", action="append", default=[], metavar="KEY=FILE",
                        help="use a saved copy of a source instead of downloading it "
                             "(keys: brands, r4, endpoints, practices, facilities, gastro)")
    parser.add_argument("--failures", type=Path, help="write the vendors that couldn't be refreshed to this file")
    parser.add_argument("--tidy-only", action="store_true", help="apply the rules to the files as they are, downloading nothing")
    parser.add_argument("--summarize", action="store_true", help="print what changed since the last commit, as Markdown")
    parser.add_argument("--sign-in-report", type=Path, help="with --summarize: the sign-in check's --json file")
    parser.add_argument("--github-output", type=Path, help="with --summarize: write review=N to this file")
    parser.add_argument("--check-fresh", type=int, metavar="DAYS", help="fail when the sign-in check is older than this")
    parser.add_argument("--warn-only", action="store_true", help="with --check-fresh: warn, and don't fail")
    args = parser.parse_args(argv)
    if args.check_fresh is not None:
        return check_fresh(STATUS_FILE, args.check_fresh, warn_only=args.warn_only)
    if args.summarize:
        body, review = summary_markdown(REPO_ROOT, args.sign_in_report, args.failures)
        print(body)
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as out:
                out.write(f"review={review}\n")
        return 0
    saved = dict(s.split("=", 1) for s in args.source)
    unknown = [v for v in args.vendors if v not in VENDORS]
    if unknown:
        parser.error(f"unknown vendor(s): {', '.join(unknown)}")

    vocab = vocabulary(directory_names())
    if args.tidy_only:
        tidy_only(args.vendors or list(VENDORS), vocab)
        return 0
    failed = []
    for vendor in args.vendors or list(VENDORS):
        try:
            refresh_vendor(vendor, saved, vocab)
        except Exception as exc:  # noqa: BLE001 - one vendor's bad day shouldn't stop the others
            failed.append(f"{vendor}: {type(exc).__name__}: {exc}"[:300])
            print(f"  FAILED {failed[-1]}", file=sys.stderr)
    if args.failures:
        args.failures.write_text("".join(f"{line}\n" for line in failed), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
