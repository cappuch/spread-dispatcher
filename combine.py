"""Greedy construction of multi-car gigs.

A *candidate* is one car that needs a driver::

    {"orderId": "R00001", "leg": 0,
     "pickup":   {"placeId": "porvoo",  "window": {"from": 493, "to": 2640}, "hours": {...}},
     "delivery": {"placeId": "hyvinkaa","window": {"from": 493, "to": 2640}, "hours": {...}},
     "serves": []}

`serves` is carried through untouched: it names extra ``(orderId, leg)`` pairs that ride along with the
candidate (a trade-in's old car, for example).  The route is planned from ``pickup``/``delivery``; a
candidate is returned unchanged, so the caller keeps whatever else it put in the dict (car flags,
`withPlates`, `withCharging`).

The rules, as far as this module models them
(https://spread.bahnexpress.com/kit/RULES.md, "Gigs"):

* a gig is an ordered list of stops, a pickup and a delivery per car, pickup before delivery;
* a gig with more than one car may plan at most 12 hours (720 minutes) of driving from its first
  pickup to its last delivery, counting the drive between every two stops;
* a driver can wait at a pickup (the car is not ready yet) and can leave later, but every stop must be
  reached inside its window (a delivery must arrive by its deadline);
* plates (30 min) and charging (40 min) cost time on the clock but do not count as driving.

The world already cuts each stop window to the site's opening hours, so the windows alone decide
feasibility here; ``hours`` is kept for callers that need it.

Saving model
------------
Solo, a driver drives a car's lane and brings the same distance back empty, which is what a separate
driver costs in time on the road: ``2 * road_minutes(pickup, delivery)``.  Chained, one driver does
the cars in order, so the empty returns become the short hops from one car's delivery to the next
car's pickup::

    drive   = sum(direct_i) + sum(connect_i)
    saving  = 2 * sum(direct_i) - drive = sum(direct_i) - sum(connect_i)

That sets each car's empty return against each hop between cars.  The trip home from the last
delivery, which every driver makes anyway, is not counted, so the figure is an estimate of the driver
minutes a chain takes out and it errs against over-grouping.  One car on its own shares nothing and
saves 0.

A gig is only formed when its saving is more than ``min_saving`` minutes (20 by default) and every
window can be met.  Ties go to the smaller drive, then to the better shared direction (the cosine
between each hop and the lane that precedes it, when the map has coordinates), then to the order ids,
so the result is deterministic.
"""
from __future__ import annotations

import itertools
import json
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HOUR = 60
MAX_DRIVE_MINUTES = 12 * HOUR
MAX_SPAN_MINUTES = 24 * HOUR  # a driver works a day: a chain may not wait across days
MIN_SAVING_MINUTES = 20
PLATES_MINUTES = 30
CHARGING_MINUTES = 40

DEFAULT_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "map.json")


class Roads:
    """Road minutes and kilometres between places, from ``GET /v1/map``."""

    def __init__(self, map_data: dict):
        self._minutes: Dict[Tuple[str, str], float] = {}
        self._km: Dict[Tuple[str, str], float] = {}
        self._coord: Dict[str, Tuple[float, float]] = {}
        for place in map_data.get("places", ()):
            if "lat" in place and "lon" in place:
                self._coord[place["id"]] = (float(place["lat"]), float(place["lon"]))
        for road in map_data.get("roads", ()):
            self._minutes[(road["from"], road["to"])] = road["minutes"]
            self._km[(road["from"], road["to"])] = road["km"]

    def minutes(self, a: str, b: str) -> float:
        try:
            return self._minutes[(a, b)]
        except KeyError:
            if a == b:
                return 0.0
            raise KeyError(f"no road from {a!r} to {b!r} in the map")

    def km(self, a: str, b: str) -> float:
        try:
            return self._km[(a, b)]
        except KeyError:
            if a == b:
                return 0.0
            raise KeyError(f"no road from {a!r} to {b!r} in the map")

    def coord(self, place: str) -> Optional[Tuple[float, float]]:
        return self._coord.get(place)

    def __len__(self) -> int:
        return len(self._minutes)


def road_minutes(map_or_roads, a: str, b: str) -> float:
    """Minutes by road from ``a`` to ``b``; accepts a map dict or a :class:`Roads`."""
    return _as_roads(map_or_roads).minutes(a, b)


def road_km(map_or_roads, a: str, b: str) -> float:
    """Kilometres by road from ``a`` to ``b``; accepts a map dict or a :class:`Roads`."""
    return _as_roads(map_or_roads).km(a, b)


def _as_roads(map_or_roads) -> Roads:
    """A :class:`Roads` from either a built one or the map dict itself."""
    if isinstance(map_or_roads, Roads):
        return map_or_roads
    return Roads(map_or_roads)


def load_map(path: str = DEFAULT_MAP) -> Roads:
    with open(path, encoding="utf-8") as handle:
        return Roads(json.load(handle))


_DEFAULT: Optional[Roads] = None


def default_roads() -> Roads:
    """The real map, read once from ``data/map.json``."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_map()
    return _DEFAULT


class Plan:
    """A feasible ordering of a chain of candidates and what it costs."""

    __slots__ = ("order", "drive", "saving", "shared", "stops", "finish")

    def __init__(self, order, drive, saving, shared, stops, finish):
        self.order = list(order)  # the candidates, in the order they are driven
        self.drive = drive  # road minutes, first pickup to last delivery
        self.saving = saving  # estimated minutes saved against driving the cars separately
        self.shared = shared  # mean cosine between each hop and the lane before it, 0 if unknown
        self.stops = stops  # [(orderId, leg, kind, placeId, arrive, window)]
        self.finish = finish  # when the last car is delivered on the earliest plan

    def __repr__(self) -> str:
        ids = [f"{c['orderId']}.{c['leg']}" for c in self.order]
        return (f"Plan({ids}, drive={self.drive:g}, saving={self.saving:g}, "
                f"shared={self.shared:.2f}, finish={self.finish:g})")

    def as_dict(self) -> dict:
        return {
            "stops": [{"orderId": oid, "leg": leg, "kind": kind, "placeId": place}
                      for oid, leg, kind, place, _arrive, _window in self.stops],
            "driveMinutes": self.drive,
            "savingMinutes": self.saving,
            "sharedDirection": round(self.shared, 3),
            "finish": self.finish,
        }


def _key(cand: dict) -> Tuple[str, int]:
    return (str(cand["orderId"]), int(cand.get("leg", 0)))


def _service(cand: dict, kind: str) -> float:
    """Time a stop takes on the clock but not on the road."""
    if kind != "pickup":
        return 0.0
    return (PLATES_MINUTES if cand.get("withPlates") else 0.0) + (
        CHARGING_MINUTES if cand.get("withCharging") else 0.0)


def _bearing(roads: Roads, a: str, b: str) -> Optional[float]:
    pa, pb = roads.coord(a), roads.coord(b)
    if pa is None or pb is None or pa == pb:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (pa[0], pa[1], pb[0], pb[1]))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(x, y)


def _shared(roads: Roads, order: Sequence[dict]) -> float:
    """Mean cosine between each connecting hop and the lane that ends where the hop starts."""
    cosines = []
    for first, second in zip(order, order[1:]):
        lane = _bearing(roads, first["pickup"]["placeId"], first["delivery"]["placeId"])
        hop = _bearing(roads, first["delivery"]["placeId"], second["pickup"]["placeId"])
        if lane is not None and hop is not None:
            cosines.append(math.cos(hop - lane))
    return sum(cosines) / len(cosines) if cosines else 0.0


def windows_overlap(chain: Sequence[dict]) -> bool:
    """True when the cars can all be collected in one stretch of time: the pickup windows share a
    minute.  Cars whose pickups cannot even happen together are not worth chaining, however well
    their towns line up."""
    lo = max(c["pickup"]["window"]["from"] for c in chain)
    hi = min(c["pickup"]["window"]["to"] for c in chain)
    return lo <= hi


def plan(roads: Roads, order: Sequence[dict], now: float,
         max_drive_minutes: float = MAX_DRIVE_MINUTES,
         max_span_minutes: float = MAX_SPAN_MINUTES) -> Optional[Plan]:
    """The earliest feasible run of ``order`` as given, or None when a stop misses its window, the
    driving is over ``max_drive_minutes``, or the run from the first pickup to the last delivery is
    longer than ``max_span_minutes`` (a driver works a day, so a chain may not wait across days).

    A wait at a pickup is allowed (the car is not ready yet) and so is leaving later, so starting at
    the earliest minute and moving forward is enough to decide feasibility.
    """
    if not order:
        return None
    chain = len(order) > 1  # the 12-hour limit applies to a gig with more than one car only
    start = max(now, order[0]["pickup"]["window"]["from"])
    t = start
    prev = order[0]["pickup"]["placeId"]
    drive = 0.0
    stops = []
    for cand in order:
        for kind in ("pickup", "delivery"):
            stop = cand[kind]
            place = stop["placeId"]
            window = stop["window"]
            if stops:  # the first stop is where the driver starts: no drive to it
                hop = roads.minutes(prev, place)
                drive += hop
                t += hop
            if t < window["from"]:
                t = window["from"]  # wait: the car is not ready, or the hour is booked
            if t > window["to"] or (chain and t - start > max_span_minutes):
                return None
            stops.append((cand["orderId"], int(cand.get("leg", 0)), kind, place, t, window))
            t += _service(cand, kind)
            prev = place
    if chain and drive > max_drive_minutes:
        return None
    direct = sum(roads.minutes(c["pickup"]["placeId"], c["delivery"]["placeId"]) for c in order)
    saving = 2 * direct - drive if chain else 0.0  # one car shares nothing
    return Plan(order, drive, saving, _shared(roads, order), stops, t)


def _direct(roads: Roads, cand: dict) -> float:
    return roads.minutes(cand["pickup"]["placeId"], cand["delivery"]["placeId"])


def _rank(p: Plan) -> Tuple[float, float, float, List[Tuple[str, int]]]:
    """Sort key: more saving, then less driving, then better shared direction, then ids."""
    return (-p.saving, p.drive, -p.shared, [_key(c) for c in p.order])


def group(candidates: Sequence[dict], now: float, max_cars: int = 3,
          max_drive_minutes: float = MAX_DRIVE_MINUTES,
          min_saving: float = MIN_SAVING_MINUTES,
          max_span_minutes: float = MAX_SPAN_MINUTES,
          roads: Optional[Roads] = None) -> List[List[dict]]:
    """Groups candidates into gigs, greedily, and returns them with the leftovers as singletons.

    Pairs come first: every pair is scored (both orders tried) and the pairs with the most saving are
    taken while both candidates are free.  The groups then grow to ``max_cars`` by the single
    extension that raises the saving most, trying every order of the longer chain.  What remains is
    returned as one-candidate groups.

    A group is only formed when its saving beats ``min_saving`` minutes, its pickup windows overlap
    (:func:`windows_overlap`), every stop can be reached inside its window, the planned driving is at
    most ``max_drive_minutes`` and the run from first pickup to last delivery is at most
    ``max_span_minutes``.

    ``now`` is the earliest minute a driver may start.  The result is deterministic: the input is
    sorted by ``(orderId, leg)`` before anything happens, and ties are broken by ids.
    """
    roads = _as_roads(roads) if roads is not None else default_roads()
    if max_cars < 1:
        raise ValueError("max_cars must be at least 1")
    items = sorted(candidates, key=_key)

    memo: Dict[Tuple[int, ...], Optional[Plan]] = {}

    def planned(idxs: Sequence[int]) -> Optional[Plan]:
        key = tuple(idxs)
        if key not in memo:
            chain = [items[i] for i in key]
            memo[key] = (plan(roads, chain, now, max_drive_minutes, max_span_minutes)
                         if len(chain) < 2 or windows_overlap(chain) else None)
        return memo[key]

    # Pairs, best saving first.  A pair can never be part of a feasible chain when no order of the
    # two is feasible, so dropping the bad pairs early loses nothing.
    pairs: List[Tuple[Tuple, Tuple[int, ...]]] = []
    if max_cars >= 2:
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                # Connect >= 0, so the saving cannot exceed the two lanes' own minutes.
                if _direct(roads, items[i]) + _direct(roads, items[j]) <= min_saving:
                    continue
                best, best_idxs = None, None
                for idxs in ((i, j), (j, i)):
                    p = planned(idxs)
                    if p is None or p.saving <= min_saving:
                        continue
                    if best is None or _rank(p) < _rank(best):
                        best, best_idxs = p, idxs
                if best is not None:
                    pairs.append((_rank(best), best_idxs))
    pairs.sort()

    taken = [False] * len(items)
    groups: List[List[int]] = []
    for _rank_key, idxs in pairs:
        if any(taken[i] for i in idxs):
            continue
        for i in idxs:
            taken[i] = True
        groups.append(list(idxs))

    # Grow to max_cars: apply the single best improvement anywhere until nothing improves.
    if max_cars >= 3:
        while True:
            best: Optional[Tuple[Tuple, int, Tuple[int, ...]]] = None
            for g, idxs in enumerate(groups):
                if len(idxs) >= max_cars:
                    continue
                base = planned(idxs)
                if base is None:  # cannot happen: groups hold feasible plans
                    continue
                for k in range(len(items)):
                    if taken[k]:
                        continue
                    for perm in itertools.permutations(tuple(idxs) + (k,)):
                        p = planned(perm)
                        if p is None or p.saving <= base.saving:
                            continue
                        key = (_rank(p), g, perm)
                        if best is None or (key[0], key[2]) < (best[0], best[2]):
                            best = key
            if best is None:
                break
            _rank_key, g, idxs = best
            taken[[i for i in idxs if i not in groups[g]][0]] = True
            groups[g] = list(idxs)

    result = [[items[i] for i in idxs] for idxs in groups]
    result += [[items[i]] for i in range(len(items)) if not taken[i]]
    result.sort(key=lambda g: [_key(c) for c in g])
    return result


def group_plan(gig: Sequence[dict], now: float,
               max_drive_minutes: float = MAX_DRIVE_MINUTES,
               max_span_minutes: float = MAX_SPAN_MINUTES,
               roads: Optional[Roads] = None) -> Optional[Plan]:
    """The plan of a group exactly as given (no reordering): drive, saving and the stop timeline."""
    return plan(_as_roads(roads) if roads is not None else default_roads(), gig, now,
                max_drive_minutes, max_span_minutes)


def best_plan(gig: Sequence[dict], now: float,
              max_drive_minutes: float = MAX_DRIVE_MINUTES,
              max_span_minutes: float = MAX_SPAN_MINUTES,
              roads: Optional[Roads] = None) -> Optional[Plan]:
    """The best feasible order of ``gig``, or None when no order fits the windows and limits."""
    roads = _as_roads(roads) if roads is not None else default_roads()
    best = None
    for order in itertools.permutations(gig):
        p = plan(roads, order, now, max_drive_minutes, max_span_minutes)
        if p is not None and (best is None or _rank(p) < _rank(best)):
            best = p
    return best


def gig_stops(gig: Sequence[dict], plan_: Optional[Plan] = None,
              with_windows: bool = False) -> List[dict]:
    """The kit's stop list for a gig, in the planned order: a pickup and a delivery per car.

    With ``with_windows`` each stop carries the candidate's own window; without, the world falls back
    to the order's window, which is the same thing.
    """
    order = plan_.order if plan_ is not None else list(gig)
    stops: List[dict] = []
    for cand in order:
        for kind in ("pickup", "delivery"):
            stop = {"orderId": cand["orderId"], "leg": int(cand.get("leg", 0)), "kind": kind}
            if cand.get("withPlates") and kind == "pickup":
                stop["withPlates"] = True
            if cand.get("withCharging") and kind == "pickup":
                stop["withCharging"] = True
            if with_windows:
                stop["window"] = dict(cand[kind]["window"])
            stops.append(stop)
    return stops


def candidates_from_requests(requests: Iterable[dict]) -> List[dict]:
    """Every leg of every request as a candidate: the pickup and delivery windows of the request.

    The delivery window ends at the earliest deadline the request offers, the tightest promise it
    carries, so a group planned on it is feasible whichever option the dealer picks.
    """
    out = []
    for r in requests:
        for leg_index, leg in enumerate(r["legs"]):
            pickup, delivery = leg["pickup"], leg["delivery"]
            options = r.get("options") or [{"deadline": delivery["window"]["to"]}]
            deadline = min(o["deadline"] for o in options)
            out.append({
                "orderId": r["id"],
                "leg": leg_index,
                "pickup": {"placeId": pickup["placeId"],
                           "window": dict(pickup["window"]),
                           "hours": pickup.get("hours")},
                "delivery": {"placeId": delivery["placeId"],
                             "window": {"from": delivery["window"]["from"],
                                        "to": min(delivery["window"]["to"], deadline)},
                             "hours": delivery.get("hours")},
                "serves": [(r["id"], leg_index)],
            })
    return out