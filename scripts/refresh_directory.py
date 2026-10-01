"""
Rebuilds the vendor directories in ``app/data/institutions/`` from each vendor's published endpoint list.

    ./run.sh directory                    # refresh every vendor below
    ./run.sh directory epic oracle        # just these
    python scripts/refresh_directory.py epic --source brands=brands.json --source r4=r4.json   # from saved copies

| Vendor | File | Source (public, no sign-in) |
|---|---|---|
| Epic | ``epic.json`` | **Brands bundle** (open.epic.com/Endpoints/Brands, ~95 MB): each patient-facing brand with its endpoint, and every facility (``partOf`` the brand) with its city and state. It is Epic's current list; the **legacy R4 list** (open.epic.com/Endpoints/R4) only fills in endpoints no brand uses yet. |
| Oracle Health | ``oracle.json`` | Millennium patient R4 endpoints (github.com/oracle-samples/ignite-endpoints). |
| eClinicalWorks | ``ecw.json`` | fhir.eclinicalworks.com/ecwopendev/external/practiceList. Its addresses use the provider host; patient apps must use ``fhir4.healow.com`` (the other host answers 403 before any sign-in page), so the host is rewritten. Test and training databases are dropped. |

athenahealth (one national endpoint), the VA and the SMART demo are hand-maintained in ``curated.json``.

Existing ids are kept (matched by name, then address) so saved connections still resolve; an id whose entry merged into
another stays resolvable through ``former_ids``. ``featured`` flags carry over.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = REPO_ROOT / "app" / "data" / "institutions"
MAX_PLACES = 8

EPIC_BRANDS_URL = "https://open.epic.com/Endpoints/Brands"
EPIC_R4_URL = "https://open.epic.com/Endpoints/R4"
ORACLE_URL = ("https://raw.githubusercontent.com/oracle-samples/ignite-endpoints/refs/heads/main/"
              "oracle_health_fhir_endpoints/millennium_patient_r4_endpoints.json")
ECW_URL = "https://fhir.eclinicalworks.com/ecwopendev/external/practiceList"
ECW_PROVIDER_HOST, ECW_PATIENT_HOST = "fhir4.eclinicalworks.com", "fhir4.healow.com"
TEST_NAME = re.compile(r"\b(test|tests|testing|train|training|demo|sandbox|tst)\b|test ?db|-test\b|----", re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> Any:
    print(f"Downloading {url} …", file=sys.stderr)
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "SyntropyHealth directory refresh"})
    with urlopen(req, timeout=600) as resp:  # noqa: S310 - fixed https URLs
        return json.load(resp)


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


def location(places: Counter[tuple[str, str]]) -> Optional[str]:
    """'San Jose, CA' for a single-city organization, otherwise its states by size ('WA, OR, MT')."""
    if not places:
        return None
    states: Counter[str] = Counter()
    for (_, state), n in places.items():
        states[state] += n
    (city, state), top = places.most_common(1)[0]
    if len(states) == 1 and top >= 0.6 * sum(places.values()):
        return f"{city.title() if city.isupper() else city}, {state}" if city else state
    return ", ".join(s for s, _ in states.most_common(4)) + ("…" if len(states) > 4 else "")


def first_place(org: dict[str, Any]) -> Counter[tuple[str, str]]:
    for addr in org.get("address") or []:
        if addr.get("state"):
            return Counter({(addr.get("city") or "", addr["state"]): 1})
    return Counter()


def resources(bundle: dict[str, Any], rtype: str) -> list[dict[str, Any]]:
    return [e["resource"] for e in bundle.get("entry", []) if e.get("resource", {}).get("resourceType") == rtype]


def ref_id(ref: str) -> str:
    return re.split(r"[:/]", ref)[-1]


# ---------------------------------------------------------------------------
# Vendors: each returns entries without ids
# ---------------------------------------------------------------------------

def epic_entries(sources: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = sources["brands"]
    endpoints = {r["id"]: r for r in resources(bundle, "Endpoint") if r.get("status", "active") == "active"}
    orgs = [r for r in resources(bundle, "Organization") if r.get("active", True)]
    places: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for org in orgs:
        if org.get("partOf"):
            places[ref_id(org["partOf"]["reference"])].update(first_place(org))
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
        })

    # Legacy endpoints only where no brand already covers the address or the name.
    brand_addrs = {address_key(b["fhir_base_url"]) for b in brands}
    brand_names = {name_key(b["name"]) for b in brands}
    legacy = [{"name": r["name"].strip(), "platform": "epic", "fhir_base_url": r["address"].strip()}
              for r in resources(sources["r4"], "Endpoint") if r.get("address") and r.get("status", "active") == "active"]
    return brands + [l for l in legacy
                     if address_key(l["fhir_base_url"]) not in brand_addrs and name_key(l["name"]) not in brand_names]


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


# name: (file, id prefix, sources {key: url}, builder)
VENDORS: dict[str, tuple[str, str, dict[str, str], Callable[[dict[str, Any]], list[dict[str, Any]]]]] = {
    "epic": ("epic.json", "epic-", {"brands": EPIC_BRANDS_URL, "r4": EPIC_R4_URL}, epic_entries),
    "oracle": ("oracle.json", "oracle-", {"endpoints": ORACLE_URL}, oracle_entries),
    "ecw": ("ecw.json", "ecw-", {"practices": ECW_URL}, ecw_entries),
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

    # Addresses of old entries may predate a host rewrite; compare paths too.
    def addr_matches(old: dict[str, Any]) -> list[dict[str, Any]]:
        key = address_key(old.get("fhir_base_url") or "")
        return by_addr.get(key, []) or by_addr.get(key.replace(ECW_PROVIDER_HOST, ECW_PATIENT_HOST), [])

    # Names first (a shared endpoint shouldn't hand one brand's id to another), then addresses.
    kept, pending = 0, list(existing)
    for lookup in (lambda o: by_name.get(name_key(o["name"]), []), addr_matches):
        rest = []
        for old in pending:
            match = max((c for c in lookup(old) if "id" not in c), key=lambda c: similarity(old["name"], c), default=None)
            if match:
                match["id"], match["featured"] = old["id"], bool(old.get("featured"))
                match["former_ids"] = list(old.get("former_ids") or [])
                kept += 1
            else:
                rest.append(old)
        pending = rest
    for old in pending:
        target = next(iter(by_name.get(name_key(old["name"]), []) + addr_matches(old)), None)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vendors", nargs="*", help=f"vendors to refresh: {', '.join(VENDORS)} (default: all)")
    parser.add_argument("--source", action="append", default=[], metavar="KEY=FILE",
                        help="use a saved copy of a source instead of downloading it (keys: brands, r4, endpoints, practices)")
    args = parser.parse_args()
    saved = dict(s.split("=", 1) for s in args.source)
    unknown = [v for v in args.vendors if v not in VENDORS]
    if unknown:
        parser.error(f"unknown vendor(s): {', '.join(unknown)}")

    for vendor in args.vendors or list(VENDORS):
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
        merged = merge(existing, build(sources), prefix)
        path.write_text(json.dumps(merged, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  wrote {len(merged)} institutions to {path.relative_to(REPO_ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
