#!/usr/bin/env python3
"""Probe what level-8 drivers actually accept, as a function of the posted fee.

For each seed we create a level-8 world, quote the FIRST offered option at its
list price on every price request (level 8's competitor is 26-54% above list, so
we win most cars), and post every won car as its own gig with one fixed,
purely fee-driven schedule:

    opening  0.50 x reference
    +0.05 x reference every 15 minutes, 31 steps, capped at 2.0 x reference.

No repricing, no direct offers, no combining cars.  A gig that expires is
reposted with the same stops (no stop windows) and the SAME schedule, so the car
stays on the board.  Every event is logged, together with the per-order facts we
need to normalise the booked fee (lane, road minutes, reference share, lead time,
option, deadline, pickup/delivery times).

Output: data/fee_probe.json  (map places, roster, per-run events/orders/gigs).
"""
from __future__ import annotations

import envload  # noqa: F401  (loads SPREAD_URL / SPREAD_KEY)
import json
import os
import sys
import time
from collections import Counter

from spread.client import Client, SpreadError
from spread.starter import Retry, stops_for, undelivered, without_windows

SEEDS = ["fp-1", "fp-2"]
LEVEL = 8
END_FALLBACK = 14280
STEP_MIN = 15
N_STEPS = 31
OPEN_MULT = 0.50
RISE_MULT = 0.05
CAP_MULT = 2.0
OUT = os.path.join("data", "fee_probe.json")

WAKE = ["price_request", "order_won", "gig_expired"]


def fee_schedule(now: int, base_cents: float) -> list[dict]:
    """Opening 0.5*base, +0.05*base per 15 min, 31 steps, capped at 2.0*base.

    Integer cents, nondecreasing (the world refuses a fall)."""
    cap = int(CAP_MULT * base_cents + 0.5)
    steps: list[dict] = []
    prev = 0
    for k in range(N_STEPS):
        fee = int(base_cents * (OPEN_MULT + RISE_MULT * k) + 0.5)
        fee = max(1, min(fee, cap), prev)
        steps.append({"at": now + STEP_MIN * k, "feeCents": fee})
        prev = fee
    return steps


def step_index(schedule: list[dict], t: int) -> int:
    """Which schedule step is visible at minute t."""
    k = 0
    for i, s in enumerate(schedule):
        if s["at"] <= t:
            k = i
        else:
            break
    return k


class Probe:
    def __init__(self, client: Client, world, seed: str):
        self.c = client
        self.w = world
        self.seed = seed
        self.now = world.snapshot()["now"]
        self.requests: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}          # order id -> latest order object
        self.won: dict[str, dict] = {}             # order id -> our bookkeeping
        self.gigs: dict[str, dict] = {}            # gig id -> our record
        self.cars: dict[str, dict] = {}            # "orderId:leg" -> lifecycle
        self.events: list[dict] = []
        self.posts_log: list[dict] = []            # accepted / refused posts
        self.outbox: list[tuple[dict, dict | None]] = []
        world_map = client.map()
        self.places = {p["id"]: p["name"] for p in world_map["places"]}
        self.road = {(r["from"], r["to"]): r["minutes"] for r in world_map["roads"]}
        self.roster = world.roster()
        self.density = Counter(d["homePlaceId"] for d in self.roster)

    # ---------------------------------------------------------------- actions
    def handle(self, e: dict) -> None:
        self.events.append(e)
        k = e["kind"]
        if k == "price_request":
            self.on_request(e["request"])
        elif k == "order_won":
            self.on_won(e["order"], e["at"])
        elif k == "order_cancelled":
            book = self.won.get(e["orderId"])
            if book:
                book["cancelled"] = True
            if e["orderId"] in self.orders:
                self.orders[e["orderId"]]["status"] = "cancelled"
        elif k == "order_changed":
            o = e["order"]
            if o["id"] in self.orders:
                self.orders[o["id"]] = o
                book = self.won.get(o["id"])
                if book:
                    book["deadline"] = o["deadline"]
                    book["lastPickupAt"] = o["lastPickupAt"]
                    book.setdefault("changes", []).append(e["at"])
        elif k == "gig_booked":
            self.on_booked(e)
        elif k in ("gig_dropped", "no_show"):
            g = self.gigs.get(e["gigId"])
            if g:
                g["dropped"] = g.get("dropped", 0) + 1
        elif k == "gig_expired":
            self.on_expired(e)
        elif k == "picked_up":
            g = self.gigs.get(e["gigId"])
            if g:
                g["pickupAt"] = e["at"]
            car = self.cars.get(f"{e['orderId']}:{e['leg']}")
            if car:
                car["pickupAt"] = e["at"]
        elif k == "delivered":
            g = self.gigs.get(e["gigId"])
            if g:
                g["deliveredAt"] = e["at"]
            car = self.cars.get(f"{e['orderId']}:{e['leg']}")
            if car:
                car["deliveredAt"] = e["at"]

    def on_request(self, r: dict) -> None:
        self.requests[r["id"]] = r
        first = r["options"][0]
        self.outbox.append((
            {"type": "quote", "requestId": r["id"],
             "prices": {first["kind"]: first["listPriceCents"]}},
            None,
        ))

    def on_won(self, order: dict, at: int) -> None:
        oid = order["id"]
        self.orders[oid] = order
        req = self.requests[oid]
        first = req["options"][0]
        quoted = None
        for o in req["options"]:
            if o["kind"] == order["option"]:
                quoted = o["listPriceCents"]
        book = {
            "orderId": oid,
            "dealerId": order["dealerId"],
            "option": order["option"],
            "priceCents": order["priceCents"],
            "deadline": order["deadline"],
            "lastPickupAt": order["lastPickupAt"],
            "referenceFeeCents": req["referenceFeeCents"],
            "quotedOption": first["kind"],
            "quotedListPriceCents": first["listPriceCents"],
            "wonOptionListPriceCents": quoted if quoted is not None else first["listPriceCents"],
            "note": (req.get("note") or {}).get("text"),
            "arrivedAt": req["arrivedAt"],
            "respondBy": req["respondBy"],
            "wonAt": at,
            "cancelled": False,
            "legs": [],
        }
        for leg in range(len(order["legs"])):
            l = order["legs"][leg]
            base = req["referenceFeeCents"] / len(order["legs"])
            lane_from = l["pickup"]["placeId"]
            lane_to = l["delivery"]["placeId"]
            book["legs"].append({
                "leg": leg,
                "laneFrom": lane_from,
                "laneTo": lane_to,
                "laneFromName": self.places.get(lane_from, lane_from),
                "laneToName": self.places.get(lane_to, lane_to),
                "roadMinutes": self.road.get((lane_from, lane_to)),
                "privatePickup": l["pickup"].get("privateAddress", False),
                "privateDelivery": l["delivery"].get("privateAddress", False),
                "premium": l["car"].get("premium", False),
                "electric": l["car"].get("electric", False),
                "readyAt": l["pickup"]["window"]["from"],
                "refShareCents": base,
                "deliveredAt": None,
                "pickupAt": None,
            })
            self.cars[f"{oid}:{leg}"] = {
                "orderId": oid, "leg": leg, "seed": self.seed,
                "laneFrom": lane_from, "laneTo": lane_to,
                "roadMinutes": self.road.get((lane_from, lane_to)),
                "refShareCents": base,
                "option": order["option"],
                "deadline": order["deadline"],
                "firstPostAt": None, "posts": [], "bookedFee": None,
                "bookedAt": None, "bookedGig": None, "everBooked": False,
                "pickupAt": None, "deliveredAt": None,
            }
        self.won[oid] = book
        for leg in undelivered(order):
            self.post(order, leg, None)

    def post(self, order: dict, leg: int, prev_gig: dict | None) -> None:
        oid = order["id"]
        req = self.requests[oid]
        base = req["referenceFeeCents"] / len(order["legs"])
        if prev_gig is None:
            stops = without_windows(stops_for(order, [leg], req.get("note"), Retry()), self.now)
            audience = {"experienced": bool(order["legs"][leg]["car"].get("premium")),
                        "manualLicence": False}
            schedule = fee_schedule(self.now, base)
        else:
            # Repost after expiry: same stops without any stop window, same schedule.
            stops = without_windows(prev_gig["action"]["stops"], float("inf"))
            audience = prev_gig["action"]["audience"]
            schedule = prev_gig["action"]["schedule"]
        action = {"type": "post", "stops": stops, "audience": audience, "schedule": schedule}
        meta = {"orderId": oid, "leg": leg, "base": base, "schedule": schedule,
                "repostOf": prev_gig["gigId"] if prev_gig else None}
        self.outbox.append((action, meta))

    def on_expired(self, e: dict) -> None:
        g = self.gigs.get(e["gigId"])
        if not g:
            return
        g["expiredAt"] = e["at"]
        car = self.cars.get(f"{g['orderId']}:{g['leg']}")
        if car is None or car["bookedGig"] is not None:
            return
        if car["deliveredAt"] is not None:
            return
        book = self.won.get(g["orderId"])
        if book is None or book["cancelled"]:
            return
        self.post(self.orders[g["orderId"]], g["leg"], g)

    def on_booked(self, e: dict) -> None:
        g = self.gigs.get(e["gigId"])
        if not g:
            return
        g["bookedFee"] = e["feeCents"]
        g["bookedAt"] = e["at"]
        g["driverId"] = e["driverId"]
        g["bookingStep"] = step_index(g["schedule"], e["at"])
        g["bookingStepFee"] = g["schedule"][g["bookingStep"]]["feeCents"]
        car = self.cars.get(f"{g['orderId']}:{g['leg']}")
        if car:
            car["bookedGig"] = g["gigId"]
            car["bookedFee"] = e["feeCents"]
            car["bookedAt"] = e["at"]
            car["everBooked"] = True

    # ---------------------------------------------------------------- results
    def read_results(self, actions: list[dict], metas: list[dict | None],
                     results: list[dict], t0: int) -> None:
        for action, meta, res in zip(actions, metas, results):
            if action["type"] != "post":
                continue
            ok = bool(res.get("ok"))
            rec = {"at": t0, "ok": ok, "orderId": meta["orderId"], "leg": meta["leg"],
                   "repostOf": meta["repostOf"]}
            if not ok:
                rec["reason"] = res.get("reason")
                self.posts_log.append(rec)
                continue
            gid = res["id"]
            rec["gigId"] = gid
            self.posts_log.append(rec)
            car = self.cars[f"{meta['orderId']}:{meta['leg']}"]
            if car["firstPostAt"] is None:
                car["firstPostAt"] = t0
            car["posts"].append(gid)
            self.gigs[gid] = {
                "gigId": gid,
                "orderId": meta["orderId"],
                "leg": meta["leg"],
                "seed": self.seed,
                "laneFrom": car["laneFrom"],
                "laneTo": car["laneTo"],
                "roadMinutes": car["roadMinutes"],
                "refShareCents": car["refShareCents"],
                "option": car["option"],
                "postedAt": t0,
                "schedule": meta["schedule"],
                "firstFeeCents": meta["schedule"][0]["feeCents"],
                "repostOf": meta["repostOf"],
                "action": {"stops": action["stops"], "audience": action["audience"],
                           "schedule": meta["schedule"]},
                "bookedFee": None, "bookedAt": None,
                "pickupAt": None, "deliveredAt": None, "expiredAt": None,
                "dropped": 0,
            }

    # ------------------------------------------------------------------- play
    def play(self) -> None:
        ends = self.w.snapshot()["endsAt"]
        guard = 0
        while self.now < ends:
            guard += 1
            actions = [a for a, _ in self.outbox]
            metas = [m for _, m in self.outbox]
            self.outbox = []
            t0 = self.now
            step = self.w.advance(until=WAKE, max=ends, actions=actions)
            self.read_results(actions, metas, step.get("results", []), t0)
            self.now = step["now"]
            for e in step["events"]:
                self.handle(e)
            if guard > 20000:
                raise RuntimeError("advance guard tripped")

    # ------------------------------------------------------------------ output
    def dump(self) -> dict:
        orders = []
        for oid, book in self.won.items():
            o = dict(book)
            legs = []
            for leg in book["legs"]:
                l = dict(leg)
                car = self.cars.get(f"{oid}:{l['leg']}")
                if car:
                    l.update({
                        "posts": car["posts"],
                        "firstPostAt": car["firstPostAt"],
                        "bookedFeeCents": car["bookedFee"],
                        "bookedAt": car["bookedAt"],
                        "everBooked": car["everBooked"],
                        "firstGigId": car["posts"][0] if car["posts"] else None,
                        "finalGigId": car["posts"][-1] if car["posts"] else None,
                    })
                l["deliveredAt"] = car["deliveredAt"] if car else None
                l["pickupAt"] = car["pickupAt"] if car else None
                if l["deliveredAt"] is not None:
                    l["latenessMin"] = l["deliveredAt"] - book["deadline"]
                    l["onTime"] = l["deliveredAt"] <= book["deadline"]
                else:
                    l["latenessMin"] = None
                    l["onTime"] = False
                legs.append(l)
            o["legs"] = legs
            orders.append(o)
        gigs = []
        for g in self.gigs.values():
            gg = {k: v for k, v in g.items() if k != "action"}
            gigs.append(gg)
        return {
            "seed": self.seed,
            "worldId": self.w.id,
            "level": LEVEL,
            "now": self.now,
            "endsAt": self.w.snapshot()["endsAt"],
            "score": self.w.score(),
            "events": self.events,
            "orders": orders,
            "gigs": gigs,
            "postsLog": self.posts_log,
            "density": dict(self.density),
            "roster": self.roster,
        }


def run_seed(client: Client, seed: str) -> dict:
    world = client.create(level=LEVEL, seed=seed)
    t0 = time.time()
    probe = Probe(client, world, seed)
    reveal = None
    try:
        probe.play()
        out = probe.dump()
        try:
            reveal = world.reveal()
            out["views"] = reveal.get("views", [])
        except SpreadError as exc:  # pragma: no cover
            out["revealError"] = str(exc)
    finally:
        try:
            world.delete()
        except SpreadError:
            pass
    out["seconds"] = round(time.time() - t0, 1)
    return out


def main() -> None:
    client = Client()
    os.makedirs("data", exist_ok=True)
    worlds = client.worlds()
    live = [w for w in worlds if not w.get("ended")]
    if len(live) >= 2:
        sys.exit(f"too many live worlds already: {[w['worldId'] for w in live]}")
    doc = {"seeds": SEEDS, "level": LEVEL,
           "scheduleSpec": {"openMult": OPEN_MULT, "riseMult": RISE_MULT,
                            "stepMinutes": STEP_MIN, "steps": N_STEPS,
                            "capMult": CAP_MULT},
           "runs": []}
    for seed in SEEDS:
        print(f"== seed {seed}", flush=True)
        run = run_seed(client, seed)
        print(f"   world {run['worldId']}: {len(run['orders'])} orders, "
              f"{len(run['gigs'])} gigs, score {run['score']}, {run['seconds']}s", flush=True)
        doc["runs"].append(run)
        with open(OUT, "w") as fh:
            json.dump(doc, fh)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()