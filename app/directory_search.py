"""
Finding a health system by whatever someone calls it.

People type the name on their bill or wristband, the portal they log in to, a town, or a misspelling of any of those. The
index holds one sorted vocabulary of folded words (see ``fold``), and for each kind of text the rows it appears in:

* name: the listing's name, generated acronyms and the hand-kept aliases;
* network: the legal name of who runs its sign-in, long and full of other organizations' words, so it counts for less;
* place: its location and every town it has a site in;
* facility: each of a large system's facilities, so "brigham and women" finds Mass General Brigham and can say why.

A portal or vendor name ("MyChart", "healow") stands for a whole platform, so it's looked up per platform and takes no room
per row. Every word typed has to match somewhere; where it matched decides the rank (``Index.search``). A search finding
nothing is corrected by Damerau-Levenshtein distance over the vocabulary, preferring the common spelling. A rare word that
is listed isn't corrected: its rows come first, and rows of a much commoner spelling come after them.

Postings are ``array("I")`` runs laid end to end in vocabulary order, so a prefix is one slice. About 40,000 listings and
93,000 facilities take about 25 MB here and answer in under 20 ms on the broadest query on a laptop.
"""

from __future__ import annotations

import heapq
import math
import re
import unicodedata
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

_INITIALS = re.compile(r"\b(?:[a-z]\.){2,}")
_WORD = re.compile(r"[a-z0-9]+")
_SYNONYMS = {"saint": "st", "sainte": "st", "ste": "st", "mount": "mt", "fort": "ft", "centre": "center", "ctr": "center", "cntr": "center",
             "peds": "pediatric", "hosp": "hospital", "univ": "university"}
_STOP = frozenset({"of", "the", "and", "at", "for", "a", "an"})
# Written as one word or two. A compound is indexed both ways; "my chart" is typed for what's only ever one word.
_COMPOUNDS = {"healthcare": ("health", "care"), "healthsystem": ("health", "system"), "obgyn": ("ob", "gyn")}
_JOINED = {("my", "chart"): "mychart", ("your", "care"): "yourcare", ("mycare", "corner"): "mycarecorner"}
# Said alongside a name, these narrow nothing: "mayo patient portal", "hospitals in dallas".
_NOISE = frozenset({"patient", "portal", "login", "log", "sign", "signin", "online"})
# What a big city goes by. "children's hospital la" could mean Louisiana too, so these sit beside the state.
_CITY_SHORT = {"la": ("los", "angeles"), "sf": ("san", "francisco"), "nyc": ("new", "york"), "philly": ("philadelphia",)}

STATE_NAMES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy", "puerto rico": "pr",
}
_STATE_WORDS = {tuple(w for w in name.split() if w not in _STOP): code for name, code in STATE_NAMES.items()}
_STATE_BY_CODE = {code: words for words, code in _STATE_WORDS.items()}
_STATE_CODES = frozenset(STATE_NAMES.values())
_LONGEST_STATE = max(len(k) for k in _STATE_WORDS)

_RANK_BITS = 22
_RANK_MASK = (1 << _RANK_BITS) - 1
_EXACT, _STARTS, _PHRASE, _FACILITY_NAME, _ALL_IN_NAME, _IN_NETWORK, _FACILITY, _WITH_PLACE, _IN_PLACE, _ELSEWHERE, _BANDS = range(11)
# Each of these puts a row behind every row without it, whatever its band, and the later ones count for more: a row spelled
# as typed whose state isn't known is still likelier than a similar spelling that's known to be there.
_STATE_UNKNOWN, _NOT_AS_SAID, _SIMILAR_SPELLING = _BANDS, 2 * _BANDS, 4 * _BANDS
_FACILITIES_READ = 1000  # more than this sharing the words, and a facility being called just that says little
_RARE = 3               # at most this many rows, and a far commoner spelling one edit away: a typo that happens to be listed
_RARE_WHEN_NOTHING_FOUND = 100
_MUCH_COMMONER = 10
_SIMILAR = 10           # alternatives kept: within this factor of the commonest at the same distance


def fold_text(text: str) -> str:
    if not text.isascii():
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.lower().replace("'", "").replace("\u2019", "")
    return _INITIALS.sub(lambda m: m.group(0).replace(".", ""), text)


def fold_word(word: str) -> str:
    word = _SYNONYMS.get(word, word)
    if len(word) >= 4 and word[-1] == "s" and word[-2] not in "sui":
        word = word[:-1]                                    # Luke's and Lukes and Luke; Children's and Childrens
    return word


def fold(text: str) -> list[str]:
    """The words of ``text`` as the index holds them. The same on both sides, so anything it does can't cause a miss."""
    return [fold_word(w) for w in _WORD.findall(fold_text(text)) if w not in _STOP]


_STATE_FOLDED = {tuple(fold_word(w) for w in words): code for words, code in _STATE_WORDS.items()}


def _index_words(text: str) -> list[str]:
    words = fold(text)
    for w in tuple(words):
        words.extend(_COMPOUNDS.get(w, ()))
    return words


def _acronym(words: list[str]) -> Optional[str]:
    """OHSU for Oregon Health & Science University. Only from three words up: two make too many accidents."""
    letters = "".join(w[0] for w in words if not w.isdigit())
    return letters if 3 <= len(letters) <= 6 and len(words) >= 3 else None


def _states_of(location: Optional[str], places: Iterable[str]) -> tuple[str, ...]:
    found = dict.fromkeys(p.removesuffix(" and more").strip() for p in (location or "").split(","))
    found.update(dict.fromkeys(p.rsplit(" ", 1)[-1] for p in places))
    return tuple(sorted(s for s in found if s.isupper() and s.lower() in _STATE_CODES))


def _distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein (optimal string alignment), giving up past ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    before2: list[int] = []
    before = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = min(before[j] + 1, row[j - 1] + 1, before[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, before2[j - 2] + 1)
            row.append(cost)
            best = min(best, cost)
        if best > limit:
            return limit + 1
        before2, before = before, row
    return before[-1]


def _is_state(word: str) -> bool:
    return word in _STATE_CODES or (word,) in _STATE_WORDS


def _terms_for(word: str) -> list[tuple[str, ...]]:
    """What a typed word can mean: itself in folded form, and for a state its code or its name."""
    folded = fold_word(word)
    terms: list[tuple[str, ...]] = [_COMPOUNDS.get(folded, (folded,))]
    if word in _STATE_CODES:
        terms.append(tuple(fold_word(w) for w in _STATE_BY_CODE[word]))
    elif (folded,) in _STATE_FOLDED:
        terms.append((_STATE_FOLDED[folded,],))
    if word in _CITY_SHORT:
        terms.append(tuple(fold_word(w) for w in _CITY_SHORT[word]))
    return terms


def _name_band(name: str, phrase: str) -> int:
    """How well a folded name matches what was typed, all of whose words it has. Without spaces too: OhioHealth is "ohio health"."""
    bare, solid = name.replace(" ", ""), phrase.replace(" ", "")
    if bare == solid:
        return _EXACT
    if bare.startswith(solid):
        return _STARTS
    return _PHRASE if f" {phrase}" in name else _ALL_IN_NAME


def _as_said(q: str) -> str:
    """The folded phrase with nothing left out: searched as "first", "patient first" is still someone's name."""
    return " ".join(t for w in fold(q) for t in _COMPOUNDS.get(w, (w,)))


def _said_with(said: str, words: list[_Word], fixed: list[_Word]) -> str:
    if len(words) != len(fixed):
        return said
    said = f" {said} "
    for word, fix in zip(words, fixed):
        said = said.replace(f" {' '.join(word.terms[0])} ", f" {' '.join(fix.terms[0])} ")
    return said.strip()


def _letters(word: str) -> int:
    mask = 0
    for c in word:
        mask |= 1 << (ord(c) % 31)
    return mask


class _Pairs:
    """(word, row) pairs as they're found, in two flat arrays, then counted out into ``_Field`` order. A list per word
    would take several times the memory while building, and Python keeps what it has grown to."""
    __slots__ = ("words", "rows")

    def __init__(self) -> None:
        self.words = array("I")
        self.rows = array("I")

    def add(self, word: int, row: int) -> None:
        self.words.append(word)
        self.rows.append(row)


class _Field:
    """One kind of text: for each word of the vocabulary, the rows it's in."""
    __slots__ = ("starts", "rows")

    def __init__(self, pairs: _Pairs, position: array):
        counts = array("I", bytes(4 * (len(position) + 1)))
        for word in pairs.words:
            counts[position[word] + 1] += 1
        total = 0
        for i, n in enumerate(counts):
            total += n
            counts[i] = total
        self.starts = counts
        self.rows = array("I", bytes(4 * len(pairs.rows)))
        next_free = array("I", counts[:-1])
        for word, row in zip(pairs.words, pairs.rows):
            at = position[word]
            self.rows[next_free[at]] = row
            next_free[at] += 1

    def rows_between(self, lo: int, hi: int) -> set[int]:
        return set(self.rows[self.starts[lo]:self.starts[hi]])

    def count_at(self, i: int) -> int:
        return self.starts[i + 1] - self.starts[i]


@dataclass
class _Word:
    """One word typed, and where it was found. ``text`` is what a correction would replace."""
    text: str
    terms: list[tuple[str, ...]]        # any of these, each all of its words
    prefix: bool
    state: bool = False
    name: set[int] = field(default_factory=set)
    network: set[int] = field(default_factory=set)
    place: set[int] = field(default_factory=set)
    platform: set[int] = field(default_factory=set)
    facilities: set[int] = field(default_factory=set)
    rows: set[int] = field(default_factory=set)


class Index:
    def __init__(self, rows: list[dict[str, Any]], facilities: Iterable[tuple[str, str, str, str]], aliases: dict[str, list[str]],
                 platform_words: dict[str, Iterable[str]]):
        """``facilities`` yields (listing id, facility or "", city, state), one listing's together."""
        self.rows = rows
        names, networks, places, sites = _Pairs(), _Pairs(), _Pairs(), _Pairs()
        numbers: dict[str, int] = {}

        def number_of(word: str) -> int:
            return numbers.setdefault(word, len(numbers))

        self.folded_names: list[str] = []
        self.platform_of: list[int] = []
        self.platform_names = sorted({r["platform"] for r in rows})
        platform_number = {p: i for i, p in enumerate(self.platform_names)}
        self.platform_rows = [array("I") for _ in self.platform_names]
        self.exact_alias: dict[str, array] = defaultdict(lambda: array("I"))
        row_of = {}
        for row, inst in enumerate(rows):
            row_of[inst["id"]] = row
            words = fold(inst["name"])
            self.folded_names.append(" ".join(t for w in words for t in _COMPOUNDS.get(w, (w,))))
            seen = set(_index_words(inst["name"]))
            for word in set(_index_words(inst.get("network") or "")):
                networks.add(number_of(word), row)
            acronym = _acronym(words)
            if acronym:
                seen.add(acronym)
            for alias in aliases.get(inst["id"], ()):
                seen.update(_index_words(alias))
                self.exact_alias[" ".join(fold(alias))].append(row)
            for word in seen:
                names.add(number_of(word), row)
            number = platform_number[inst["platform"]]
            self.platform_of.append(number)
            self.platform_rows[number].append(row)

        blob = bytearray()
        self.site_row = array("I")
        self.site_start = array("I", [0])
        self.site_range: dict[int, tuple[int, int]] = {}
        sizes: dict[int, int] = {}
        states_of: dict[int, set[str]] = {}
        towns_of: dict[int, set[str]] = {}
        for inst_id, site, city, state in facilities:
            row = row_of.get(inst_id)
            if row is None:
                continue
            sizes[row] = sizes.get(row, 0) + 1
            if city or state:
                towns_of.setdefault(row, set()).update(fold(f"{city} {state}"))
                states_of.setdefault(row, set()).add(state)
            if site:
                number = len(self.site_row)
                self.site_row.append(row)
                blob += site.encode()
                blob += b"\n"
                self.site_start.append(len(blob))
                first = self.site_range.get(row, (number,))[0]
                self.site_range[row] = (first, number + 1)
                for word in set(_index_words(site)):
                    sites.add(number_of(word), number)
        self.site_names = bytes(blob)
        del blob, row_of

        self.states: list[tuple[str, ...]] = []
        shared_states: dict[tuple[str, ...], tuple[str, ...]] = {}
        for row, inst in enumerate(rows):
            place_words = towns_of.pop(row, set())
            place_words.update(fold(inst.get("location") or ""))
            for place in inst.get("places", ()):
                place_words.update(fold(place))
            for word in place_words:
                places.add(number_of(word), row)
            states = _states_of(inst.get("location"), [*inst.get("places", ()), *states_of.pop(row, ())])
            self.states.append(shared_states.setdefault(states, states))

        self.vocabulary = sorted(numbers)
        position = array("I", bytes(4 * len(numbers)))
        for i, word in enumerate(self.vocabulary):
            position[numbers[word]] = i
        numbers.clear()
        self.name_field = _Field(names, position)
        self.network_field = _Field(networks, position)
        self.place_field = _Field(places, position)
        self.site_field = _Field(sites, position)
        self.platform_terms = sorted((fold_word(word), platform_number[p]) for p, words in platform_words.items()
                                     if p in platform_number for word in words)
        self.portal_words = frozenset(term for term, _ in self.platform_terms)

        # Featured first, then larger systems (by powers of two: a tie-breaker, not a contest), then by name.
        order = sorted(range(len(rows)), key=lambda r: (not rows[r].get("featured"), -int(math.log2(sizes.get(r, 0) + 1)), rows[r]["name"].lower()))
        self.rank_of = array("I", [0]) * len(rows)
        for rank, row in enumerate(order):
            self.rank_of[row] = rank
        self.row_at = array("I", order)

        self.by_length: dict[int, array] = defaultdict(lambda: array("I"))
        self.letters = array("I")
        for i, word in enumerate(self.vocabulary):
            self.letters.append(_letters(word))
            if not word.isdigit():
                self.by_length[len(word)].append(i)

    # ------------------------------------------------------------------ words typed
    def _range(self, word: str, prefix: bool) -> tuple[int, int]:
        lo = bisect_left(self.vocabulary, word)
        if prefix:
            return lo, bisect_left(self.vocabulary, word + "\uffff", lo)
        return (lo, lo + 1) if lo < len(self.vocabulary) and self.vocabulary[lo] == word else (lo, lo)

    def _find_term(self, word: str, prefix: bool) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
        lo, hi = self._range(word, prefix)
        name = self.name_field.rows_between(lo, hi)
        short = len(word) < 2           # on its way to being a word, so names alone: it would otherwise be half the index
        network = self.network_field.rows_between(lo, hi) if not short else set()
        place = self.place_field.rows_between(lo, hi) if not short else set()
        sites = self.site_field.rows_between(lo, hi) if not short else set()
        platform: set[int] = set()
        for term, number in self.platform_terms:
            if term == word or (prefix and term.startswith(word)):
                platform.update(self.platform_rows[number])
        return name, network, place, platform, sites

    def _look_up(self, word: _Word) -> _Word:
        for term in word.terms:
            found = [self._find_term(w, len(w) >= 3 or (word.prefix and w not in _STATE_CODES)) for w in term]
            if len(found) == 1:
                name, network, place, platform, sites = found[0]
            else:
                # All the words of "new york", wherever each one is; it counts as a name or a place only when together in one.
                word.rows |= set.intersection(*(n | m | p | q | {self.site_row[s] for s in f} for n, m, p, q, f in found))
                name, network, place, platform, sites = (set.intersection(*part) for part in zip(*found))
            word.name |= name
            word.network |= network
            word.place |= place
            word.platform |= platform
            word.facilities |= sites
        word.rows |= word.name | word.network | word.place | word.platform | {self.site_row[s] for s in word.facilities}
        return word

    def _join_neighbors(self, words: list[_Word]) -> list[_Word]:
        """A name written as one word answers to its parts: "ohio health" is OhioHealth, "mercy one" MercyOne."""
        for a, b in zip(words, words[1:]):
            lo, hi = self._range(fold_word(a.text + b.text), False)
            if lo < hi and (rows := self.name_field.rows_between(lo, hi)):
                for word in (a, b):
                    word.name = word.name | rows        # a new set: a respelled search shares these with the one as typed
                    word.rows = word.rows | rows
        return words

    def _parse(self, q: str) -> list[_Word]:
        typed: list[str] = []
        for w in _WORD.findall(fold_text(q)):
            if typed and (typed[-1], w) in _JOINED:
                typed[-1] = _JOINED[typed[-1], w]
            else:
                typed.append(w)
        # "hospitals in dallas", but "evansville in"; and on its own, "the" may be the start of ThedaCare.
        kept = [w for i, w in enumerate(typed) if not (w in _STOP or w in _NOISE or (w == "in" and i < len(typed) - 1))]
        typed = kept or typed
        words: list[_Word] = []
        i = 0
        while i < len(typed):
            for size in range(min(_LONGEST_STATE, len(typed) - i), 1, -1):
                key = tuple(typed[i:i + size])
                if key in _STATE_WORDS:
                    words.append(_Word(" ".join(key), [tuple(fold_word(w) for w in key), (_STATE_WORDS[key],)], prefix=False, state=True))
                    i += size
                    break
            else:
                w = typed[i]
                words.append(_Word(w, _terms_for(w), prefix=i == len(typed) - 1 or len(w) >= 3, state=_is_state(w)))
                i += 1
        return words

    # ------------------------------------------------------------------ spelling
    def _count(self, word: str) -> int:
        lo, hi = self._range(word, False)
        if lo == hi:
            return 0
        return (10 * (self.name_field.count_at(lo) + self.network_field.count_at(lo)) + 2 * self.place_field.count_at(lo)
                + self.site_field.count_at(lo))

    def _spellings(self, word: str) -> list[str]:
        """Words one or two edits from ``word``: the closest, and of those the commonest."""
        limit = 0 if len(word) < 4 or word.isdigit() else 1 if len(word) < 8 else 2
        if not limit:
            return []
        mask = _letters(word)
        close: dict[int, list[str]] = defaultdict(list)
        for length in range(len(word) - limit, len(word) + limit + 1):
            for i in self.by_length.get(length, ()):
                # An edit changes at most two of the letters present; most of the vocabulary stops here.
                if (self.letters[i] ^ mask).bit_count() <= 2 * limit:
                    other = self.vocabulary[i]
                    if other != word and (d := _distance(word, other, limit)) <= limit:
                        close[d].append(other)
        if not close:
            return []
        words = close[min(close)]
        counts = {w: self._count(w) for w in words}
        most = max(counts.values())
        return sorted((w for w in words if counts[w] * _SIMILAR >= most), key=lambda w: -counts[w])[:4]

    def _respell(self, words: list[_Word], *, nothing_found: bool) -> Optional[list[_Word]]:
        """The same words with what may be typos fixed, or None when no word looks like one. A listed word also keeps its own rows."""
        changed = False
        out = []
        for word in words:
            term = word.terms[0]
            # At four letters and under, a rare word is more likely an acronym (OHSU) than a typo of a common one (OHS).
            rare = (not word.rows or len(term[0]) >= 5) and len(word.rows) <= (_RARE_WHEN_NOTHING_FOUND if nothing_found else _RARE)
            if len(term) == 1 and not word.state and rare:
                own = self._count(term[0])
                # A word that's listed but rare stays unless the fix is far commoner. "mays" isn't a typo for "mayo".
                # The vendors' lists have typos of their own: "clinc" is in a few names.
                fixes = [f for f in self._spellings(term[0]) if not word.rows or self._count(f) >= _MUCH_COMMONER * max(own, 1)]
                if fixes:
                    terms = [t for f in fixes for t in _terms_for(f)]
                    out.append(self._look_up(_Word(fixes[0], terms + ([term] if word.rows else []), prefix=False)))
                    changed = True
                    continue
            out.append(replace(word))
        if changed:
            return out
        return self._rejoin(words) if nothing_found else None

    def _rejoin(self, words: list[_Word]) -> Optional[list[_Word]]:
        """'north shore' for NorthShore, 'mountsinai' for Mount Sinai."""
        for i in range(len(words) - 1):
            together = fold_word(words[i].text + words[i + 1].text)
            if self._count(together):
                fixed = self._look_up(_Word(together, [(together,)], prefix=False))
                return [*words[:i], fixed, *words[i + 2:]]
        for i, word in enumerate(words):
            for cut in range(2, len(word.text) - 1):
                a, b = fold_word(word.text[:cut]), fold_word(word.text[cut:])
                if self._count(a) and self._count(b):
                    parts = [self._look_up(_Word(p, [(p,)], prefix=False)) for p in (a, b)]
                    return [*words[:i], *parts, *words[i + 1:]]
        return None

    # ------------------------------------------------------------------ results
    def _matches(self, words: list[_Word]) -> set[int]:
        return set.intersection(*sorted((w.rows for w in words), key=len))

    def _is_portal(self, word: _Word) -> bool:
        return len(word.terms[0]) == 1 and word.terms[0][0] in self.portal_words

    def _site_name(self, site: int) -> str:
        return self.site_names[self.site_start[site]:self.site_start[site + 1] - 1].decode()

    def _keys(self, matched: set[int], words: list[_Word], later: Optional[set[str]], troubled: set[int], *,
              said: Optional[str] = None, behind: int = 0, in_state: bool = False) -> list[int]:
        """A number per row that sorts it: how well its text matched, then whether it can be connected and works, then rank.
        ``said`` is the whole phrase when the search left some of it out; ``in_state``, that a state was chosen."""
        # "mayo clinic mychart" asks for Mayo Clinic and says how they log in: a portal word narrows, and isn't part of the name.
        portals = [w for w in words if self._is_portal(w)]
        named = [w for w in words if not self._is_portal(w)] or words
        all_in_name = set.intersection(matched, *(w.name for w in words))
        name_and_portal = set.intersection(matched, *(w.name for w in named), *(w.name | w.platform for w in portals))
        in_network = set.intersection(matched, *(w.network for w in named))
        any_in_name = matched & set.union(*(w.name for w in words))
        in_name_or_place = set.intersection(matched, *(w.name | w.place | w.platform for w in words))
        in_place = set.intersection(matched, *(w.place | w.platform for w in words))
        same_site = set.intersection(*sorted((w.facilities for w in named), key=len))
        site_rows = {self.site_row[s] for s in same_site}
        phrase = " ".join(t for w in words for t in w.terms[0])
        short_phrase = " ".join(t for w in named for t in w.terms[0])
        called_that: set[int] = set()
        if len(same_site) <= _FACILITIES_READ:
            for site in same_site:
                folded_site = " ".join(t for w in fold(self._site_name(site)) for t in _COMPOUNDS.get(w, (w,)))
                if folded_site == short_phrase or folded_site.startswith(short_phrase + " "):
                    called_that.add(self.site_row[site])
        aliased = set(self.exact_alias.get(phrase, ())) | set(self.exact_alias.get(short_phrase, ()))
        if said == phrase:
            said = None
        said_alias = set(self.exact_alias.get(said, ())) if said else set()
        later_platform = [later is not None and p in later for p in self.platform_names]
        folded = self.folded_names
        keys = []
        for row in matched:
            if row in aliased:
                band = _EXACT
            elif row in all_in_name:
                band = _name_band(folded[row], phrase)
            elif row in name_and_portal:
                band = _name_band(folded[row], short_phrase)
            elif row in in_network:
                band = _IN_NETWORK
            elif row in site_rows:
                band = _FACILITY
            elif row in any_in_name and row in in_name_or_place:
                band = _WITH_PLACE
            elif row in in_place:
                band = _IN_PLACE
            else:
                band = _ELSEWHERE
            if row in called_that:
                band = min(band, _FACILITY_NAME)
            if said:
                whole = _EXACT if row in said_alias else _name_band(folded[row], said)
                band = whole if whole <= _PHRASE else band + _NOT_AS_SAID
            if in_state and not self.states[row]:
                band += _STATE_UNKNOWN
            keys.append((band + behind) << (_RANK_BITS + 2) | later_platform[self.platform_of[row]] << (_RANK_BITS + 1)
                        | (row in troubled) << _RANK_BITS | self.rank_of[row])
        return keys

    def includes(self, row: int, words: list[_Word]) -> Optional[str]:
        """The facility that explains why ``row`` matched, when its own name doesn't."""
        span = self.site_range.get(row)
        if not span or all(row in w.name or (row in w.platform and self._is_portal(w)) for w in words):
            return None
        best, best_key = None, (0, 0)
        for site in range(*span):
            hits = sum(site in w.facilities for w in words)
            # The shortest of those matching most: Brigham and Women's Hospital before its Faulkner campus.
            key = (hits, self.site_start[site] - self.site_start[site + 1])
            if hits and (best is None or key > best_key):
                best, best_key = site, key
        if best is None:
            return None
        return self._site_name(best)

    def search(self, q: str, *, keep: Optional[set[int]], drop: set[int], later: Optional[set[str]], troubled: set[int],
               limit: int, offset: int, state: Optional[str]) -> dict[str, Any]:
        words = self._join_neighbors([self._look_up(w) for w in self._parse(q)])
        if not words:
            return {"total": 0, "results": [], "states": {}}
        def findable(rows: set[int]) -> set[int]:
            return (rows if keep is None else rows & keep) - drop

        found = self._matches(words)
        as_typed = findable(found)
        similar: set[int] = set()
        said = _as_said(q)
        similar_words, similar_said = words, said
        corrected = None
        fixed = self._respell(words, nothing_found=not found)
        if fixed is not None and (again := findable(self._matches(self._join_neighbors(fixed))) - as_typed):
            if as_typed:
                # Listed as typed, so it isn't called a typo: those rows lead and the commoner spelling follows.
                similar, similar_words, similar_said = again, fixed, _said_with(said, words, fixed)
            else:
                corrected = " ".join(t for w in fixed for t in w.terms[0])
                as_typed, words, said = again, fixed, _said_with(said, words, fixed)
        everywhere = as_typed | similar
        matched = {r for r in everywhere if state in self.states[r] or not self.states[r]} if state else everywhere
        keys = self._keys(matched - similar, words, later, troubled, said=said, in_state=bool(state))
        if similar:
            keys += self._keys(matched & similar, similar_words, later, troubled, said=similar_said, behind=_SIMILAR_SPELLING, in_state=bool(state))
        results = []
        for key in heapq.nsmallest(limit + offset, keys)[offset:]:
            row = self.row_at[key & _RANK_MASK]
            site = self.includes(row, similar_words if row in similar else words)
            results.append({**self.rows[row], "includes": site} if site else self.rows[row])
        out = {"total": len(matched), "results": results, "states": self._states_among(everywhere) if state or len(matched) > limit else {}}
        if corrected:
            out["corrected"] = corrected
        if similar:
            out["as_typed"] = len(matched - similar)
        if state and (unplaced := sum(not self.states[r] for r in matched)):
            out["no_state"] = unplaced
        return out

    def _states_among(self, rows: Iterable[int]) -> dict[str, int]:
        found: Counter[str] = Counter()
        for row in rows:
            found.update(self.states[row])
        return dict(sorted(found.items()))

    def listing(self, *, keep: Optional[set[int]], drop: set[int], later: Optional[set[str]], limit: int, offset: int,
                state: Optional[str]) -> dict[str, Any]:
        """Everything (or one platform's or state's), best known first. Choosing a state keeps listings whose place isn't known, last."""
        everywhere = [r for r in (range(len(self.rows)) if keep is None else keep) if r not in drop]
        matched = [r for r in everywhere if state in self.states[r] or not self.states[r]] if state else everywhere
        later_platform = [later is not None and p in later for p in self.platform_names]
        order = heapq.nsmallest(limit + offset, matched, key=lambda r: (not self.states[r], later_platform[self.platform_of[r]], self.rank_of[r])
                                if state else (later_platform[self.platform_of[r]], self.rank_of[r]))
        out = {"total": len(matched), "results": [self.rows[r] for r in order[offset:]],
               "states": self._states_among(everywhere) if state or len(matched) > limit else {}}
        if state and (unplaced := sum(not self.states[r] for r in matched)):
            out["no_state"] = unplaced
        return out

    def rows_on(self, platform: str) -> set[int]:
        try:
            return set(self.platform_rows[self.platform_names.index(platform)])
        except ValueError:
            return set()

    def rows_on_any(self, platforms: Iterable[str]) -> set[int]:
        out: set[int] = set()
        for p in platforms:
            out |= self.rows_on(p)
        return out
