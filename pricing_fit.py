"""Offline fit of the dealer acceptance curve from data/pricing_probe.json.

Outputs data/lane_prior.json with:
  - the global empirical win probability by quote/list ratio bin (width 0.05)
  - per-lane (directed place pair), per-origin and per-destination aggregates:
    win rate at ratio > 1.2 vs <= 1.2 with counts, plus a logistic fit of
    p(win) on ratio whose 50% point is the lane's threshold ratio ("lane multiplier")
  - the global base threshold

Only single-option requests are used for the clean fit: with several quoted
options the dealer's choice between them contaminates a per-quote win rate.

Usage: .venv/bin/python pricing_fit.py [--in data/pricing_probe.json] [--out data/lane_prior.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict

BIN_W = 0.05
R_LO, R_HI = 0.70, 1.95
HIGH = 1.20  # "expensive" quote threshold for the win-rate split
MIN_N_LANE = 5
MIN_N_PLACE = 10
MIN_N_FIT = 5


# ---------------------------------------------------------------- logistic fit
def _sigmoid(z: float) -> float:
    if z < -35:
        return 1e-15
    if z > 35:
        return 1.0 - 1e-15
    return 1.0 / (1.0 + math.exp(-z))


def _nll(rows, b1: float, b0: float, mu: float = 0.0) -> float:
    """Negative log-likelihood of the centred logistic model (grid fallback objective)."""
    s = 0.0
    for x, y in rows:
        z = b0 + b1 * (x - mu)
        s += -math.log(_sigmoid(z)) if y else -math.log(1.0 - _sigmoid(z))
    return s


def logit_fit(rows: list[tuple[float, int]], ridge: float = 1e-3):
    """IRLS logistic fit of y ~ sigmoid(b0 + b1*x).

    Returns (b0, b1, se_threshold, ok). ok=False when the data do not separate
    the 50% point inside the sampled range (no variation, or a degenerate fit).
    """
    n = len(rows)
    if n < MIN_N_FIT:
        return (None, None, None, False)
    ys = [y for _, y in rows]
    if all(y == 1 for y in ys) or all(y == 0 for y in ys):
        return (None, None, None, False)
    mu = sum(x for x, _ in rows) / n
    b0, b1 = 0.0, -2.0
    for _ in range(100):
        g0 = g1 = 0.0
        h00 = h01 = h11 = 0.0
        for x, y in rows:
            xc = x - mu
            p = _sigmoid(b0 + b1 * xc)
            w = max(p * (1 - p), 1e-9)
            g0 += y - p
            g1 += (y - p) * xc
            h00 += w
            h01 += w * xc
            h11 += w * xc * xc
        g0 -= ridge * b0
        g1 -= ridge * b1
        h00 += ridge
        h11 += ridge
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            return (None, None, None, False)
        d0 = (h11 * g0 - h01 * g1) / det
        d1 = (-h01 * g0 + h00 * g1) / det
        # damped Newton
        step = 1.0
        for _ in range(30):
            nb0, nb1 = b0 + step * d0, b1 + step * d1
            if _finite(nb0) and _finite(nb1):
                b0, b1 = nb0, nb1
                break
            step *= 0.5
        if abs(step * d0) + abs(step * d1) < 1e-10:
            break
    if not (_finite(b0) and _finite(b1)) or b1 >= 0:
        return (None, None, None, False)
    # threshold in centred x: -b0/b1 ; convert to raw ratio
    thr_c = -b0 / b1
    thr = thr_c + mu
    # delta-method se via inverse Hessian at optimum
    h00 = h01 = h11 = 0.0
    for x, y in rows:
        xc = x - mu
        p = _sigmoid(b0 + b1 * xc)
        w = max(p * (1 - p), 1e-9)
        h00 += w
        h01 += w * xc
        h11 += w * xc * xc
    det = h00 * h11 - h01 * h01
    if abs(det) < 1e-12:
        return (b0, b1, None, False)
    v00 = h11 / det
    v01 = -h01 / det
    v11 = h00 / det
    g0 = -1.0 / b1
    g1 = b0 / (b1 * b1)
    var = g0 * g0 * v00 + 2 * g0 * g1 * v01 + g1 * g1 * v11
    se = math.sqrt(var) if var > 0 else None
    ok = thr is not None and R_LO - 0.3 <= thr <= R_HI + 0.3
    # return the intercept of the uncentred model, so that threshold == -b0/b1
    return (b0 - b1 * mu, b1, se, ok)


def _finite(v) -> bool:
    return v is not None and not (math.isnan(v) or math.isinf(v))


def grid_threshold(rows: list[tuple[float, int]]):
    """Fallback: coarse+fine grid MLE over (b1, b0), return the 50% ratio."""
    if len(rows) < MIN_N_FIT:
        return None
    mu = sum(x for x, _ in rows) / len(rows)

    def nll(b1, b0):
        return _nll(rows, b1, b0, mu)

    best = (float("inf"), -2.0, 0.0)
    for i in range(1, 61):
        b1 = -0.05 - i * 0.5
        for j in range(-40, 41):
            b0 = j * 0.25
            v = nll(b1, b0)
            if v < best[0]:
                best = (v, b1, b0)
    v, b1, b0 = best
    for _ in range(3):
        step1, step0 = 0.25, 0.05
        for _ in range(40):
            improved = False
            for db1, db0 in (
                (step1, 0), (-step1, 0), (0, step0), (0, -step0),
                (step1, step0), (-step1, -step0), (step1, -step0), (-step1, step0),
            ):
                nb1, nb0 = b1 + db1, b0 + db0
                if nb1 >= 0:
                    continue
                nv = nll(nb1, nb0)
                if nv < v - 1e-12:
                    v, b1, b0 = nv, nb1, nb0
                    improved = True
            if not improved:
                step1 *= 0.5
                step0 *= 0.5
    return (-b0 / b1) + mu if b1 < 0 else None


def fit_group(rows: list[tuple[float, int]]) -> dict:
    n = len(rows)
    wins = sum(y for _, y in rows)
    hi = [y for x, y in rows if x > HIGH]
    lo = [y for x, y in rows if x <= HIGH]
    base = {
        "n": n,
        "wins": wins,
        "winRate": round(wins / n, 4) if n else None,
        "nHigh": len(hi),
        "winRateHigh": round(sum(hi) / len(hi), 4) if hi else None,
        "nLow": len(lo),
        "winRateLow": round(sum(lo) / len(lo), 4) if lo else None,
    }
    if n < MIN_N_FIT or wins == 0 or wins == n:
        # no separation: the threshold lies outside the sampled ratios
        xs = [x for x, _ in rows]
        out = dict(base, threshold=None, logThreshold=None, se=None, slope=None,
                   intercept=None, confidence="none")
        if wins == n and xs:
            out["note"] = "all wins"
            out["thresholdLowerBound"] = round(max(xs), 4)
        elif wins == 0 and xs:
            out["note"] = "all losses"
            out["thresholdUpperBound"] = round(min(xs), 4)
        else:
            out["note"] = "too few samples"
        return out
    b0, b1, se, ok = logit_fit(rows)
    thr = None
    if ok and b0 is not None:
        thr = -b0 / b1
    if thr is None:
        thr = grid_threshold(rows)
        se = None
        ok = thr is not None and R_LO - 0.35 <= thr <= R_HI + 0.35
    if not ok or thr is None:
        # quasi-separated data (or a 50% point far outside the quoted range):
        # report bounds instead of a number the MLE cannot support
        out = dict(base, threshold=None, logThreshold=None, se=None, slope=None,
                   intercept=None, confidence="none",
                   note="no 50% crossing inside the quoted range")
        won = [x for x, y in rows if y]
        lost = [x for x, y in rows if not y]
        out["thresholdLowerBound"] = round(min(won), 4) if won else None
        out["thresholdUpperBound"] = round(max(lost), 4) if lost else None
        return out
    conf = "none"
    if thr is not None:
        if se is not None and n >= 25 and se <= 0.15:
            conf = "high"
        elif n >= 12 and (se is None or se <= 0.35):
            conf = "medium"
        else:
            conf = "low"
    return {
        "n": n,
        "wins": wins,
        "winRate": round(wins / n, 4) if n else None,
        "nHigh": len(hi),
        "winRateHigh": round(sum(hi) / len(hi), 4) if hi else None,
        "nLow": len(lo),
        "winRateLow": round(sum(lo) / len(lo), 4) if lo else None,
        "threshold": round(thr, 4),
        "logThreshold": round(math.log(thr), 4) if thr > 0 else None,
        "se": round(se, 4) if se is not None else None,
        "slope": round(b1, 4) if b1 is not None else None,
        "intercept": round(b0, 4) if b0 is not None else None,
        "confidence": conf,
    }


# ------------------------------------------------------------------- bin curve
def bin_index(r: float) -> int:
    return int(math.floor((r - R_LO) / BIN_W + 1e-9))


def binned(rows: list[dict]) -> list[dict]:
    bins: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        if row.get("ratio") is None:
            continue
        bins[bin_index(row["ratio"])].append(1 if row["optionWon"] else 0)
    out = []
    lo_i = min(bins) if bins else 0
    hi_i = max(bins) if bins else -1
    for i in range(lo_i, hi_i + 1):
        ys = bins.get(i, [])
        out.append(
            {
                "lo": round(R_LO + i * BIN_W, 3),
                "hi": round(R_LO + (i + 1) * BIN_W, 3),
                "mid": round(R_LO + (i + 0.5) * BIN_W, 3),
                "n": len(ys),
                "wins": sum(ys),
                "winRate": round(sum(ys) / len(ys), 4) if ys else None,
            }
        )
    return out


def interp_half(curve: list[dict]):
    """Empirical 50% crossing from the binned curve (linear between bin centres)."""
    pts = [(b["mid"], b["winRate"], b["n"]) for b in curve if b["winRate"] is not None and b["n"] >= 5]
    for (x0, y0, _), (x1, y1, _) in zip(pts, pts[1:]):
        if (y0 - 0.5) * (y1 - 0.5) <= 0 and y0 != y1:
            return round(x0 + (0.5 - y0) * (x1 - x0) / (y1 - y0), 4)
    return None


def monotonic_report(curve: list[dict]) -> dict:
    pts = [(b["lo"], b["winRate"], b["n"]) for b in curve if b["winRate"] is not None and b["n"] >= 5]
    ups, worst = 0, 0.0
    for (_, y0, _), (_, y1, _) in zip(pts, pts[1:]):
        if y1 > y0 + 1e-9:
            ups += 1
            worst = max(worst, y1 - y0)
    # rank correlation between the bin centre and its win rate: -1 is a perfect monotone curve
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r

    rho = None
    if len(pts) >= 4:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        rx, ry = rank(xs), rank(ys)
        n = len(pts)
        mx, my = sum(rx) / n, sum(ry) / n
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
        dy = math.sqrt(sum((b - my) ** 2 for b in ry))
        rho = round(num / (dx * dy), 4) if dx and dy else None
    return {
        "binsUsed": len(pts),
        "increasingSteps": ups,
        "worstIncrease": round(worst, 4),
        "spearmanBinMidVsWinRate": rho,
    }


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/pricing_probe.json")
    ap.add_argument("--out", default="data/lane_prior.json")
    args = ap.parse_args()

    blob = json.load(open(args.inp))
    rows = blob["rows"]
    meta = blob.get("meta", {})

    quoted = [r for r in rows if r.get("quoteCents")]
    clean = [r for r in quoted if r.get("optionCount") == 1]
    multi = [r for r in quoted if (r.get("optionCount") or 1) > 1]
    unquoted = [r for r in rows if not r.get("quoteCents")]

    print(
        f"rows={len(rows)} quoted={len(quoted)} clean(single-option)={len(clean)} "
        f"multi-option={len(multi)} unquoted={len(unquoted)} seeds={meta.get('seedsDone')}"
    )
    if len(clean) < 100:
        print("WARNING: too few single-option rows for a reliable fit", file=sys.stderr)

    curve = binned(clean)
    curve_all = binned(quoted)

    print("\nratio bin      clean n   win%     all n   win%")
    for c, a in zip(curve, curve_all):
        cw = f"{100 * c['winRate']:5.1f}" if c["winRate"] is not None else "    -"
        aw = f"{100 * a['winRate']:5.1f}" if a["winRate"] is not None else "    -"
        print(f"{c['lo']:.2f}-{c['hi']:.2f}   {c['n']:7d}  {cw}    {a['n']:7d}  {aw}")
    mono = monotonic_report(curve)
    half = interp_half(curve)
    print(f"\nbins used={mono['binsUsed']} increasing steps={mono['increasingSteps']} "
          f"worst increase={mono['worstIncrease']:.3f}   empirical 50% crossing={half}")

    # ---- global logistic fit on clean rows
    gl = fit_group([(r["ratio"], 1 if r["optionWon"] else 0) for r in clean])
    print(
        f"global threshold={gl['threshold']} (se {gl['se']}, {gl['confidence']}, n={gl['n']}) "
        f"win@ratio>1.2={gl['winRateHigh']} win@<=1.2={gl['winRateLow']}"
    )

    # ---- groups
    def group(key_fn, min_n):
        acc: dict[str, list[tuple[float, int]]] = defaultdict(list)
        for r in clean:
            k = key_fn(r)
            if k is None:
                continue
            acc[k].append((r["ratio"], 1 if r["optionWon"] else 0))
        out = {}
        for k, v in acc.items():
            if len(v) < min_n:
                continue
            g = fit_group(v)
            if gl["threshold"] and g["threshold"]:
                g["multiplier"] = round(g["threshold"] / gl["threshold"], 4)
            out[k] = g
        return out

    lanes = group(lambda r: r.get("laneKey"), MIN_N_LANE)
    lanes_strict = group(
        lambda r: r.get("laneKey") if r.get("legCount") == 1 and not r.get("privatePickup")
        and not r.get("privateDelivery") else None,
        MIN_N_LANE,
    )
    origins = group(lambda r: r["lane"][0], MIN_N_PLACE)
    destinations = group(lambda r: r["lane"][1], MIN_N_PLACE)

    # ---- compact lane table
    def show(title, groups, min_n):
        sizeable = {k: g for k, g in groups.items() if g["threshold"] is not None}
        order = sorted(sizeable.items(), key=lambda kv: kv[1]["threshold"])
        print(f"\n{title}: {len(sizeable)} groups with a 50% threshold "
              f"(of {len(groups)} with n>={min_n})")
        print(f"  {'group':<26} {'n':>4} {'win%':>5} {'>1.2':>5} {'<=1.2':>5} {'thr':>6} {'mult':>5} conf")
        for label, sel in (("HARDEST", order[:10]), ("EASIEST", order[-10:][::-1])):
            print(f"  {label}")
            for k, g in sel:
                hi = f"{100 * g['winRateHigh']:4.0f}" if g["winRateHigh"] is not None else "   -"
                lo = f"{100 * g['winRateLow']:4.0f}" if g["winRateLow"] is not None else "   -"
                mult = f"{g.get('multiplier', 0):5.2f}"
                print(
                    f"  {k:<26} {g['n']:4d} {100 * g['winRate']:5.1f} {hi:>5} {lo:>5} "
                    f"{g['threshold']:6.2f} {mult} {g['confidence']}"
                )

    show("LANES", lanes, MIN_N_LANE)
    show("LANES with n>=10", {k: g for k, g in lanes.items() if g["n"] >= 10}, 10)
    show("ORIGINS", origins, MIN_N_PLACE)
    show("DESTINATIONS", destinations, MIN_N_PLACE)

    # ---- group stats and a cross-check: is the measured threshold tracking lane difficulty?
    def add_stats(groups, key_fn, lane_stats=True):
        agg: dict[str, dict] = defaultdict(lambda: {"fee": [], "km": [], "mins": [], "list": []})
        for r in clean:
            k = key_fn(r)
            if k is None:
                continue
            a = agg[k]
            a["fee"].append(r["referenceFeeCents"] / r["listPriceCents"])
            a["list"].append(r["listPriceCents"])
            if lane_stats:
                if r.get("laneKm") is not None:
                    a["km"].append(r["laneKm"])
                if r.get("laneMinutes") is not None:
                    a["mins"].append(r["laneMinutes"])
        for k, g in groups.items():
            a = agg.get(k)
            if not a:
                continue
            g["meanFeeRatio"] = round(sum(a["fee"]) / len(a["fee"]), 4) if a["fee"] else None
            g["meanListPriceCents"] = round(sum(a["list"]) / len(a["list"]), 0) if a["list"] else None
            if lane_stats:
                g["meanLaneKm"] = round(sum(a["km"]) / len(a["km"]), 2) if a["km"] else None
                g["meanLaneMinutes"] = (
                    round(sum(a["mins"]) / len(a["mins"]), 1) if a["mins"] else None
                )

    add_stats(lanes, lambda r: r.get("laneKey"))
    add_stats(origins, lambda r: r["lane"][0], lane_stats=False)
    add_stats(destinations, lambda r: r["lane"][1], lane_stats=False)

    def spearman(xs: list[float], ys: list[float]):
        def ranks(v):
            order = sorted(range(len(v)), key=lambda i: v[i])
            rk = [0.0] * len(v)
            i = 0
            while i < len(order):
                j = i
                while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                    j += 1
                avg = (i + j) / 2.0 + 1.0
                for t in range(i, j + 1):
                    rk[order[t]] = avg
                i = j + 1
            return rk

        n = len(xs)
        if n < 4:
            return None
        rx, ry = ranks(xs), ranks(ys)
        mx, my = sum(rx) / n, sum(ry) / n
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
        dy = math.sqrt(sum((b - my) ** 2 for b in ry))
        return round(num / (dx * dy), 4) if dx and dy else None

    val = {}
    for name, groups, stats in (
        ("lanes", lanes, ("meanFeeRatio", "meanLaneKm", "meanLaneMinutes", "meanListPriceCents")),
        ("origins", origins, ("meanFeeRatio", "meanListPriceCents")),
        ("destinations", destinations, ("meanFeeRatio", "meanListPriceCents")),
    ):
        for stat in stats:
            pts = [
                (g["threshold"], g[stat])
                for g in groups.values()
                if g.get("threshold") is not None and g.get(stat) is not None
            ]
            rho = spearman([p[0] for p in pts], [p[1] for p in pts])
            val[f"{name}_threshold_vs_{stat}"] = {"rho": rho, "n": len(pts)}
            print(f"validation: {name} threshold vs {stat}: rho={rho} (n={len(pts)})")
    val["note"] = ("the reference fee over the list price proxies how hard a lane is for drivers; "
                   "a positive rho means the dealer accepts dearer quotes where drivers cost more, "
                   "i.e. the competitor is dearer there. Weak rho at lane level is expected: the "
                   "per-lane sample is thin, so trust the place-level aggregates more.")

    out = {
        "meta": {
            "source": args.inp,
            "seeds": meta.get("seedsDone"),
            "level": 8,
            "rows": len(rows),
            "rowsQuoted": len(quoted),
            "rowsCleanSingleOption": len(clean),
            "rowsMultiOption": len(multi),
            "rowsUnquoted": len(unquoted),
            "filters": {
                "cleanFit": "requests with exactly one delivery option (dealer cannot pick between "
                            "your own options); no-gig quoting",
                "multiOption": "rows of requests with 2-3 quoted options: excluded from the "
                               "acceptance fit because only the chosen option wins the request",
                "highRatioSplit": HIGH,
                "binWidth": BIN_W,
                "minNLane": MIN_N_LANE,
                "minNPlace": MIN_N_PLACE,
            },
            "method": "logistic MLE p(win)=sigmoid(b0+b1*ratio) by IRLS (ridge 1e-3), "
                      "threshold=-b0/b1 with delta-method standard error; grid MLE fallback",
            "generatedAt": blob.get("meta", {}).get("generatedAt"),
        },
        "global": dict(gl, empiricalHalfCrossing=half),
        "curve": {"clean": curve, "allQuoted": curve_all, "monotonicity": mono},
        "lanes": lanes,
        "origins": origins,
        "destinations": destinations,
        "lanesStrict": lanes_strict,
        "validation": val,
    }
    d = os.path.dirname(args.out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.out + ".tmp", "w") as f:
        json.dump(out, f, indent=1)
    os.replace(args.out + ".tmp", args.out)
    print(f"\nwrote {args.out}: {len(lanes)} lanes, {len(origins)} origins, "
          f"{len(destinations)} destinations, global threshold {gl['threshold']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())