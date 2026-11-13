"""Offline lane and driver-supply model for the Spread dispatcher game.

The model is deliberately data-driven and has no network calls:

    m = load_map()                     # data/map.json
    roster = load_roster()             # data/roster.json (an ended level-8 world)
    road(m, "espoo", "oulu")           # {'from', 'to', 'km', 'minutes'}
    drivers_near("tampere", 60)        # drivers whose home is <= 60 road min away
    lane_ease(m, roster, "espoo", "oulu")
    cluster_batches(requests)          # batch / yard-hour / trade-in clustering

The supply side uses the public map and the driver list.  A driver's home town
is all we know before the week starts; every distance is a road minute from
the map, never a straight line.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter, defaultdict
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(HERE, "data", "map.json")
DEFAULT_ROSTER = os.path.join(HERE, "data", "roster.json")
DEFAULT_REQUESTS = os.path.join(HERE, "study1.json")
DEFAULT_OUT = os.path.join(HERE, "data", "analysis.json")

# The 12-hour rule for a multi-car gig, in road minutes.
MAX_GIG_MINUTES = 12 * 60

# Cumulative road-minute rings used for the supply index.
RINGS = (30, 60, 120)

# Weights of the rings when collapsing them to one 0..1 density.  The 30/60
# rings describe the local labour pool; the 120 ring describes drivers who
# could reach a morning pickup by travelling the evening before.
RING_WEIGHTS = (0.5, 0.3, 0.2)
# The destination end gets a little more weight: a driver who ends near home
# is the cheap return, and a dead-end destination strands the car.
ORIGIN_EXP = 0.45
DEST_EXP = 0.55


# --------------------------------------------------------------------------
# Loading and road distances
# --------------------------------------------------------------------------


def _cached_json(path: str) -> Any:
    cache = _cached_json.__dict__.setdefault("_cache", {})
    real = os.path.abspath(path)
    if real not in cache:
        with open(real, "r", encoding="utf-8") as fh:
            cache[real] = json.load(fh)
    return cache[real]


def load_map(path: Optional[str] = None) -> Dict[str, Any]:
    """Load a map dict (`{'places': [...], 'roads': [...]}`)."""
    return _cached_json(path or DEFAULT_MAP)


def load_roster(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load a driver roster saved from `world.roster()`."""
    return _cached_json(path or DEFAULT_ROSTER)


def _as_map(m: Any) -> Dict[str, Any]:
    if m is None:
        return load_map()
    if isinstance(m, str):
        return load_map(m)
    return m


def _as_roster(r: Any) -> List[Dict[str, Any]]:
    if r is None:
        return load_roster()
    return r


def _road_index(m: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    cache = _road_index.__dict__.setdefault("_cache", {})
    key = id(m)
    if key not in cache:
        cache[key] = {(r["from"], r["to"]): r for r in m["roads"]}
    return cache[key]


def road(map: Any, a: str, b: str) -> Dict[str, Any]:
    """Return the directed road from place `a` to place `b`.

    The map carries every ordered pair, including `a == b`.  A plain dict is
    returned so callers can use `road(map, a, b)["minutes"]` or `["km"]`.
    Raises KeyError with both place ids when the pair is missing.
    """
    map = _as_map(map)
    try:
        return _road_index(map)[(a, b)]
    except KeyError:
        raise KeyError("no road %s -> %s in map" % (a, b)) from None


def road_minutes(m: Any, a: str, b: str) -> int:
    return int(road(m, a, b)["minutes"])


# --------------------------------------------------------------------------
# Driver density
# --------------------------------------------------------------------------


def _place_ids(m: Dict[str, Any]) -> List[str]:
    return [p["id"] for p in m["places"]]


def driver_counts(
    m: Any = None, roster: Any = None
) -> Dict[str, Dict[str, Any]]:
    """Per town: own drivers and drivers within 30/60/120 road minutes.

    Returns ``{town: {'own': int, 'within30': int, 'within60': int,
    'within120': int}}``.  The rings are cumulative and include the town
    itself (a home town is 0 minutes away).
    """
    m = _as_map(m)
    roster = _as_roster(roster)
    key = (id(m), id(roster))
    cache = driver_counts.__dict__.setdefault("_cache", {})
    if key in cache:
        return cache[key]

    home = Counter(d.get("homePlaceId") for d in roster)
    places = _place_ids(m)
    out: Dict[str, Dict[str, Any]] = {}
    for p in places:
        counts = {t: 0 for t in RINGS}
        for q in places:
            n = home.get(q, 0)
            if not n:
                continue
            minutes = road_minutes(m, p, q)
            for t in RINGS:
                if minutes <= t:
                    counts[t] += n
        out[p] = {
            "own": home.get(p, 0),
            "within30": counts[30],
            "within60": counts[60],
            "within120": counts[120],
            "nearest_driver_minutes": (
                0 if home.get(p)
                else min(
                    (road_minutes(m, p, q) for q, n in home.items() if n),
                    default=None,
                )
            ),
        }
    cache[key] = out
    return out


def drivers_near(
    town: str, minutes: int = 60, roster: Any = None, map: Any = None
) -> int:
    """Number of listed drivers whose home is within `minutes` road minutes."""
    counts = driver_counts(map, roster)
    if town not in counts:
        raise KeyError("unknown town %r" % (town,))
    # Any threshold works: use the exact ring only for the standard rings and
    # fall back to a direct scan for arbitrary values.
    if minutes in RINGS:
        return counts[town]["within%d" % minutes]
    m = _as_map(map)
    roster = _as_roster(roster)
    home = Counter(d.get("homePlaceId") for d in roster)
    total = 0
    for q, n in home.items():
        if road_minutes(m, town, q) <= minutes:
            total += n
    return total


def _caps(m: Dict[str, Any], roster: Any) -> Tuple[int, int, int]:
    """Largest ring count on the map, used to normalise the supply index."""
    counts = driver_counts(m, roster)
    return tuple(max(c["within%d" % t] for c in counts.values()) for t in RINGS)


def _supply_index(
    n30: int, n60: int, n120: int, caps: Tuple[int, int, int]
) -> float:
    """Collapse the three rings to a 0..1 density.

    Square roots temper the metro towns: a town with 200 drivers is not ten
    times as easy as a town with 20 for a single car.
    """
    vals = (n30, n60, n120)
    return sum(
        w * math.sqrt(min(1.0, v / cap)) if cap else 0.0
        for w, v, cap in zip(RING_WEIGHTS, vals, caps)
    )


# --------------------------------------------------------------------------
# Lane supply / ease
# --------------------------------------------------------------------------


def lane_supply(map: Any, roster: Any, a: str, b: str) -> Dict[str, Any]:
    """Full supply picture for the directed lane `a -> b`.

    `ease` is 0..100.  It multiplies the pickup-side and delivery-side driver
    densities (so one dead end cannot be hidden by a busy other end) and
    applies a mild penalty for a long drive.
    """
    map = _as_map(map)
    roster = _as_roster(roster)
    r = road(map, a, b)
    counts = driver_counts(map, roster)
    caps = _caps(map, roster)
    ca, cb = counts[a], counts[b]
    o = _supply_index(ca["within30"], ca["within60"], ca["within120"], caps)
    d = _supply_index(cb["within30"], cb["within60"], cb["within120"], caps)
    drive = 1.0 - min(1.0, float(r["minutes"]) / MAX_GIG_MINUTES)
    ease = 100.0 * (o ** ORIGIN_EXP) * (d ** DEST_EXP) * (0.75 + 0.25 * drive)
    return {
        "from": a,
        "to": b,
        "km": r["km"],
        "minutes": r["minutes"],
        "origin": dict(ca),
        "destination": dict(cb),
        "origin_index": round(o, 4),
        "destination_index": round(d, 4),
        "drive_index": round(drive, 4),
        "ease": round(ease, 2),
    }


def lane_ease(map: Any, roster: Any, a: str, b: str) -> float:
    """Estimated ease (0 hard .. 100 easy) of sourcing the directed lane."""
    return lane_supply(map, roster, a, b)["ease"]


def _median_or_none(vals: Sequence[float]) -> Optional[float]:
    return float(median(vals)) if vals else None


def lane_stats(
    m: Any, roster: Any, requests: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """One row per directed lane that appears in `requests`."""
    m = _as_map(m)
    roster = _as_roster(roster)
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for req in requests:
        ref = req.get("referenceFeeCents")
        option_list = [o["listPriceCents"] for o in req.get("options", [])]
        same_day = any(o.get("kind") == "same_day" for o in req.get("options", []))
        for leg in req.get("legs", []):
            a = leg["pickup"]["placeId"]
            b = leg["delivery"]["placeId"]
            row = buckets.setdefault(
                (a, b),
                {
                    "from": a,
                    "to": b,
                    "count": 0,
                    "trade_in_legs": 0,
                    "private_pickups": 0,
                    "private_deliveries": 0,
                    "premium": 0,
                    "electric": 0,
                    "_refs": [],
                    "_lists": [],
                    "_same_day": 0,
                },
            )
            row["count"] += 1
            row["trade_in_legs"] += int(len(req.get("legs", [])) > 1)
            row["private_pickups"] += int(bool(leg["pickup"].get("privateAddress")))
            row["private_deliveries"] += int(bool(leg["delivery"].get("privateAddress")))
            car = leg.get("car") or {}
            row["premium"] += int(bool(car.get("premium")))
            row["electric"] += int(bool(car.get("electric")))
            if ref:
                row["_refs"].append(ref)
            row["_lists"].extend(option_list)
            row["_same_day"] += int(same_day)

    rows: List[Dict[str, Any]] = []
    for row in buckets.values():
        supply = lane_supply(m, roster, row["from"], row["to"])
        refs = row.pop("_refs")
        lists = row.pop("_lists")
        row.update(
            {
                "km": supply["km"],
                "minutes": supply["minutes"],
                "origin": supply["origin"],
                "destination": supply["destination"],
                "origin_index": supply["origin_index"],
                "destination_index": supply["destination_index"],
                "ease": supply["ease"],
                "reference_fee_median_cents": (
                    int(median(refs)) if refs else None
                ),
                "list_price_median_cents": (
                    int(median(lists)) if lists else None
                ),
                "same_day_share": round(row.pop("_same_day") / row["count"], 3),
            }
        )
        rows.append(row)

    # A lane with much more traffic one way than back strands drivers.
    forward = {(r["from"], r["to"]): r["count"] for r in rows}
    for row in rows:
        row["reverse_count"] = forward.get((row["to"], row["from"]), 0)
        row["round_trip_gap"] = row["count"] - row["reverse_count"]
        # A coarse label for the dispatcher; the score itself stays numeric.
        if row["ease"] < 25:
            row["dispatch_action"] = "avoid_or_quote_high"
        elif row["ease"] < 45:
            row["dispatch_action"] = "price_up"
        elif row["ease"] >= 70 and row["count"] >= 3:
            row["dispatch_action"] = "absorb_volume"
        else:
            row["dispatch_action"] = "neutral"
    rows.sort(key=lambda x: (-x["count"], x["ease"]))
    return rows


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    def ranks(vs: Sequence[float]) -> List[float]:
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 4) if den else None


# --------------------------------------------------------------------------
# Batch / request clustering
# --------------------------------------------------------------------------


def _leg_key(leg: Dict[str, Any]) -> str:
    return "%s>%s" % (leg["pickup"]["placeId"], leg["delivery"]["placeId"])


def _car_flags(req: Dict[str, Any]) -> Tuple[int, int]:
    premium = sum(1 for l in req["legs"] if (l.get("car") or {}).get("premium"))
    electric = sum(1 for l in req["legs"] if (l.get("car") or {}).get("electric"))
    return premium, electric


def _chain_stats(
    legs: Sequence[Dict[str, Any]], m: Dict[str, Any], max_minutes: int
) -> Dict[str, Any]:
    """Greedy packing of legs into <= 12 h multi-car gigs.

    A gig is an ordered list of cars.  Its driving time is the sum of the
    cars' own drives plus the drive from each delivery to the next pickup.
    The first pickup to the last delivery must be <= `max_minutes`.
    """
    n = len(legs)
    if n == 0:
        return {"gigs": 0, "chain_sizes": [], "max_chain": 0,
                "cars_in_multi": 0, "total_minutes": 0}
    drive = [road_minutes(m, l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in legs]
    after = [
        [
            road_minutes(m, legs[i]["delivery"]["placeId"], legs[j]["pickup"]["placeId"])
            if i != j else 0
            for j in range(n)
        ]
        for i in range(n)
    ]
    remaining = set(range(n))
    chains: List[List[int]] = []
    total_minutes = 0
    while remaining:
        seed = min(remaining, key=lambda i: (drive[i], after[i][i]))
        remaining.remove(seed)
        chain = [seed]
        route = drive[seed]
        last = seed
        while True:
            best = None
            for j in remaining:
                extra = after[last][j] + drive[j]
                if route + extra <= max_minutes:
                    cand = (after[last][j], drive[j], j)
                    if best is None or cand < best[0]:
                        best = (cand, j)
            if best is None:
                break
            j = best[1]
            route += after[last][j] + drive[j]
            chain.append(j)
            remaining.remove(j)
            last = j
        chains.append(chain)
        total_minutes += route
    sizes = [len(c) for c in chains]
    return {
        "gigs": len(chains),
        "chain_sizes": sorted(sizes, reverse=True),
        "max_chain": max(sizes),
        "cars_in_multi": sum(s for s in sizes if s > 1),
        "total_minutes": total_minutes,
    }


def _same_yard_chain(
    legs: Sequence[Dict[str, Any]], yard: str, m: Dict[str, Any], max_minutes: int
) -> Dict[str, Any]:
    """Best <=12 h chain for cars that all leave one yard.

    Each additional car costs a delivery -> yard return, so the cheapest
    destinations chain first.  The last car needs no return drive.
    """
    ds = []
    for l in legs:
        if l["pickup"]["placeId"] != yard:
            continue
        d = road_minutes(m, yard, l["delivery"]["placeId"])
        ret = road_minutes(m, l["delivery"]["placeId"], yard)
        ds.append((d, ret, l["delivery"]["placeId"], l.get("_req"), l.get("_leg")))
    ds.sort()
    chosen: List[Dict[str, Any]] = []
    used = 0
    for one_way, ret, dest, req_id, leg_no in ds:
        # Route cost with this car appended: 2*sum(one_way) - last_one_way
        prospective = 2 * (used + one_way) - one_way
        if prospective <= max_minutes:
            chosen.append({"delivery": dest, "one_way_minutes": one_way,
                           "return_minutes": ret, "request": req_id,
                           "leg": leg_no})
            used += one_way
    # Recompute exact cost for the selected cars (order by one_way ascending).
    exact = 2 * sum(c["one_way_minutes"] for c in chosen) - (
        chosen[-1]["one_way_minutes"] if chosen else 0
    )
    return {
        "yard": yard,
        "cars_at_yard": len(ds),
        "max_cars_in_one_gig": len(chosen),
        "route_minutes": exact,
        "destination_counts": dict(Counter(c["delivery"] for c in chosen)),
        "cars": chosen,
        "cars_not_in_chain": len(ds) - len(chosen),
    }


def cluster_batches(
    requests: Sequence[Dict[str, Any]],
    map: Any = None,
    max_drive_minutes: int = MAX_GIG_MINUTES,
    examples: int = 3,
) -> Dict[str, Any]:
    """Cluster requests into batches, yard-hour groups and multi-car options.

    Returns a dict with:
      ``batches``       per batchId: cars, dealers, yards, detours, packing
      ``same_yard_hour``groups of legs sharing a pickup place and hour
      ``totals``        trade-ins, private legs, premium/electric counts
      ``bulk``          the two bulk batches, in detail

    ``detour_matrix[i][j]`` is the road minutes from car i's delivery to car
    j's pickup: the empty drive that a chained gig pays to combine them.  The
    diagonal is 0.  ``greedy_packing`` is one feasible packing, not a proof of
    the minimum number of gigs.
    """
    m = _as_map(map)
    reqs = list(requests)

    batches: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for req in reqs:
        if req.get("batchId"):
            batches[req["batchId"]].append(req)

    out_batches: Dict[str, Any] = {}
    for bid, rs in batches.items():
        legs = [
            {**leg, "_req": r["id"], "_leg": k}
            for r in rs
            for k, leg in enumerate(r["legs"])
        ]
        n = len(legs)
        pickups = Counter(l["pickup"]["placeId"] for l in legs)
        deliveries = Counter(l["delivery"]["placeId"] for l in legs)
        premium = sum(1 for l in legs if (l.get("car") or {}).get("premium"))
        electric = sum(1 for l in legs if (l.get("car") or {}).get("electric"))
        priv_pick = sum(1 for l in legs if l["pickup"].get("privateAddress"))
        priv_del = sum(1 for l in legs if l["delivery"].get("privateAddress"))
        trade_ins = sum(1 for r in rs if len(r["legs"]) > 1)

        # Detour(i -> j) is the empty drive after delivering car i to the
        # pickup of car j.  It is the only extra road time of merging them.
        matrix = [[0] * n for _ in range(n)]
        detours: List[Tuple[int, str, str, str, str]] = []
        for i, li in enumerate(legs):
            bi = li["delivery"]["placeId"]
            for j, lj in enumerate(legs):
                if i == j:
                    continue
                d = road_minutes(m, bi, lj["pickup"]["placeId"])
                matrix[i][j] = d
                detours.append(
                    (d, li["_req"], lj["_req"], bi, lj["pickup"]["placeId"])
                )
        det_vals = [d[0] for d in detours]
        best_pairs = sorted(detours)[:examples]
        worst_pairs = sorted(detours, reverse=True)[:examples]

        yard, yard_n = pickups.most_common(1)[0]
        chain = _chain_stats(legs, m, max_drive_minutes)
        same_yard_chains = [
            _same_yard_chain(legs, y, m, max_drive_minutes)
            for y, c in pickups.most_common()
            if c >= 2
        ]
        same_yard = max(
            same_yard_chains, key=lambda c: c["max_cars_in_one_gig"],
            default=_same_yard_chain(legs, yard, m, max_drive_minutes),
        )

        arrived = [r["arrivedAt"] for r in rs]
        windows = [l["pickup"]["window"]["from"] for l in legs]
        out_batches[bid] = {
            "requests": len(rs),
            "cars": n,
            "dealers": dict(Counter(r["dealerId"] for r in rs)),
            "arrived_at": [min(arrived), max(arrived)],
            "window_from": [min(windows), max(windows)],
            "pickups": dict(pickups),
            "deliveries": dict(deliveries),
            "dominant_yard": yard,
            "dominant_yard_share": round(yard_n / n, 3),
            "trade_ins": trade_ins,
            "private_pickups": priv_pick,
            "private_deliveries": priv_del,
            "premium": premium,
            "electric": electric,
            "car_labels": [
                {
                    "index": i,
                    "request": l["_req"],
                    "leg": l["_leg"],
                    "pickup": l["pickup"]["placeId"],
                    "delivery": l["delivery"]["placeId"],
                }
                for i, l in enumerate(legs)
            ],
            "detour_matrix": matrix,
            "pairwise_detour_minutes": {
                "pairs": len(detours),
                "min": min(det_vals) if det_vals else None,
                "median": _median_or_none(det_vals),
                "p90": (
                    sorted(det_vals)[int(0.9 * (len(det_vals) - 1))]
                    if det_vals else None
                ),
                "max": max(det_vals) if det_vals else None,
            },
            "best_merge_pairs": [
                {"detour_minutes": d, "after_request": a, "next_request": b,
                 "delivery": c, "next_pickup": e}
                for d, a, b, c, e in best_pairs
            ],
            "worst_merge_pairs": [
                {"detour_minutes": d, "after_request": a, "next_request": b,
                 "delivery": c, "next_pickup": e}
                for d, a, b, c, e in worst_pairs
            ],
            "greedy_packing": chain,
            "same_yard_chain": same_yard,
            "same_yard_chains": same_yard_chains,
        }

    # Same yard + same hour groups, across all requests (batched or not).
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for req in reqs:
        for leg in req["legs"]:
            hour = int(leg["pickup"]["window"]["from"]) // 60
            groups[(leg["pickup"]["placeId"], hour)].append({"req": req, "leg": leg})
    yard_hour = []
    for (place, hour), items in groups.items():
        if len(items) < 2:
            continue
        refs = [it["req"].get("referenceFeeCents") for it in items if it["req"].get("referenceFeeCents")]
        yard_hour.append(
            {
                "place": place,
                "hour": hour,
                "minute": hour * 60,
                "legs": len(items),
                "requests": len({it["req"]["id"] for it in items}),
                "batches": sorted({it["req"].get("batchId") for it in items if it["req"].get("batchId")}),
                "private": sum(1 for it in items if it["leg"]["pickup"].get("privateAddress")),
                "premium": sum(1 for it in items if (it["leg"].get("car") or {}).get("premium")),
                "electric": sum(1 for it in items if (it["leg"].get("car") or {}).get("electric")),
                "reference_fee_median_cents": int(median(refs)) if refs else None,
                "request_ids": [it["req"]["id"] for it in items][:20],
            }
        )
    yard_hour.sort(key=lambda x: (-x["legs"], x["place"], x["hour"]))

    totals = {
        "requests": len(reqs),
        "cars": sum(len(r["legs"]) for r in reqs),
        "batched_requests": sum(1 for r in reqs if r.get("batchId")),
        "batches": len(batches),
        "trade_in_requests": sum(1 for r in reqs if len(r["legs"]) > 1),
        "private_pickup_legs": sum(
            1 for r in reqs for l in r["legs"] if l["pickup"].get("privateAddress")
        ),
        "private_delivery_legs": sum(
            1 for r in reqs for l in r["legs"] if l["delivery"].get("privateAddress")
        ),
        "premium_cars": sum(
            1 for r in reqs for l in r["legs"] if (l.get("car") or {}).get("premium")
        ),
        "electric_cars": sum(
            1 for r in reqs for l in r["legs"] if (l.get("car") or {}).get("electric")
        ),
        "same_yard_hour_groups": len(yard_hour),
        "same_yard_hour_legs": sum(g["legs"] for g in yard_hour),
    }

    bulk = {bid: out_batches[bid] for bid in ("B0128", "B0001") if bid in out_batches}
    return {
        "batches": out_batches,
        "bulk": bulk,
        "same_yard_hour": yard_hour,
        "totals": totals,
        "max_drive_minutes": max_drive_minutes,
    }


# --------------------------------------------------------------------------
# Whole analysis
# --------------------------------------------------------------------------


def analyze(
    requests: Sequence[Dict[str, Any]],
    m: Any = None,
    roster: Any = None,
) -> Dict[str, Any]:
    m = _as_map(m)
    roster = _as_roster(roster)
    lanes = lane_stats(m, roster, requests)
    for i, row in enumerate(lanes):
        row["frequency_rank"] = i + 1
    by_ease = sorted(lanes, key=lambda r: (r["ease"], -r["count"]))
    for i, row in enumerate(by_ease):
        row["ease_rank"] = i + 1

    counts = [r["count"] for r in lanes]
    eases = [r["ease"] for r in lanes]
    corr = _spearman(counts, eases)

    hardest = sorted(lanes, key=lambda r: (r["ease"], -r["count"]))[:20]
    easiest = sorted(lanes, key=lambda r: (-r["ease"], -r["count"]))[:20]

    # Lanes that are hard *and* frequent are the money losers: every extra
    # request on them needs a driver who is not there.
    pressure = sorted(
        (r for r in lanes if r["count"] >= 3 and r["ease"] < 60),
        key=lambda r: (r["count"] * (100 - r["ease"])),
        reverse=True,
    )
    for r in pressure:
        r["pressure"] = round(r["count"] * (100 - r["ease"]) / 100, 2)

    supply = driver_counts(m, roster)
    towns = [
        {"town": t, **c, "supply_index": round(
            _supply_index(c["within30"], c["within60"], c["within120"],
                          _caps(m, roster)), 4)}
        for t, c in supply.items()
    ]
    towns.sort(key=lambda x: (-x["within60"], -x["own"]))
    # A town is scarce for dispatch when few drivers can reach it, not when it
    # merely has few residents: a commuter town inside a metro ring is fine.
    scarce = sorted(
        (t for t in towns if t["within60"] <= 25),
        key=lambda x: (x["within60"], x["own"]),
    )
    # Remote: even the 120-minute ring is thin.  These are the towns where a
    # car can strand a driver, not commuter towns inside a metro ring.
    remote = sorted(
        (t for t in towns if t["within120"] <= 60),
        key=lambda x: (x["within120"], x["own"]),
    )
    thin_local = sorted(
        (t for t in towns if t["own"] <= 5 and t["within60"] >= 100),
        key=lambda x: (x["own"], -x["within60"]),
    )

    # Compare request frequency with supply in deciles of ease.
    total_legs = sum(counts) or 1
    profile = []
    for lo in range(0, 100, 10):
        members = [r for r in lanes if lo <= r["ease"] < lo + 10]
        legs = sum(r["count"] for r in members)
        profile.append(
            {
                "ease_min": lo,
                "ease_max": lo + 10,
                "lanes": len(members),
                "legs": legs,
                "leg_share": round(legs / total_legs, 4),
                "mean_requests_per_lane": (
                    round(legs / len(members), 2) if members else 0
                ),
            }
        )

    clusters = cluster_batches(requests, m)
    totals = clusters["totals"]
    hard_legs = sum(r["count"] for r in lanes if r["ease"] < 40)
    very_hard_legs = sum(r["count"] for r in lanes if r["ease"] < 30)
    actions = Counter(r["dispatch_action"] for r in lanes)
    action_legs = Counter()
    for r in lanes:
        action_legs[r["dispatch_action"]] += r["count"]
    return {
        "source": {
            "requests": len(requests),
            "map_places": len(m["places"]),
            "map_roads": len(m["roads"]),
            "roster_drivers": len(roster),
            "roster_world": "wa2a02f0aa031b3a4d762",
            "roster_note": (
                "ended level-8 practice world; town shares are world-specific, "
                "so rerun with a fresh roster when the final world starts"
            ),
        },
        "towns": towns,
        "scarce_towns": scarce,
        "remote_towns": remote,
        "thin_local_towns": thin_local,
        "ease_frequency_profile": profile,
        "volume_on_hard_lanes": {
            "legs_ease_lt_30": very_hard_legs,
            "share_ease_lt_30": round(very_hard_legs / total_legs, 4),
            "legs_ease_lt_40": hard_legs,
            "share_ease_lt_40": round(hard_legs / total_legs, 4),
        },
        "dispatch_actions": {
            k: {"lanes": actions[k], "legs": action_legs[k]}
            for k in ("avoid_or_quote_high", "price_up", "absorb_volume", "neutral")
        },
        "lane_count": len(lanes),
        "lanes": lanes,
        "hardest_lanes": hardest,
        "easiest_lanes": easiest,
        "high_frequency_hard_lanes": [
            {**{k: v for k, v in r.items() if k not in ("origin", "destination")},
             "pressure": round(r["count"] * (100 - r["ease"]) / 100, 2)}
            for r in pressure[:20]
        ],
        "frequency_vs_ease_spearman": corr,
        "headline": {
            "drivers": len(roster),
            "towns_without_local_driver": [
                t["town"] for t in towns if t["own"] == 0
            ],
            "remote_towns": [t["town"] for t in remote],
            "hardest_lane": hardest[0] if hardest else None,
            "top_frequency_weighted_hard_lane": pressure[0] if pressure else None,
            "leg_share_ease_lt_30": round(very_hard_legs / total_legs, 4),
        },
        "clusters": clusters,
        "totals": totals,
    }


def _short(row: Dict[str, Any]) -> str:
    return (
        "%-11s -> %-13s n=%-3d (#%-3d) %4d min %6.1f km rev=%-2d ease %5.1f"
        % (
            row["from"], row["to"], row["count"], row.get("frequency_rank", 0),
            row["minutes"], row["km"], row.get("reverse_count", 0), row["ease"],
        )
    )


def report(a: Dict[str, Any]) -> str:
    lines: List[str] = []
    s = a["source"]
    lines.append(
        "LANE MODEL  %d requests / %d lanes / %d drivers / %d places"
        % (s["requests"], a["lane_count"], s["roster_drivers"], s["map_places"])
    )
    lines.append("")
    lines.append("DRIVERS BY TOWN (cumulative road minutes)")
    lines.append("%-13s %5s %6s %6s %6s %5s  %s" % (
        "town", "own", "30m", "60m", "120m", "near", "index"))
    for t in a["towns"]:
        lines.append(
            "%-13s %5d %6d %6d %6d %5s  %.2f"
            % (t["town"], t["own"], t["within30"], t["within60"], t["within120"],
               t["nearest_driver_minutes"], t["supply_index"])
        )
    lines.append("")
    lines.append("THIN MARKETS (drivers within 60 road minutes <= 25)")
    lines.append(", ".join("%s %d own/%d@60" % (t["town"], t["own"], t["within60"])
                           for t in a["scarce_towns"]))
    if a.get("remote_towns"):
        lines.append(
            "REMOTE (within 120 road minutes <= 60): "
            + ", ".join("%s %d@120" % (t["town"], t["within120"])
                        for t in a["remote_towns"])
        )
    if a.get("thin_local_towns"):
        lines.append(
            "COMMUTER TOWNS: few locals, wide reach: "
            + ", ".join("%s %d own/%d@60" % (t["town"], t["own"], t["within60"])
                        for t in a["thin_local_towns"])
        )
    lines.append("")
    lines.append("20 HARDEST LANES BY SUPPLY")
    for r in a["hardest_lanes"]:
        lines.append("  " + _short(r))
    lines.append("")
    lines.append("20 EASIEST LANES BY SUPPLY")
    for r in a["easiest_lanes"]:
        lines.append("  " + _short(r))
    lines.append("")
    lines.append(
        "FREQUENCY vs EASE: Spearman rho = %s (positive = volume leans to "
        "well-supplied lanes)" % a["frequency_vs_ease_spearman"]
    )
    lines.append("EASE DECILE   lanes   legs  share")
    for p in a["ease_frequency_profile"]:
        lines.append(
            "%3d-%3d      %5d %6d  %5.1f%%"
            % (p["ease_min"], p["ease_max"], p["lanes"], p["legs"],
               100 * p["leg_share"])
        )
    v = a["volume_on_hard_lanes"]
    lines.append(
        "Volume on hard lanes: %.1f%% of legs below ease 40, %.1f%% below 30"
        % (100 * v["share_ease_lt_40"], 100 * v["share_ease_lt_30"])
    )
    da = a["dispatch_actions"]
    lines.append(
        "Dispatch buckets: absorb %d lanes/%d legs, neutral %d/%d, "
        "price-up %d/%d, avoid-or-quote-high %d/%d"
        % (da["absorb_volume"]["lanes"], da["absorb_volume"]["legs"],
           da["neutral"]["lanes"], da["neutral"]["legs"],
           da["price_up"]["lanes"], da["price_up"]["legs"],
           da["avoid_or_quote_high"]["lanes"],
           da["avoid_or_quote_high"]["legs"])
    )
    lines.append("HIGH-FREQUENCY HARD LANES (price up or decline first)")
    for r in a["high_frequency_hard_lanes"][:15]:
        lines.append(
            "  %-11s -> %-13s n=%-3d rev=%-2d ease %5.1f pressure %6.1f"
            % (r["from"], r["to"], r["count"], r.get("reverse_count", 0),
               r["ease"], r["pressure"])
        )
    lines.append("")
    t = a["totals"]
    lines.append(
        "CLUSTERS  %d batches / %d batched requests / %d trade-ins / "
        "%d private pickups / %d private deliveries / %d premium / %d electric"
        % (t["batches"], t["batched_requests"], t["trade_in_requests"],
           t["private_pickup_legs"], t["private_delivery_legs"],
           t["premium_cars"], t["electric_cars"])
    )
    lines.append(
        "same-yard+hour groups: %d groups covering %d legs"
        % (t["same_yard_hour_groups"], t["same_yard_hour_legs"])
    )
    lines.append("largest same-yard+hour groups:")
    for g in a["clusters"]["same_yard_hour"][:6]:
        lines.append(
            "  %-11s day %d %02d:00  %2d cars, private %d, premium %d, "
            "electric %d"
            % (g["place"], g["minute"] // 1440 + 1, (g["minute"] % 1440) // 60,
               g["legs"], g["private"], g["premium"], g["electric"])
        )
    for bid in ("B0128", "B0001"):
        b = a["clusters"]["bulk"].get(bid)
        if not b:
            continue
        ch = b["same_yard_chain"]
        gp = b["greedy_packing"]
        lines.append(
            "  %s: %d requests/%d cars from %s (%.0f%%), detour median %s min "
            "(min %s); greedy %d gigs (vs %d single-car posts, %d cars in "
            "multi-car gigs), max chain %d; largest same-yard <12h chain = %d "
            "cars in %d min (%s), %d cars not in that chain"
            % (bid, b["requests"], b["cars"], b["dominant_yard"],
               100 * b["dominant_yard_share"],
               b["pairwise_detour_minutes"]["median"],
               b["pairwise_detour_minutes"]["min"], gp["gigs"], b["cars"],
               gp["cars_in_multi"], gp["max_chain"],
               ch["max_cars_in_one_gig"], ch["route_minutes"],
               ", ".join("%s x%d" % (d, n) for d, n in
                         ch["destination_counts"].items()),
               ch["cars_not_in_chain"])
        )
        if b.get("best_merge_pairs"):
            p = b["best_merge_pairs"][0]
            lines.append(
                "      cheapest merge: %s -> %s costs %d min extra (%s -> %s)"
                % (p["after_request"], p["next_request"], p["detour_minutes"],
                   p["delivery"], p["next_pickup"])
            )
    lines.append("")
    lines.append("ACTIONABLE GUIDANCE")
    lines.append(
        "1. Do not chase volume on hard lanes: the 20 hardest carry %d legs; "
        "quote them high or decline unless a return/chain gig exists."
        % sum(r["count"] for r in a["hardest_lanes"])
    )
    if a["high_frequency_hard_lanes"]:
        r = a["high_frequency_hard_lanes"][0]
        lines.append(
            "2. Worst frequency-weighted lane: %s->%s (n=%d, reverse %d, "
            "ease %.1f). Raise the quote premium there before adding drivers."
            % (r["from"], r["to"], r["count"], r.get("reverse_count", 0), r["ease"])
        )
    easiest = a["easiest_lanes"][:5]
    lines.append(
        "3. Easiest supply: %s. Under-price the competitor selectively here; "
        "these lanes can absorb extra wins."
        % ", ".join("%s->%s(%.0f)" % (r["from"], r["to"], r["ease"]) for r in easiest)
    )
    if a["remote_towns"]:
        lines.append(
            "4. Remote markets (few drivers even within 120 road minutes): %s. "
            "A pickup there needs a long deadhead fee or a chain from a "
            "nearby town."
            % ", ".join(t["town"] for t in a["remote_towns"])
        )
    lines.append(
        "5. Batch play: post the near-destination cars from the bulk yard as "
        "multi-car gigs (cheap return legs) and the far ones singly; a 12h "
        "chain cannot absorb a 6h one-way delivery."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Self-test / CLI
# --------------------------------------------------------------------------


def _selftest() -> None:
    m = load_map()
    roster = load_roster()
    assert len(m["places"]) == 30 and len(m["roads"]) == 900
    assert road(m, "helsinki", "espoo")["minutes"] == 32
    assert road(m, "helsinki", "helsinki")["minutes"] == 25
    assert road_minutes(m, "savonlinna", "helsinki") > 0

    # Towns and driver counts.
    assert drivers_near("kajaani", 30) == 6
    assert drivers_near("savonlinna", 120) == 30
    assert drivers_near("espoo", 30) >= 206
    assert drivers_near("helsinki", 30) == 197

    # Ease: dense metro lane must beat a dead-end lane.
    easy = lane_ease(m, roster, "helsinki", "espoo")
    hard = lane_ease(m, roster, "hyvinkaa", "savonlinna")
    assert 0 <= hard < easy <= 100, (easy, hard)
    assert easy > 60 and hard < 25, (easy, hard)
    ls = lane_supply(m, roster, "espoo", "oulu")
    assert ls["minutes"] > 300 and ls["destination"]["own"] < 20

    # Batch clustering on a tiny fixture with two cars at one yard.
    reqs = [
        {"id": "T1", "dealerId": "D1", "batchId": "BT", "arrivedAt": 600,
         "respondBy": 615, "referenceFeeCents": 5000, "note": None,
         "options": [{"kind": "next_day", "deadline": 2640,
                      "listPriceCents": 5000}],
         "legs": [{"car": {"premium": False, "electric": False,
                           "chargePercent": None},
                   "pickup": {"placeId": "espoo", "privateAddress": False,
                              "window": {"from": 600, "to": 2640}, "hours": None},
                   "delivery": {"placeId": "vantaa", "privateAddress": False,
                                "window": {"from": 600, "to": 2640}, "hours": None}}]},
        {"id": "T2", "dealerId": "D1", "batchId": "BT", "arrivedAt": 601,
         "respondBy": 616, "referenceFeeCents": 5000, "note": None,
         "options": [{"kind": "next_day", "deadline": 2640,
                      "listPriceCents": 5000}],
         "legs": [{"car": {"premium": True, "electric": True,
                           "chargePercent": None},
                   "pickup": {"placeId": "espoo", "privateAddress": False,
                              "window": {"from": 600, "to": 2640}, "hours": None},
                   "delivery": {"placeId": "helsinki", "privateAddress": False,
                                "window": {"from": 600, "to": 2640}, "hours": None}}]},
    ]
    c = cluster_batches(reqs, m)
    b = c["batches"]["BT"]
    assert b["cars"] == 2 and b["premium"] == 1 and b["electric"] == 1
    assert b["dominant_yard"] == "espoo"
    assert b["same_yard_chain"]["max_cars_in_one_gig"] == 2
    assert b["same_yard_chain"]["route_minutes"] == 82
    assert b["same_yard_chain"]["destination_counts"] == {"vantaa": 1, "helsinki": 1}
    assert b["detour_matrix"][0][1] == 25   # vantaa -> espoo pickup
    assert b["detour_matrix"][1][0] == 32   # helsinki -> espoo pickup
    assert b["greedy_packing"]["gigs"] == 1
    assert b["greedy_packing"]["cars_in_multi"] == 2
    assert c["same_yard_hour"][0]["legs"] == 2
    print("selftest: ok")

    # Full study, exercised without writing anything.
    study = _cached_json(DEFAULT_REQUESTS)
    a = analyze(study["requests"], m, roster)
    assert a["lane_count"] >= 300
    assert len(a["hardest_lanes"]) == 20 and len(a["easiest_lanes"]) == 20
    assert a["clusters"]["totals"]["batches"] >= 100
    assert a["clusters"]["bulk"]["B0128"]["cars"] == 38
    print("analysis: %d lanes, %d batches, B0128 max chain %d" % (
        a["lane_count"], a["clusters"]["totals"]["batches"],
        a["clusters"]["bulk"]["B0128"]["same_yard_chain"]["max_cars_in_one_gig"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline lane and supply analysis")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--requests", default=DEFAULT_REQUESTS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    study = _cached_json(args.requests)
    reqs = study["requests"] if isinstance(study, dict) else study
    a = analyze(reqs, load_map(), load_roster())
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(a, fh, ensure_ascii=False, indent=1)
    if not args.quiet:
        print(report(a))
        print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())