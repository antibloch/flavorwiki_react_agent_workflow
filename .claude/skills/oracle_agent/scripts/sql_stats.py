#!/usr/bin/env python3
"""Bounded statistical analysis over a SQL result set from gpi-db.

Give it one SQL query; it pulls the rows out of the docker Postgres and runs
the appropriate tests with real p-values, confidence intervals and effect
sizes - descriptive stats, ANOVA + Tukey HSD, Welch t-tests, Kruskal-Wallis /
Mann-Whitney, Pearson + Spearman, chi-square, and proportion tests.

This is the local counterpart to scripts/stats_api.py. Use the Charts API when
the question exists there (it is the same engine the product uses); use this
when the API has no data for a locally-dumped question, when the analysis is
not one of the API's fixed test types, or to independently verify an API
number.

    # per-product rating comparison: ANOVA + Tukey + pairwise Welch
    sql_stats.py groups --sql "SELECT p.name, (aqo.\"answerData\"->>'optionAnswer')::numeric
                               FROM ... WHERE ..."

    # one numeric column: N, mean, SD, SE, median, quartiles, 95% CI
    sql_stats.py describe --sql "SELECT (a.value)::numeric FROM ..."

    # two numeric columns, one row per respondent: Pearson + Spearman + regression
    sql_stats.py paired --sql "SELECT liking, purchase_intent FROM ..."

    # two label columns (optional third = count): chi-square + Cramer's V
    sql_stats.py crosstab --sql "SELECT gender, choice, count(*) FROM ... GROUP BY 1,2"

    # one label column + one 0/1|bool column: proportions + Wilson CIs + z-test
    sql_stats.py proportion --sql "SELECT p.name, (score >= 4) FROM ..."

Add --json for machine-readable output, --alpha to change the 0.05 default.
Columns default to positional order; override with --group-col/--value-col/etc.
Stdlib only: the distributions (t, F, chi-square, studentized range) are
implemented here, so no numpy/scipy install is needed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys

# ---------------------------------------------------------------- data access

CONTAINER = os.getenv("GPI_DB_CONTAINER", "gpi-db")
DB_USER = os.getenv("GPI_DB_USER", "gpi")
DB_NAME = os.getenv("GPI_DB_NAME", "gpi_sample_db")


def fetch_rows(sql: str) -> tuple[list[str], list[list[str]]]:
    proc = subprocess.run(
        [
            "docker", "exec",
            # Read-only via PGOPTIONS, not a leading `SET`: a SET command tag
            # would be printed ahead of the result and parsed as the CSV header.
            "-e", "PGOPTIONS=-c default_transaction_read_only=on",
            "-i", CONTAINER,
            "psql", "-U", DB_USER, "-d", DB_NAME,
            "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "--csv",
            "-c", sql,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit("SQL failed:\n" + (proc.stderr or proc.stdout).strip())
    reader = csv.reader(io.StringIO(proc.stdout))
    rows = [r for r in reader if r]
    if not rows:
        sys.exit("Query returned no rows. Inspect the underlying data before treating "
                 "this as an answer - a zero from loose text matching is a diagnostic, "
                 "not a result.")
    return rows[0], rows[1:]


def pick(header: list[str], name: str | None, default_index: int) -> int:
    if name is None:
        if default_index >= len(header):
            sys.exit(f"Query returned {len(header)} column(s); need at least {default_index + 1}.")
        return default_index
    if name in header:
        return header.index(name)
    try:
        return int(name)
    except ValueError:
        sys.exit(f"Column '{name}' not in result columns {header}.")


def to_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def to_bool(raw: str) -> bool | None:
    v = (raw or "").strip().lower()
    if v in ("t", "true", "1", "yes", "y"):
        return True
    if v in ("f", "false", "0", "no", "n"):
        return False
    return None


# ------------------------------------------------------------- distributions

def _log_gamma(x: float) -> float:
    return math.lgamma(x)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny, eps, max_iter = 1e-30, 3e-16, 400
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        _log_gamma(a + b) - _log_gamma(a) - _log_gamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        _log_gamma(a + b) - _log_gamma(a) - _log_gamma(b)
        + b * math.log(1.0 - x) + a * math.log(x)
    ) * _betacf(b, a, 1.0 - x) / b


def gammainc_lower(s: float, x: float) -> float:
    """Regularized lower incomplete gamma P(s, x)."""
    if x <= 0:
        return 0.0
    if x < s + 1.0:                      # series expansion
        term = 1.0 / s
        total = term
        for n in range(1, 500):
            term *= x / (s + n)
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return total * math.exp(-x + s * math.log(x) - _log_gamma(s))
    # continued fraction for Q(s, x), then complement
    tiny = 1e-300
    b = x + 1.0 - s
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - s)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    q = math.exp(-x + s * math.log(x) - _log_gamma(s)) * h
    return 1.0 - q


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def t_sf_two_sided(t: float, df: float) -> float:
    """Two-sided p-value for Student's t."""
    if df <= 0:
        return float("nan")
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_ppf(p: float, df: float) -> float:
    """Inverse t CDF by bisection on the two-sided tail (p is one-sided lower)."""
    if df <= 0:
        return float("nan")
    if df > 1e6:
        return norm_ppf(p)
    target = p
    lo, hi = -400.0, 400.0
    for _ in range(300):
        mid = (lo + hi) / 2.0
        cdf = 1.0 - 0.5 * t_sf_two_sided(mid, df) if mid > 0 else 0.5 * t_sf_two_sided(mid, df)
        if cdf < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def f_sf(f: float, df1: float, df2: float) -> float:
    """Upper-tail p-value for the F distribution."""
    if f <= 0 or df1 <= 0 or df2 <= 0:
        return float("nan")
    return betainc(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * f))


def f_ppf(p: float, df1: float, df2: float) -> float:
    lo, hi = 1e-8, 1e8
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        if 1.0 - f_sf(mid, df1, df2) < p:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def chi2_sf(x: float, df: float) -> float:
    if x <= 0 or df <= 0:
        return float("nan")
    return 1.0 - gammainc_lower(df / 2.0, x / 2.0)


# --- studentized range (Tukey), needed for honest p-adj values -------------

_GL_NODES, _GL_WEIGHTS = None, None


def _gauss_legendre(n: int = 60):
    """Nodes/weights on [-1, 1] via Newton iteration on Legendre polynomials."""
    global _GL_NODES, _GL_WEIGHTS
    if _GL_NODES is not None:
        return _GL_NODES, _GL_WEIGHTS
    nodes, weights = [], []
    for i in range(1, n + 1):
        x = math.cos(math.pi * (i - 0.25) / (n + 0.5))
        for _ in range(100):
            p0, p1 = 1.0, x
            for k in range(2, n + 1):
                p0, p1 = p1, ((2 * k - 1) * x * p1 - (k - 1) * p0) / k
            dp = n * (x * p1 - p0) / (x * x - 1.0)
            dx = -p1 / dp
            x += dx
            if abs(dx) < 1e-15:
                break
        p0, p1 = 1.0, x
        for k in range(2, n + 1):
            p0, p1 = p1, ((2 * k - 1) * x * p1 - (k - 1) * p0) / k
        dp = n * (x * p1 - p0) / (x * x - 1.0)
        nodes.append(x)
        weights.append(2.0 / ((1.0 - x * x) * dp * dp))
    _GL_NODES, _GL_WEIGHTS = nodes, weights
    return nodes, weights


def _integrate(fn, a: float, b: float) -> float:
    nodes, weights = _gauss_legendre()
    half, mid = (b - a) / 2.0, (a + b) / 2.0
    return half * sum(w * fn(mid + half * x) for x, w in zip(nodes, weights))


def _range_cdf_normal(q: float, k: int) -> float:
    """P(range of k iid standard normals <= q)."""
    if q <= 0:
        return 0.0
    inner = lambda z: norm_pdf(z) * (norm_cdf(z) - norm_cdf(z - q)) ** (k - 1)  # noqa: E731
    # Split the z range so the Gauss-Legendre panel resolves the peak well.
    total = sum(_integrate(inner, lo, hi) for lo, hi in
                ((-9.0, -3.0), (-3.0, 0.0), (0.0, 3.0), (3.0, 9.0)))
    return min(1.0, max(0.0, k * total))


def ptukey(q: float, k: int, df: float) -> float:
    """P(Q <= q) for the studentized range with k groups and df error df."""
    if q <= 0:
        return 0.0
    if df > 5000:                      # s concentrates at 1; the normal case
        return _range_cdf_normal(q, k)
    # Mix over s = sqrt(chi2_df / df), whose density is
    #   f(s) = df^(df/2) / (Gamma(df/2) 2^(df/2 - 1)) * s^(df-1) * exp(-df s^2/2)
    log_c = (df / 2.0) * math.log(df) - _log_gamma(df / 2.0) - (df / 2.0 - 1.0) * math.log(2.0)

    def outer(s: float) -> float:
        if s <= 0:
            return 0.0
        log_f = log_c + (df - 1.0) * math.log(s) - df * s * s / 2.0
        if log_f < -700:
            return 0.0
        return math.exp(log_f) * _range_cdf_normal(q * s, k)

    spread = 8.0 / math.sqrt(2.0 * df)
    lo = max(1e-6, 1.0 - 6.0 * spread)
    hi = 1.0 + 6.0 * spread
    total = sum(_integrate(outer, a, b) for a, b in
                ((lo, 1.0 - spread), (1.0 - spread, 1.0 + spread), (1.0 + spread, hi)))
    return min(1.0, max(0.0, total))


def qtukey(p: float, k: int, df: float) -> float:
    """Critical value q such that P(Q <= q) = p."""
    lo, hi = 0.0, 100.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if ptukey(mid, k, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ------------------------------------------------------------------ analyses

def describe(values: list[float], alpha: float) -> dict:
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n) if n else float("nan")
    ordered = sorted(values)

    def quantile(q: float) -> float:
        if n == 1:
            return ordered[0]
        pos = q * (n - 1)
        low = math.floor(pos)
        high = math.ceil(pos)
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

    tcrit = t_ppf(1 - alpha / 2, n - 1) if n > 1 else float("nan")
    return {
        "n": n, "mean": mean, "sd": sd, "se": se,
        "min": ordered[0], "q1": quantile(0.25), "median": quantile(0.5),
        "q3": quantile(0.75), "max": ordered[-1],
        "ci_low": mean - tcrit * se if n > 1 else float("nan"),
        "ci_high": mean + tcrit * se if n > 1 else float("nan"),
        "ci_level": 1 - alpha,
    }


def welch_ttest(a: list[float], b: list[float], alpha: float) -> dict:
    na, nb = len(a), len(b)
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((v - ma) ** 2 for v in a) / (na - 1) if na > 1 else 0.0
    vb = sum((v - mb) ** 2 for v in b) / (nb - 1) if nb > 1 else 0.0
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return {"n1": na, "n2": nb, "mean_diff": ma - mb, "t": float("nan"),
                "df": float("nan"), "p": float("nan"), "cohens_d": float("nan"),
                "hedges_g": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"),
                "note": "zero variance in both groups"}
    t = (ma - mb) / se
    df_num = (va / na + vb / nb) ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = df_num / df_den if df_den else float("nan")
    p = t_sf_two_sided(t, df)
    pooled_sd = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)) if na + nb > 2 else 0.0
    d = (ma - mb) / pooled_sd if pooled_sd else float("nan")
    correction = 1 - 3 / (4 * (na + nb) - 9) if na + nb > 3 else 1.0
    tcrit = t_ppf(1 - alpha / 2, df)
    return {
        "n1": na, "n2": nb, "mean1": ma, "mean2": mb, "mean_diff": ma - mb,
        "t": t, "df": df, "p": p, "cohens_d": d,
        "hedges_g": d * correction if d == d else float("nan"),
        "ci_low": (ma - mb) - tcrit * se, "ci_high": (ma - mb) + tcrit * se,
    }


def mann_whitney(a: list[float], b: list[float]) -> dict:
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda p: p[0])
    ranks = [0.0] * len(combined)
    i = 0
    tie_correction = 0.0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        t = j - i + 1
        tie_correction += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    na, nb, n = len(a), len(b), len(combined)
    u1 = r1 - na * (na + 1) / 2.0
    mu = na * nb / 2.0
    sigma_sq = na * nb / 12.0 * ((n + 1) - tie_correction / (n * (n - 1))) if n > 1 else 0.0
    if sigma_sq <= 0:
        return {"u": u1, "p": float("nan"), "note": "degenerate ranks"}
    z = (u1 - mu) / math.sqrt(sigma_sq)
    z_c = z - math.copysign(0.5 / math.sqrt(sigma_sq), z) if z else 0.0
    return {"u": u1, "z": z_c, "p": 2 * (1 - norm_cdf(abs(z_c))),
            "rank_biserial": 2 * u1 / (na * nb) - 1}


def one_way_anova(groups: dict[str, list[float]], alpha: float) -> dict:
    k = len(groups)
    all_values = [v for vals in groups.values() for v in vals]
    n = len(all_values)
    grand_mean = sum(all_values) / n
    ss_between = sum(len(v) * (sum(v) / len(v) - grand_mean) ** 2 for v in groups.values())
    ss_within = sum((x - sum(v) / len(v)) ** 2 for v in groups.values() for x in v)
    ss_total = ss_between + ss_within
    df_b, df_w = k - 1, n - k
    ms_b = ss_between / df_b if df_b else float("nan")
    ms_w = ss_within / df_w if df_w else float("nan")
    f = ms_b / ms_w if ms_w else float("nan")
    p = f_sf(f, df_b, df_w) if f == f else float("nan")
    eta_sq = ss_between / ss_total if ss_total else float("nan")
    omega_sq = ((ss_between - df_b * ms_w) / (ss_total + ms_w)) if ss_total + ms_w else float("nan")
    return {
        "k_groups": k, "n_total": n, "grand_mean": grand_mean,
        "ss_between": ss_between, "ss_within": ss_within, "ss_total": ss_total,
        "df_between": df_b, "df_within": df_w, "ms_between": ms_b, "ms_within": ms_w,
        "f": f, "f_crit": f_ppf(1 - alpha, df_b, df_w) if df_b and df_w else float("nan"),
        "p": p, "reject": bool(p == p and p < alpha),
        "eta_squared": eta_sq, "omega_squared": omega_sq, "alpha": alpha,
    }


def kruskal_wallis(groups: dict[str, list[float]]) -> dict:
    flat = [(v, name) for name, vals in groups.items() for v in vals]
    flat.sort(key=lambda p: p[0])
    n = len(flat)
    ranks: dict[str, list[float]] = {name: [] for name in groups}
    i = 0
    tie_correction = 0.0
    while i < n:
        j = i
        while j + 1 < n and flat[j + 1][0] == flat[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for idx in range(i, j + 1):
            ranks[flat[idx][1]].append(avg_rank)
        t = j - i + 1
        tie_correction += t ** 3 - t
        i = j + 1
    h = 12.0 / (n * (n + 1)) * sum(
        (sum(r) ** 2) / len(r) for r in ranks.values() if r
    ) - 3 * (n + 1)
    if tie_correction and n > 1:
        h /= 1 - tie_correction / (n ** 3 - n)
    df = len(groups) - 1
    return {"h": h, "df": df, "p": chi2_sf(h, df) if df else float("nan")}


def tukey_hsd(groups: dict[str, list[float]], ms_within: float, df_within: float,
              alpha: float) -> tuple[list[dict], dict[str, str]]:
    names = list(groups)
    k = len(names)
    means = {nm: sum(groups[nm]) / len(groups[nm]) for nm in names}
    q_crit = qtukey(1 - alpha, k, df_within)
    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            a, b = names[i], names[j]
            na, nb = len(groups[a]), len(groups[b])
            se = math.sqrt(ms_within / 2.0 * (1.0 / na + 1.0 / nb))
            diff = means[a] - means[b]
            q = abs(diff) / se if se else float("inf")
            p_adj = 1.0 - ptukey(q, k, df_within)
            margin = q_crit * se
            pairs.append({
                "group1": a, "group2": b, "mean_diff": diff,
                "q": q, "p_adj": p_adj, "reject": bool(p_adj < alpha),
                "ci_low": diff - margin, "ci_high": diff + margin,
            })
    sig = {(p["group1"], p["group2"]): p["reject"] for p in pairs}

    def differs(a: str, b: str) -> bool:
        return sig.get((a, b), sig.get((b, a), False))

    return pairs, compact_letter_display(names, means, differs)


def compact_letter_display(names, means: dict[str, float], differs) -> dict[str, str]:
    """Assign Tukey letters so two groups share a letter iff they do not differ.

    One letter per *maximal clique* of the "not significantly different" graph.
    A greedy first-fit pass is not sufficient: a group placed in an early
    cluster is never reconsidered for a cluster created later, so a middle
    group that differs from nobody can end up sharing a letter with only one
    side of the ranking (e.g. reporting `a` when it belongs to both `a` and
    `b`), silently presenting two non-significant groups as different. Cliques
    are enumerated with Bron-Kerbosch, which is cheap at these group counts.
    """
    ordered = sorted(names, key=lambda nm: -means[nm])
    rank = {nm: i for i, nm in enumerate(ordered)}
    adjacent = {
        nm: {other for other in ordered if other != nm and not differs(nm, other)}
        for nm in ordered
    }

    cliques: list[set[str]] = []

    def expand(current: set[str], candidates: set[str], excluded: set[str]) -> None:
        if not candidates and not excluded:
            cliques.append(set(current))
            return
        for node in list(candidates):
            expand(current | {node},
                   candidates & adjacent[node],
                   excluded & adjacent[node])
            candidates = candidates - {node}
            excluded = excluded | {node}

    expand(set(), set(ordered), set())

    # Letter 'a' goes to the clique containing the highest-mean group.
    cliques.sort(key=lambda clique: min(rank[nm] for nm in clique))
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    letters: dict[str, str] = {nm: "" for nm in ordered}
    for index, clique in enumerate(cliques):
        for nm in clique:
            letters[nm] += alphabet[index % 26]
    return {nm: letters[nm] for nm in ordered}


def pearson(xs: list[float], ys: list[float], alpha: float) -> dict:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return {"n": n, "r": float("nan"), "p": float("nan"),
                "note": "zero variance in one variable"}
    r = sxy / math.sqrt(sxx * syy)
    df = n - 2
    if abs(r) >= 1.0:
        t, p = float("inf"), 0.0
    else:
        t = r * math.sqrt(df / (1 - r * r))
        p = t_sf_two_sided(t, df)
    # Fisher z confidence interval
    if n > 3 and abs(r) < 1:
        z = 0.5 * math.log((1 + r) / (1 - r))
        se_z = 1 / math.sqrt(n - 3)
        crit = norm_ppf(1 - alpha / 2)
        lo, hi = math.tanh(z - crit * se_z), math.tanh(z + crit * se_z)
    else:
        lo = hi = float("nan")
    slope = sxy / sxx
    return {"n": n, "r": r, "r_squared": r * r, "t": t, "df": df, "p": p,
            "ci_low": lo, "ci_high": hi, "slope": slope,
            "intercept": my - slope * mx, "alpha": alpha}


def rankify(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float], alpha: float) -> dict:
    res = pearson(rankify(xs), rankify(ys), alpha)
    res["rho"] = res.pop("r", float("nan"))
    return res


def chi_square(table: dict[str, dict[str, float]], alpha: float) -> dict:
    rows = list(table)
    cols = sorted({c for r in table.values() for c in r})
    observed = [[table[r].get(c, 0.0) for c in cols] for r in rows]
    row_tot = [sum(r) for r in observed]
    col_tot = [sum(observed[i][j] for i in range(len(rows))) for j in range(len(cols))]
    total = sum(row_tot)
    if total <= 0:
        return {"note": "empty table"}
    expected = [[row_tot[i] * col_tot[j] / total for j in range(len(cols))]
                for i in range(len(rows))]
    chi2 = sum(
        (observed[i][j] - expected[i][j]) ** 2 / expected[i][j]
        for i in range(len(rows)) for j in range(len(cols)) if expected[i][j] > 0
    )
    df = (len(rows) - 1) * (len(cols) - 1)
    p = chi2_sf(chi2, df) if df > 0 else float("nan")
    small = sum(1 for r in expected for e in r if e < 5)
    min_dim = min(len(rows), len(cols))
    cramers_v = math.sqrt(chi2 / (total * (min_dim - 1))) if min_dim > 1 else float("nan")
    return {
        "rows": rows, "cols": cols, "observed": observed, "expected": expected,
        "row_totals": row_tot, "col_totals": col_tot, "n": total,
        "chi2": chi2, "df": df, "p": p, "reject": bool(p == p and p < alpha),
        "cramers_v": cramers_v, "cells_expected_lt_5": small,
        "total_cells": len(rows) * len(cols), "alpha": alpha,
    }


def wilson_ci(successes: int, n: int, alpha: float) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    z = norm_ppf(1 - alpha / 2)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - margin, center + margin


def two_proportion_test(s1: int, n1: int, s2: int, n2: int, alpha: float) -> dict:
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se_pooled == 0:
        return {"note": "degenerate proportions"}
    z = (p1 - p2) / se_pooled
    se_unpooled = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    crit = norm_ppf(1 - alpha / 2)
    return {
        "p1": p1, "p2": p2, "diff": p1 - p2, "z": z,
        "p": 2 * (1 - norm_cdf(abs(z))),
        "ci_low": (p1 - p2) - crit * se_unpooled,
        "ci_high": (p1 - p2) + crit * se_unpooled,
        "h": 2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2)),
    }


# ------------------------------------------------------------------ renderers

def g(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if value != value:
            return "n/a"
        if value == 0:
            return "0"
        if abs(value) < 1e-4:
            return f"{value:.2e}"
        return f"{value:.5g}"
    return str(value)


def p_text(p: float, alpha: float) -> str:
    if p != p:
        return "p unavailable"
    if p < 1e-6:
        return f"p<1e-06 (significant at alpha={alpha})"
    return f"p={g(p)} ({'significant' if p < alpha else 'NOT significant'} at alpha={alpha})"


def size_caveat(n: int) -> str:
    if n < 15:
        return (f"  CAUTION: N={n} is very small - treat this as inconclusive rather "
                "than as evidence either way.")
    if n < 30:
        return f"  NOTE: N={n} is small; the test has low power."
    return ""


def render_groups(groups: dict[str, list[float]], alpha: float) -> str:
    out = ["Per-group descriptive statistics:"]
    for name, values in groups.items():
        d = describe(values, alpha)
        out.append(
            "  %-45s N=%-5d mean=%-9s sd=%-9s median=%-7s range=[%s, %s]  %.0f%% CI [%s, %s]"
            % (name[:45], d["n"], g(d["mean"]), g(d["sd"]), g(d["median"]),
               g(d["min"]), g(d["max"]), 100 * d["ci_level"], g(d["ci_low"]), g(d["ci_high"]))
        )
    total_n = sum(len(v) for v in groups.values())
    out.append(size_caveat(total_n).rstrip())

    if len(groups) < 2:
        out.append("\nOnly one group present - no comparison possible.")
        return "\n".join(line for line in out if line)

    if len(groups) == 2:
        (n1, a), (n2, b) = list(groups.items())
        t = welch_ttest(a, b, alpha)
        out.append("\nWelch two-sample t-test (%s vs %s):" % (n1, n2))
        out.append(
            "  mean diff=%s, t=%s, df=%s, %s" % (g(t["mean_diff"]), g(t["t"]), g(t["df"]),
                                                 p_text(t["p"], alpha))
        )
        out.append("  %.0f%% CI for the difference: [%s, %s]"
                   % (100 * (1 - alpha), g(t["ci_low"]), g(t["ci_high"])))
        out.append("  effect size: Cohen's d=%s, Hedges' g=%s"
                   % (g(t["cohens_d"]), g(t["hedges_g"])))
        mw = mann_whitney(a, b)
        out.append("  non-parametric check (Mann-Whitney U): U=%s, %s"
                   % (g(mw.get("u")), p_text(mw.get("p", float("nan")), alpha)))
        return "\n".join(line for line in out if line)

    an = one_way_anova(groups, alpha)
    out.append("\nOne-way ANOVA:")
    out.append(
        "  F(%s, %s)=%s (F_crit=%s), %s"
        % (an["df_between"], an["df_within"], g(an["f"]), g(an["f_crit"]),
           p_text(an["p"], alpha))
    )
    out.append("  SS_between=%s, SS_within=%s, SS_total=%s, MS_within=%s"
               % (g(an["ss_between"]), g(an["ss_within"]), g(an["ss_total"]), g(an["ms_within"])))
    out.append("  effect size: eta^2=%s, omega^2=%s (share of variance explained by group)"
               % (g(an["eta_squared"]), g(an["omega_squared"])))
    out.append("  verdict: %s" % ("at least one group mean differs" if an["reject"]
                                  else "no detectable difference between group means"))

    kw = kruskal_wallis(groups)
    out.append("  non-parametric check (Kruskal-Wallis): H=%s, df=%s, %s"
               % (g(kw["h"]), kw["df"], p_text(kw["p"], alpha)))

    pairs, letters = tukey_hsd(groups, an["ms_within"], an["df_within"], alpha)
    means = {nm: sum(v) / len(v) for nm, v in groups.items()}
    out.append("\nTukey HSD groups (shared letter = NOT significantly different):")
    for name, letter in letters.items():
        out.append("  %-45s mean=%-9s group %s" % (name[:45], g(means[name]), letter))
    out.append("\nTukey HSD pairwise comparisons:")
    for pair in pairs:
        out.append(
            "  %s vs %s: diff=%s, p-adj=%s, %.0f%% CI [%s, %s] -> %s"
            % (pair["group1"][:30], pair["group2"][:30], g(pair["mean_diff"]),
               g(pair["p_adj"]), 100 * (1 - alpha), g(pair["ci_low"]), g(pair["ci_high"]),
               "significant" if pair["reject"] else "not significant")
        )
    return "\n".join(line for line in out if line)


def render_paired(xs: list[float], ys: list[float], names: tuple[str, str], alpha: float) -> str:
    pe = pearson(xs, ys, alpha)
    sp = spearman(xs, ys, alpha)
    out = [f"Paired analysis: x={names[0]}, y={names[1]} (one row per analytical unit)",
           f"  N pairs = {pe['n']}"]
    out.append(size_caveat(pe["n"]).rstrip())
    r = pe.get("r", float("nan"))
    strength = ""
    if r == r:
        a = abs(r)
        strength = " strong" if a > 0.7 else " moderate" if a > 0.5 else \
                   " weak" if a > 0.3 else " very weak"
    out.append("\nPearson correlation: r=%s%s, r^2=%s, %s"
               % (g(r), strength, g(pe.get("r_squared")), p_text(pe.get("p", float("nan")), alpha)))
    out.append("  %.0f%% CI for r: [%s, %s]" % (100 * (1 - alpha), g(pe.get("ci_low")),
                                                g(pe.get("ci_high"))))
    out.append("  least-squares fit: y = %s + %s * x" % (g(pe.get("intercept")), g(pe.get("slope"))))
    out.append("Spearman rank correlation: rho=%s, %s"
               % (g(sp.get("rho")), p_text(sp.get("p", float("nan")), alpha)))
    out.append("\nDescriptives:")
    for label, values in ((names[0], xs), (names[1], ys)):
        d = describe(values, alpha)
        out.append("  %-30s N=%d mean=%s sd=%s min=%s max=%s"
                   % (label[:30], d["n"], g(d["mean"]), g(d["sd"]), g(d["min"]), g(d["max"])))
    if pe.get("p", 1) is not None and pe.get("p", 1) == pe.get("p", 1) and pe["p"] >= alpha:
        out.append("\nInterpretation: the correlation is not statistically significant at "
                   f"alpha={alpha}. That is inconclusive - it is not evidence that no "
                   "relationship exists.")
    return "\n".join(line for line in out if line)


def render_crosstab(table: dict[str, dict[str, float]], alpha: float) -> str:
    res = chi_square(table, alpha)
    if "chi2" not in res:
        return "Chi-square not computable: " + res.get("note", "unknown")
    cols = res["cols"]
    width = max([len(r) for r in res["rows"]] + [12])
    out = ["Contingency table (observed counts):",
           "  " + "".ljust(width) + "".join(c[:14].rjust(15) for c in cols) + "     total"]
    for i, row in enumerate(res["rows"]):
        out.append("  " + row[:width].ljust(width)
                   + "".join(g(v).rjust(15) for v in res["observed"][i])
                   + g(res["row_totals"][i]).rjust(10))
    out.append("  " + "total".ljust(width) + "".join(g(v).rjust(15) for v in res["col_totals"])
               + g(res["n"]).rjust(10))
    out.append("\nChi-square test of independence:")
    out.append("  chi2=%s, df=%s, N=%s, %s"
               % (g(res["chi2"]), res["df"], g(res["n"]), p_text(res["p"], alpha)))
    out.append("  effect size: Cramer's V=%s" % g(res["cramers_v"]))
    if res["cells_expected_lt_5"]:
        out.append("  CAUTION: %d of %d cells have expected count < 5 - the chi-square "
                   "approximation is unreliable here; report the raw counts alongside it."
                   % (res["cells_expected_lt_5"], res["total_cells"]))
    out.append("  verdict: %s" % ("the two variables are associated" if res["reject"]
                                  else "no detectable association (independence not rejected)"))
    out.append("\nRow percentages:")
    for i, row in enumerate(res["rows"]):
        total = res["row_totals"][i] or 1
        parts = ", ".join(f"{c}={100 * res['observed'][i][j] / total:.1f}%"
                          for j, c in enumerate(cols))
        out.append(f"  {row}: {parts} (n={g(total)})")
    return "\n".join(out)


def render_proportion(groups: dict[str, tuple[int, int]], alpha: float) -> str:
    out = ["Proportions with %.0f%% Wilson confidence intervals:" % (100 * (1 - alpha))]
    for name, (successes, n) in groups.items():
        lo, hi = wilson_ci(successes, n, alpha)
        out.append("  %-40s %d/%d = %.1f%%  CI [%.1f%%, %.1f%%]"
                   % (name[:40], successes, n, 100 * successes / n if n else float("nan"),
                      100 * lo, 100 * hi))
        caveat = size_caveat(n)
        if caveat:
            out.append(caveat)
    names = list(groups)
    if len(names) >= 2:
        out.append("\nPairwise two-proportion z-tests:")
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                s1, n1 = groups[a]
                s2, n2 = groups[b]
                res = two_proportion_test(s1, n1, s2, n2, alpha)
                if "note" in res:
                    out.append(f"  {a} vs {b}: {res['note']}")
                    continue
                out.append(
                    "  %s vs %s: %.1f%% vs %.1f%% (diff=%.1f pp), z=%s, %s, CI [%.1f pp, %.1f pp]"
                    % (a[:25], b[:25], 100 * res["p1"], 100 * res["p2"], 100 * res["diff"],
                       g(res["z"]), p_text(res["p"], alpha),
                       100 * res["ci_low"], 100 * res["ci_high"])
                )
        if len(names) > 2:
            k = len(names) * (len(names) - 1) // 2
            out.append("  NOTE: %d pairwise tests were run; with no correction the family-wise "
                       "error rate exceeds alpha. Bonferroni threshold = %s."
                       % (k, g(alpha / k)))
        table = {name: {"yes": float(s), "no": float(n - s)} for name, (s, n) in groups.items()}
        out.append("\nOmnibus test across all groups (chi-square on the yes/no table):")
        res = chi_square(table, alpha)
        out.append("  chi2=%s, df=%s, %s, Cramer's V=%s"
                   % (g(res["chi2"]), res["df"], p_text(res["p"], alpha), g(res["cramers_v"])))
    return "\n".join(line for line in out if line)


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("mode", choices=("describe", "groups", "paired", "crosstab", "proportion"))
    ap.add_argument("--sql", required=True, help="Read-only SQL producing the analysis columns.")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--group-col", help="Grouping column (name or 0-based index).")
    ap.add_argument("--value-col", help="Numeric value column (name or 0-based index).")
    ap.add_argument("--x-col", help="paired mode: first numeric column.")
    ap.add_argument("--y-col", help="paired mode: second numeric column.")
    ap.add_argument("--row-col", help="crosstab mode: row label column.")
    ap.add_argument("--col-col", help="crosstab mode: column label column.")
    ap.add_argument("--count-col", help="crosstab mode: pre-aggregated count column.")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = ap.parse_args()

    header, rows = fetch_rows(args.sql)
    alpha = args.alpha

    if args.mode == "describe":
        idx = pick(header, args.value_col, len(header) - 1 if len(header) > 1 else 0)
        values = [v for v in (to_float(r[idx]) for r in rows) if v is not None]
        if not values:
            sys.exit(f"No numeric values in column '{header[idx]}'. Check the value path "
                     "(answer.value vs answerData->>'optionAnswer').")
        result = describe(values, alpha)
        dropped = len(rows) - len(values)
        if args.json:
            print(json.dumps({"column": header[idx], "dropped_non_numeric": dropped,
                              **result}, indent=2))
        else:
            print(f"Descriptive statistics for '{header[idx]}'"
                  + (f" ({dropped} non-numeric row(s) skipped)" if dropped else ""))
            print("  N=%d, mean=%s, sd=%s, se=%s" % (result["n"], g(result["mean"]),
                                                     g(result["sd"]), g(result["se"])))
            print("  min=%s, Q1=%s, median=%s, Q3=%s, max=%s"
                  % (g(result["min"]), g(result["q1"]), g(result["median"]),
                     g(result["q3"]), g(result["max"])))
            print("  %.0f%% CI for the mean: [%s, %s]"
                  % (100 * result["ci_level"], g(result["ci_low"]), g(result["ci_high"])))
            caveat = size_caveat(result["n"])
            if caveat:
                print(caveat)
        return 0

    if args.mode == "groups":
        gi = pick(header, args.group_col, 0)
        vi = pick(header, args.value_col, len(header) - 1)
        groups: dict[str, list[float]] = {}
        dropped = 0
        for row in rows:
            value = to_float(row[vi])
            if value is None:
                dropped += 1
                continue
            groups.setdefault(row[gi] or "(null)", []).append(value)
        groups = {k: v for k, v in groups.items() if v}
        if not groups:
            sys.exit(f"No numeric values in column '{header[vi]}'.")
        if args.json:
            payload: dict = {"group_column": header[gi], "value_column": header[vi],
                             "dropped_non_numeric": dropped,
                             "descriptives": {k: describe(v, alpha) for k, v in groups.items()}}
            if len(groups) == 2:
                a, b = list(groups.values())
                payload["welch_t"] = welch_ttest(a, b, alpha)
                payload["mann_whitney"] = mann_whitney(a, b)
            elif len(groups) > 2:
                an = one_way_anova(groups, alpha)
                pairs, letters = tukey_hsd(groups, an["ms_within"], an["df_within"], alpha)
                payload["anova"] = an
                payload["kruskal_wallis"] = kruskal_wallis(groups)
                payload["tukey_pairs"] = pairs
                payload["tukey_letters"] = letters
            print(json.dumps(payload, indent=2, default=str))
        else:
            if dropped:
                print(f"({dropped} row(s) with non-numeric '{header[vi]}' skipped)")
            print(render_groups(groups, alpha))
        return 0

    if args.mode == "paired":
        xi = pick(header, args.x_col, len(header) - 2 if len(header) > 1 else 0)
        yi = pick(header, args.y_col, len(header) - 1)
        xs, ys, dropped = [], [], 0
        for row in rows:
            x, y = to_float(row[xi]), to_float(row[yi])
            if x is None or y is None:
                dropped += 1
                continue
            xs.append(x)
            ys.append(y)
        if len(xs) < 3:
            sys.exit(f"Only {len(xs)} complete pair(s) after dropping {dropped} incomplete "
                     "row(s) - too few to correlate. Check that both questions were answered "
                     "by the same analytical unit (enrollment, or enrollment+product).")
        if args.json:
            print(json.dumps({"x": header[xi], "y": header[yi], "dropped_incomplete": dropped,
                              "pearson": pearson(xs, ys, alpha),
                              "spearman": spearman(xs, ys, alpha)}, indent=2, default=str))
        else:
            if dropped:
                print(f"({dropped} row(s) missing one of the two values were dropped)")
            print(render_paired(xs, ys, (header[xi], header[yi]), alpha))
        return 0

    if args.mode == "crosstab":
        ri = pick(header, args.row_col, 0)
        ci = pick(header, args.col_col, 1)
        cnt = None
        if args.count_col is not None:
            cnt = pick(header, args.count_col, 2)
        elif len(header) > 2:
            cnt = 2
        table: dict[str, dict[str, float]] = {}
        for row in rows:
            weight = 1.0
            if cnt is not None:
                weight = to_float(row[cnt]) or 0.0
            key = row[ri] or "(null)"
            col = row[ci] or "(null)"
            table.setdefault(key, {}).setdefault(col, 0.0)
            table[key][col] += weight
        if args.json:
            print(json.dumps(chi_square(table, alpha), indent=2, default=str))
        else:
            print(render_crosstab(table, alpha))
        return 0

    # proportion
    gi = pick(header, args.group_col, 0)
    vi = pick(header, args.value_col, len(header) - 1)
    prop: dict[str, list[int]] = {}
    dropped = 0
    for row in rows:
        flag = to_bool(row[vi])
        if flag is None:
            value = to_float(row[vi])
            if value is None:
                dropped += 1
                continue
            flag = value != 0
        bucket = prop.setdefault(row[gi] or "(null)", [0, 0])
        bucket[1] += 1
        if flag:
            bucket[0] += 1
    if not prop:
        sys.exit(f"No boolean/0-1 values in column '{header[vi]}'.")
    groups_p = {k: (v[0], v[1]) for k, v in prop.items()}
    if args.json:
        print(json.dumps(
            {"group_column": header[gi], "value_column": header[vi],
             "dropped": dropped,
             "groups": {k: {"successes": s, "n": n, "proportion": s / n,
                            "ci": wilson_ci(s, n, alpha)} for k, (s, n) in groups_p.items()}},
            indent=2, default=str))
    else:
        if dropped:
            print(f"({dropped} unparseable row(s) skipped)")
        print(render_proportion(groups_p, alpha))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
