"""Sample: group 20 real request legs from study1.json on the real map.

    .venv/bin/python combine_sample.py

Prints each gig with its order, its route, the planned drive minutes and the estimated saving in
driver minutes, then the totals: how many drivers the batch needs, how much driving it costs, and how
much empty driving the grouping takes out.
"""
from __future__ import annotations

import json
import os
import sys

import combine

HERE = os.path.dirname(os.path.abspath(__file__))
LEGS = 20
NOW = 480  # Monday 08:00


def first_legs(path: str, count: int) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        requests = json.load(handle)["requests"]
    cars = []
    for request in requests:
        cars += combine.candidates_from_requests([request])
        if len(cars) >= count:
            break
    return cars[:count]


def main() -> int:
    roads = combine.load_map(os.path.join(HERE, "data", "map.json"))
    cars = first_legs(os.path.join(HERE, "study1.json"), LEGS)
    groups = combine.group(cars, NOW, roads=roads)

    drive = 0.0
    saving = 0.0
    print(f"{len(cars)} legs from study1.json, now={NOW}, max 3 cars and 720 drive minutes a gig\n")
    for number, gig in enumerate(groups, 1):
        plan = combine.best_plan(gig, NOW, roads=roads)
        if plan is None:
            print(f"gig {number}: {[c['orderId'] for c in gig]} - no feasible plan")
            continue
        drive += plan.drive
        saving += plan.saving
        route = " -> ".join(f"{place}{'*' if kind == 'delivery' else ''}"
                            for _oid, _leg, kind, place, _at, _win in plan.stops)
        names = ", ".join(f"{c['orderId']}.{c['leg']}" for c in gig)
        print(f"gig {number}: {len(gig)} car(s) {names}")
        print(f"        route  {route}   (* = delivery)")
        print(f"        drive {plan.drive:>5.0f} min   saving {plan.saving:>5.0f} min   "
              f"finish {plan.finish:.0f}   shared {plan.shared:+.2f}")
        stops = " ".join(f"{s['kind'][0].upper()}{s['orderId']}.{s['leg']}"
                         for s in combine.gig_stops(gig, plan))
        print(f"        stops  {stops}")
        print()

    print(f"gigs {len(groups)} for {len(cars)} cars (was {len(cars)} drivers)")
    print(f"driving {drive:.0f} min, estimated saving {saving:.0f} driver minutes")
    return 0


if __name__ == "__main__":
    sys.exit(main())