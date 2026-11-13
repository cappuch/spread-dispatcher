"""Probe the dealer acceptance curve on level 8.

For each seed: create a level 8 practice world, quote every offered option of every
price request at listPrice * k where k is a deterministic pseudo-random number in
[0.75, 1.90] derived from sha256(seed/requestId/kind), never post a gig, play to the
end, reveal, and write one row per request option.  Every world is deleted.

Usage: .venv/bin/python probe_pricing.py [--seeds pp-1,pp-2,pp-3,pp-4] [--out data/pricing_probe.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback

import envload  # noqa: F401  (loads SPREAD_URL / SPREAD_KEY from .env)

from spread.client import Client, SpreadError, Unreachable

K_LO, K_HI = 0.75, 1.90
DAY = 1440


def ratio_for(seed: str, request_id: str, kind: str) -> float:
    """Deterministic pseudo-random quote/list ratio in [0.75, 1.90]."""
    h = hashlib.sha256(f"{seed}/{request_id}/{kind}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / 2.0**64
    return K_LO + u * (K_HI - K_LO)


def retry(fn, *a, tries=6, **kw):
    """Retry transient server trouble (restarts / rate limits)."""
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except Unreachable as e:  # server restart / no answer / 5xx
            last = e
            time.sleep(1.5 * (i + 1))
        except SpreadError as e:
            msg = str(e)
            if "429" in msg or "limit" in msg.lower():
                last = e
                time.sleep(1.0 * (i + 1))
                continue
            raise
    raise last  # type: ignore[misc]


def lane_of(req):
    """Directed (pickup, delivery) of each leg, plus the primary (first) lane."""
    lanes = [
        (leg["pickup"]["placeId"], leg["delivery"]["placeId"]) for leg in req["legs"]
    ]
    return lanes, lanes[0]


def play_world(client: Client, seed: str, road: dict, verbose=True) -> dict:
    w = client.create(level=8, seed=seed)
    wid = getattr(w, "id", None) or getattr(w, "world_id", None)
    rows: list = []
    events: list = []
    try:
        snap = retry(w.snapshot)
        ends_at = snap["endsAt"]
        now = snap["now"]
        actions: list = []
        steps = 0
        while now < ends_at:
            step = retry(w.advance, until=["price_request"], max=ends_at, actions=actions)
            steps += 1
            new_now = step["now"]
            evs = step.get("events") or []
            events.extend(evs)
            actions = []
            for ev in evs:
                if ev.get("kind") != "price_request":
                    continue
                req = ev["request"]
                prices = {}
                for opt in req["options"]:
                    k = ratio_for(seed, req["id"], opt["kind"])
                    prices[opt["kind"]] = max(1, int(round(opt["listPriceCents"] * k)))
                actions.append({"type": "quote", "requestId": req["id"], "prices": prices})
            if new_now <= now and not evs:
                break
            now = new_now
            if verbose and steps % 200 == 0:
                print(f"    {seed}: step {steps}, now={now}, events={len(events)}", flush=True)

        reveal = retry(w.reveal)

        # ---- index orders: events (order_won) joined to reveal truths by orderId ----
        truths = {o["orderId"]: o["legs"] for o in reveal.get("orders", [])}
        won_events = [e["order"] for e in events if e.get("kind") == "order_won"]

        # ---- one row per request option ----
        for item in reveal.get("requests", []):
            req = item["request"]
            quote = item.get("quote") or {}
            outcome = item["outcome"]
            chosen = item.get("option")
            lanes, (p, d) = lane_of(req)
            leg0 = req["legs"][0]
            km = road.get((p, d), (None, None))[0]
            minutes = road.get((p, d), (None, None))[1]

            order = None
            if outcome == "won" and chosen and chosen in quote:
                cands = [
                    o
                    for o in won_events
                    if o["dealerId"] == req["dealerId"]
                    and o["option"] == chosen
                    and o["priceCents"] == quote[chosen]
                    and o["deadline"] == next(
                        (x["deadline"] for x in req["options"] if x["kind"] == chosen), None
                    )
                ]
                if len(cands) == 1:
                    o = cands[0]
                    order = {
                        "orderId": o["id"],
                        "priceCents": o["priceCents"],
                        "deadline": o["deadline"],
                        "lastPickupAt": o["lastPickupAt"],
                        "status": o.get("status"),
                        "deliveredAt": o.get("deliveredAt"),
                        "legs": truths.get(o["id"]),
                    }

            for opt in req["options"]:
                kind = opt["kind"]
                q = quote.get(kind)
                rows.append(
                    {
                        "seed": seed,
                        "requestId": req["id"],
                        "dealerId": req["dealerId"],
                        "arrivedAt": req["arrivedAt"],
                        "arrivedDay": req["arrivedAt"] // DAY,
                        "respondBy": req["respondBy"],
                        "lane": [p, d],
                        "laneKey": f"{p}>{d}",
                        "lanes": lanes,
                        "legCount": len(req["legs"]),
                        "kind": kind,
                        "deadline": opt["deadline"],
                        "deadlineDay": opt["deadline"] // DAY,
                        "listPriceCents": opt["listPriceCents"],
                        "referenceFeeCents": req["referenceFeeCents"],
                        "quoteCents": q,
                        "ratio": (q / opt["listPriceCents"]) if q else None,
                        "optionCount": len(req["options"]),
                        "outcome": outcome,
                        "chosenOption": chosen,
                        "optionWon": bool(outcome == "won" and chosen == kind),
                        "laneKm": km,
                        "laneMinutes": minutes,
                        "privatePickup": bool(leg0["pickup"]["privateAddress"]),
                        "privateDelivery": bool(leg0["delivery"]["privateAddress"]),
                        "premium": bool(leg0["car"].get("premium")),
                        "electric": bool(leg0["car"].get("electric")),
                        "note": bool(req.get("note")),
                        "order": order if kind == chosen else None,
                    }
                )

        summary = {
            "seed": seed,
            "worldId": wid,
            "steps": steps,
            "endsAt": ends_at,
            "requests": len(reveal.get("requests", [])),
            "rows": len(rows),
            "wonRequests": sum(1 for i in reveal.get("requests", []) if i["outcome"] == "won"),
            "lostRequests": sum(1 for i in reveal.get("requests", []) if i["outcome"] == "lost"),
            "unquotedRequests": sum(
                1 for i in reveal.get("requests", []) if i["outcome"] == "unquoted"
            ),
            "score": retry(w.score),
        }
        return {"rows": rows, "summary": summary}
    finally:
        try:
            retry(w.delete)
            if verbose:
                print(f"  deleted {wid}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  !! could not delete {wid}: {e}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="pp-1,pp-2,pp-3,pp-4")
    ap.add_argument("--out", default="data/pricing_probe.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    verbose = not args.quiet

    client = Client()

    # road minutes / km per directed lane, for lane difficulty (map costs no token)
    world_map = client.map()
    road = {}
    for r in world_map["roads"]:
        if r["from"] != r["to"]:
            road[(r["from"], r["to"])] = (r["km"], r["minutes"])

    all_rows: list = []
    meta_seeds: list = []
    if os.path.exists(args.out):  # resume: keep seeds already played
        try:
            prev = json.load(open(args.out))
            done = set(prev.get("meta", {}).get("seedsDone") or [])
            if done:
                all_rows = [r for r in prev.get("rows", []) if r.get("seed") in done]
                meta_seeds = [
                    s for s in prev["meta"].get("perSeed", []) if s.get("seed") in done
                ]
                if verbose:
                    print(f"resuming: {sorted(done)} already have {len(all_rows)} rows", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"could not resume ({e}); starting fresh", file=sys.stderr, flush=True)
            all_rows, meta_seeds = [], []
    done_seeds = {s["seed"] for s in meta_seeds}
    t0 = time.time()
    for seed in seeds:
        if seed in done_seeds:
            continue
        if verbose:
            print(f"[{seed}] creating level 8 world", flush=True)
        try:
            got = play_world(client, seed, road, verbose=verbose)
        except Exception:  # noqa: BLE001
            print(f"[{seed}] FAILED\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        all_rows.extend(got["rows"])
        meta_seeds.append(got["summary"])
        if verbose:
            s = got["summary"]
            print(
                f"[{seed}] rows={s['rows']} won={s['wonRequests']} lost={s['lostRequests']} "
                f"unquoted={s['unquotedRequests']} steps={s['steps']}",
                flush=True,
            )
        # write a partial result as we go, so a crash keeps the finished seeds
        write_out(args.out, all_rows, meta_seeds, seeds, role="partial")

    write_out(args.out, all_rows, meta_seeds, seeds, role="complete")
    if verbose:
        print(f"wrote {args.out}: {len(all_rows)} rows in {time.time() - t0:.0f}s")
    return 0


def write_out(path: str, rows: list, per_seed: list, seeds: list, role: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    meta = {
        "level": 8,
        "seeds": seeds,
        "seedsDone": [s["seed"] for s in per_seed],
        "kRange": [K_LO, K_HI],
        "kFormula": "sha256(seed+'/'+requestId+'/'+kind) -> u in [0,1); k = 0.75 + u*1.15",
        "quotePolicy": "every offered option quoted; no gigs posted",
        "rows": len(rows),
        "requests": sum(s["requests"] for s in per_seed),
        "perSeed": per_seed,
        "role": role,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"rows": rows, "meta": meta}, f)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())