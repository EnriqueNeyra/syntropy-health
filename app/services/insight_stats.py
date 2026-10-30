"""
The statistics behind insights, kept apart so they can be tested on their own. Standard library only.

Daily health numbers are correlated: a poor week of sleep is seven related days, not seven separate pieces of evidence.
Everything here allows for that, because treating them as independent is what makes coincidences look like findings.
"""

from __future__ import annotations

import math
import random
import statistics
import zlib
from typing import Any, Optional, Sequence

MAD_TO_SD = 1.4826      # scales the median absolute deviation to a standard deviation on normal data
MEDIAN_SE = 1.2533      # the standard error of a median, as a multiple of sd / sqrt(n)


def robust_center(values: Sequence[float]) -> tuple[float, float]:
    """The median and a spread in standard-deviation units that one bad reading can't move. Falls back to the plain
    standard deviation when most values are identical (the median deviation is then zero)."""
    mid = statistics.median(values)
    spread = MAD_TO_SD * statistics.median(abs(v - mid) for v in values)
    return mid, spread or statistics.pstdev(values)


def lag1(values: Sequence[float]) -> float:
    """How much each value resembles the one before, from 0 up to 0.95. The plain estimate runs low on short series,
    so it's corrected (Marriott & Pope, 1954), and physiological series are assumed to carry at least a little."""
    n = len(values)
    if n < 4:
        return 0.3
    mean = sum(values) / n
    denom = sum((v - mean) ** 2 for v in values)
    if not denom:
        return 0.1
    rho = sum((a - mean) * (b - mean) for a, b in zip(values, values[1:])) / denom
    return max(0.1, min(0.95, rho + (1 + 3 * rho) / n))


def effective_n(n: int, rho: float) -> float:
    """How many independent values ``n`` correlated ones are worth."""
    return max(1.0, n * (1 - rho) / (1 + rho))


def normal_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def t_two_sided(t: float, df: float) -> float:
    """A two-sided p-value for Student's t, by Wallace's (1959) normal approximation."""
    df = max(df, 1.0)
    return normal_two_sided((8 * df + 1) / (8 * df + 3) * math.sqrt(df * math.log1p(t * t / df)))


def shift_p(recent: Sequence[float], base: Sequence[float]) -> float:
    """Whether the middle of ``recent`` sits away from the middle of ``base`` by more than their day-to-day swing and
    correlation explain. Both are in date order."""
    mid_r, _ = robust_center(recent)
    mid_b, spread = robust_center(base)
    delta = mid_r - mid_b
    rho = lag1(base)
    n_r, n_b = effective_n(len(recent), rho), effective_n(len(base), rho)
    # Discrete readings can have no spread at all: only the caller's smallest meaningful change then decides.
    se = MEDIAN_SE * spread * math.sqrt(1 / n_r + 1 / n_b)
    if not se:
        return 0.0 if delta else 1.0
    return t_two_sided(delta / se, n_b - 1)


def false_discoveries_removed(p_values: Sequence[float], q: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg: which of many tests still count once it's allowed for that some will look striking by
    chance. Pass every test that was looked at, or the correction is too small."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    cut = -1
    for rank, i in enumerate(order, 1):
        if p_values[i] <= q * rank / m:
            cut = rank
    keep = [False] * m
    for rank, i in enumerate(order, 1):
        keep[i] = rank <= cut
    return keep


def _block_order(n: int, size: int, rng: random.Random) -> list[int]:
    """The positions 0..n-1 cut at a random point into runs of ``size`` and shuffled, so neighbours stay neighbours."""
    start = rng.randrange(size)
    blocks = [list(range(0, start))] if start else []
    blocks += [list(range(i, min(i + size, n))) for i in range(start, n, size)]
    rng.shuffle(blocks)
    return [i for b in blocks for i in b]


def group_gap_p(values: Sequence[float], in_group: Sequence[bool], key: str, permutations: int = 499) -> float:
    """How often shuffling the days would give a gap between the group's mean and the others' at least as big as the
    real one. ``values`` are in date order. Runs of days are shuffled together, so that correlated days aren't
    counted as independent. Seeded from ``key``: the same data always gets the same answer, so an insight doesn't
    come and go between one page load and the next."""
    n = len(values)
    k = sum(in_group)
    if not k or k == n:
        return 1.0
    total = sum(values)

    def gap(picks: Sequence[int]) -> float:
        inside = sum(values[i] for i in picks)
        return abs(inside / k - (total - inside) / (n - k))

    members = [i for i, flag in enumerate(in_group) if flag]
    real = gap(members)
    rho = max(lag1(values), lag1([1.0 if f else 0.0 for f in in_group]))
    size = max(1, min(n // 5, round((1 + rho) / (1 - rho))))
    rng = random.Random(zlib.crc32(f"{key}|{n}|{k}".encode()))
    hits = 0
    for _ in range(permutations):
        order = _block_order(n, size, rng)
        if gap([order[i] for i in members]) >= real - 1e-12:
            hits += 1
    return (hits + 1) / (permutations + 1)


def gap_margin(on: Sequence[float], off: Sequence[float]) -> float:
    """About how far a gap between two group means could be off (a 95% margin), allowing for correlated days."""
    def part(xs: Sequence[float]) -> float:
        return statistics.pvariance(xs) / effective_n(len(xs), lag1(xs))
    return 1.96 * math.sqrt(part(on) + part(off))


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _detrended(ts: Sequence[float], values: Sequence[float]) -> list[float]:
    n = len(values)
    mt, mv = sum(ts) / n, sum(values) / n
    denom = sum((t - mt) ** 2 for t in ts)
    slope = sum((t - mt) * (v - mv) for t, v in zip(ts, values)) / denom if denom else 0.0
    return [v - mv - slope * (t - mt) for t, v in zip(ts, values)]


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _acf(xs: Sequence[float], k: int) -> float:
    return _pearson(xs[:-k], xs[k:]) or 0.0 if len(xs) > k + 2 else 0.0


def _rank_interval(a: Sequence[float], b: Sequence[float], z_crit: float) -> Optional[dict[str, float]]:
    r = _pearson(a, b)
    if r is None:
        return None
    n = len(a)
    horizon = min(n // 5, 14)
    weight = sum((1 - k / n) * _acf(a, k) * _acf(b, k) for k in range(1, horizon + 1))
    n_eff = max(4.0, min(float(n), n / (1 + 2 * max(weight, 0.0))))
    z = math.atanh(max(-0.999999, min(0.999999, r)))
    margin = z_crit / math.sqrt(max(n_eff - 3, 1.0))
    return {"r": r, "low": math.tanh(z - margin), "high": math.tanh(z + margin), "n_eff": n_eff}


def association(ts: Sequence[float], xs: Sequence[float], ys: Sequence[float], z_crit: float = 1.96) -> Optional[dict[str, Any]]:
    """A rank correlation with an interval, or None when either series doesn't vary. Each series' overall trend comes
    out first, since two things that both drift upward aren't linked, and the interval is sized by the effective number
    of days (Pyper & Peterman, 1998). Two wandering series still agree over long stretches by chance, so ``clear`` also
    asks that they move together from one day to the next."""
    a, b = _ranks(_detrended(ts, xs)), _ranks(_detrended(ts, ys))
    found = _rank_interval(a, b, z_crit)
    if found is None:
        return None
    clear = not found["low"] <= 0 <= found["high"]
    if clear and len(xs) >= 10:
        dx = [q - p for p, q in zip(xs, xs[1:])]
        dy = [q - p for p, q in zip(ys, ys[1:])]
        moves = _rank_interval(_ranks(dx), _ranks(dy), z_crit)
        clear = bool(moves) and moves["r"] * found["r"] > 0 and not moves["low"] <= 0 <= moves["high"]
    return {**found, "clear": clear}


def theil_sen(ts: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    """A straight-line slope that one odd result can't swing (the median of all pairwise slopes), and how consistent
    the direction is: Kendall's tau, from -1 (always falling) to 1 (always rising)."""
    slopes, concordant, pairs = [], 0, 0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if ts[j] == ts[i]:
                continue
            slope = (values[j] - values[i]) / (ts[j] - ts[i])
            slopes.append(slope)
            concordant += (slope > 0) - (slope < 0)
            pairs += 1
    return (statistics.median(slopes), concordant / pairs) if slopes else (0.0, 0.0)
