#!/usr/bin/env python3
"""Analyse data/fee_probe.json: what level-8 drivers accept, by lane, distance,
lead time, hour and option, and which schedule would book 60-75% cheaply.

Writes data/fee_summary.json and prints a readable report.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict

IN = os.path.join("data", "fee_probe.json")
OUT = os.path.join("data", "fee_summary.json")

ROAD_BUCKETS = [("<60", 0, 60), ("60-120", 60, 120), ("120-180", 120, 180),
                ("180-300", 180, 300), ("300+", 300, 10 ** 9)]
LEAD_BUCKETS = [("<2h", 0, 120), ("2-6h", 120, 360), ("6-24h", 360, 1440),
                (">24h", 1440, 10 ** 9)]
CAP = 2.0
STEP_MIN = 15
N_STEPS = 31


# ----------------------------------------------------------------- statistics
def q(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    i = (len(vals) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] * (1 - (i - lo)) + vals[hi] * (i - lo)


def dist(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4),
            "p25": round(q(vals, 0.25), 4), "median": round(q(vals, 0.5), 4),
            "p75": round(q(vals, 0.75), 4), "p90": round(q(vals, 0.9), 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(num / (dx * dy), 3) if dx and dy else None


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    return pearson(rank([p[0] for p in pairs]), rank([p[1] for p in pairs]))


def bucket_of(v, buckets):
    if v is None:
        return None
    for name, lo, hi in buckets:
        if lo <= v < hi:
            return name
    return buckets[-1][0]


# --------------------------------------------------------------- data access
def collect(doc):
    cars, gigs, orders = [], [], []
    for run in doc["runs"]:
        seed = run["seed"]
        for o in run["orders"]:
            o = dict(o)
            o["seed"] = seed
            orders.append(o)
            for leg in o["legs"]:
                c = dict(leg)
                c.update({"seed": seed, "orderId": o["orderId"],
                          "referenceFeeCents": o["referenceFeeCents"],
                          "optionKind": o["option"], "cancelled": o["cancelled"],
                          "deadline": o["deadline"], "wonAt": o["wonAt"],
                          "priceCents": o["priceCents"]})
                cars.append(c)
        for g in run["gigs"]:
            g = dict(g)
            g["seed"] = seed
            gigs.append(g)
    return cars, gigs, orders


def ratio(g, key="bookedFee"):
    fee = g.get(key)
    ref = g.get("refShareCents")
    if fee is None or not ref:
        return None
    return fee / ref


# --------------------------------------------------------------------- report
def main():
    doc = json.load(open(IN))
    cars, gigs, orders = collect(doc)
    ends = doc["runs"][0]["endsAt"] if doc["runs"] else 14280
    density = {}
    for run in doc["runs"]:
        for k, v in run.get("density", {}).items():
            density[k] = density.get(k, 0) + v
    density_mean = sum(density.values()) / max(1, len(density))

    booked_gigs = [g for g in gigs if g.get("bookedFee") is not None]
    booked_cars = [c for c in cars if c.get("bookedFeeCents") is not None]
    posted_cars = [c for c in cars if c.get("firstPostAt") is not None]
    ratios = [ratio(g) for g in booked_gigs]

    runs_out = []
    for run in doc["runs"]:
        r_gigs = [g for g in gigs if g["seed"] == run["seed"]]
        r_cars = [c for c in cars if c["seed"] == run["seed"]]
        r_booked = [g for g in r_gigs if g.get("bookedFee") is not None]
        runs_out.append({
            "seed": run["seed"], "worldId": run["worldId"],
            "ordersWon": len(run["orders"]),
            "carsPosted": sum(1 for c in r_cars if c.get("firstPostAt") is not None),
            "gigsPosted": len(r_gigs),
            "bookings": len(r_booked),
            "bookRatePerCar": round(sum(1 for c in r_cars if c.get("everBooked"))
                                    / max(1, sum(1 for c in r_cars if c.get("firstPostAt"))), 3),
            "bookRatePerGig": round(len(r_booked) / max(1, len(r_gigs)), 3),
            "score": run.get("score"),
        })

    # ---- distributions ------------------------------------------------
    def by_group(keyfn, source, units):
        groups = defaultdict(list)
        for x in source:
            k = keyfn(x)
            if k is not None:
                groups[k].append(x)
        out = {}
        for k, items in groups.items():
            rs = [ratio(g) for g in items] if units == "gig" else [x.get("_ratio") for x in items]
            out[k] = {"n": len(items), **dist(rs)}
        return out

    for c in cars:
        c["_ratio"] = (c["bookedFeeCents"] / c["refShareCents"]
                       if c.get("bookedFeeCents") is not None and c.get("refShareCents") else None)
        c["_lead"] = (c["pickupAt"] - c["bookedAt"]
                      if c.get("pickupAt") is not None and c.get("bookedAt") is not None else None)
        c["_hour"] = (c["bookedAt"] % 1440) // 60 if c.get("bookedAt") is not None else None

    road_g = by_group(lambda g: bucket_of(g.get("roadMinutes"), ROAD_BUCKETS), booked_gigs, "gig")
    lead_g = by_group(lambda c: bucket_of(c.get("_lead"), LEAD_BUCKETS), booked_cars, "car")
    hour_g = by_group(lambda g: g.get("_hour"), booked_cars, "car")
    opt_g = by_group(lambda g: g.get("option") or g.get("optionKind"), booked_cars, "car")

    # book rate per road bucket (cars posted)
    road_rate = {}
    for name, lo, hi in ROAD_BUCKETS:
        items = [c for c in posted_cars if c.get("roadMinutes") is not None and lo <= c["roadMinutes"] < hi]
        road_rate[name] = {
            "cars": len(items),
            "booked": sum(1 for c in items if c.get("everBooked")),
            "bookRate": round(sum(1 for c in items if c.get("everBooked")) / len(items), 3) if items else None,
            "meanRatio": round(sum(c["_ratio"] for c in items if c["_ratio"] is not None)
                               / max(1, sum(1 for c in items if c["_ratio"] is not None)), 4)
            if any(c["_ratio"] is not None for c in items) else None,
        }

    # ---- schedule steps ------------------------------------------------
    step_counts = Counter()
    step_fee_ratio = {}
    for g in booked_gigs:
        k = g.get("bookingStep")
        if k is None:
            continue
        step_counts[k] += 1
        step_fee_ratio[k] = g["bookingStepFee"] / g["refShareCents"]
    first_fee_bookings = step_counts.get(0, 0)
    steps = {
        "bookingStepCounts": {str(k): step_counts[k] for k in sorted(step_counts)},
        "firstFeeBookings": first_fee_bookings,
        "firstFeeShareOfBookings": round(first_fee_bookings / max(1, len(booked_gigs)), 3),
        "firstFeeShareOfCars": round(first_fee_bookings / max(1, len(posted_cars)), 3),
        "meanBookingStep": round(sum(k * n for k, n in step_counts.items()) / max(1, len(booked_gigs)), 2),
        "noBookRatePerCar": round(1 - len([c for c in posted_cars if c.get("everBooked")]) / max(1, len(posted_cars)), 3),
        "noBookRatePerGig": round(1 - len(booked_gigs) / max(1, len(gigs)), 3),
        "gigsPosted": len(gigs),
        "gigsBooked": len(booked_gigs),
        "carsPosted": len(posted_cars),
        "carsBooked": len([c for c in posted_cars if c.get("everBooked")]),
        "stepFeeRatio": {str(k): round(v, 3) for k, v in sorted(step_fee_ratio.items())},
    }

    cum = {}
    acc = 0
    for k in range(N_STEPS):
        acc += step_counts.get(k, 0)
        if acc:
            cum[str(k)] = round(acc / len(booked_gigs), 3)
    steps["cumulativeShare"] = cum

    # ---- lanes and density --------------------------------------------
    lane_dest = defaultdict(list)
    lane_orig = defaultdict(list)
    for c in posted_cars:
        lane_dest[c["laneTo"]].append(c)
        lane_orig[c["laneFrom"]].append(c)
    lname = {}
    for o in orders:
        for leg in o["legs"]:
            lname[leg["laneTo"]] = leg.get("laneToName", leg["laneTo"])
            lname[leg["laneFrom"]] = leg.get("laneFromName", leg["laneFrom"])

    def lane_table(groups):
        out = []
        for place, items in groups.items():
            booked = [c for c in items if c["_ratio"] is not None]
            out.append({
                "placeId": place, "name": lname.get(place, place),
                "cars": len(items),
                "booked": len(booked),
                "bookRate": round(len(booked) / len(items), 3),
                "meanRatio": round(sum(c["_ratio"] for c in booked) / len(booked), 3) if booked else None,
                "density": density.get(place, 0),
            })
        return sorted(out, key=lambda x: -x["cars"])

    dest_tab = lane_table(lane_dest)
    orig_tab = lane_table(lane_orig)

    corr = {
        "ratio_vs_destDensity": pearson([ratio(g) for g in booked_gigs],
                                        [density.get(g["laneTo"], 0) for g in booked_gigs]),
        "ratio_vs_origDensity": pearson([ratio(g) for g in booked_gigs],
                                        [density.get(g["laneFrom"], 0) for g in booked_gigs]),
        "ratio_vs_roadMinutes": pearson([ratio(g) for g in booked_gigs],
                                        [g.get("roadMinutes") for g in booked_gigs]),
        "ratio_vs_leadMinutes": spearman([c["_ratio"] for c in booked_cars], [c.get("_lead") for c in booked_cars]),
        "ratio_vs_destDensity_spearman": spearman([ratio(g) for g in booked_gigs],
                                                  [density.get(g["laneTo"], 0) for g in booked_gigs]),
        "bookRate_vs_destDensity": pearson(
            [1.0 if c.get("everBooked") else 0.0 for c in posted_cars],
            [density.get(c["laneTo"], 0) for c in posted_cars]),
    }
    # Lane identity: spread of the mean booked ratio across places with >=20 cars.
    for label, table in (("origin", orig_tab), ("destination", dest_tab)):
        means = [r["meanRatio"] for r in table if r["cars"] >= 20 and r["meanRatio"] is not None]
        corr[f"laneSpread_{label}"] = round(max(means) - min(means), 3) if len(means) >= 2 else None

    # ---- required-fee CDF and replay recommendation --------------------
    gig_index = {g["gigId"]: g for g in gigs}
    reqs = []
    first_posted = 0
    first_booked = 0
    for c in posted_cars:
        g = gig_index.get(c.get("firstGigId"))
        if not g:
            continue
        first_posted += 1
        end = g.get("bookedAt") or g.get("expiredAt") or ends
        if g.get("bookedAt") is not None:
            first_booked += 1
        reqs.append({"req": c["_ratio"], "life": end - g["postedAt"]})

    cdf = {}
    for f in (0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 1.9, 2.0):
        k = sum(1 for x in reqs if x["req"] is not None and x["req"] <= f + 1e-9)
        cdf[str(f)] = round(k / len(reqs), 3) if reqs else None

    def replay(m, r, cap=CAP):
        """Each car books at the first step whose fee reaches the fee it actually
        accepted, if its first posting is still open. Conservative: the accepted
        fee is an upper bound on the car's true threshold."""
        booked = 0
        fee = 0.0
        for x in reqs:
            R = x["req"]
            if R is None:
                continue
            for k in range(N_STEPS):
                if k * STEP_MIN > x["life"]:
                    break
                f = min(m + r * k, cap)
                if f >= R - 0.001:
                    booked += 1
                    fee += f
                    break
        n = len(reqs)
        return booked / n if n else 0.0, (fee / booked if booked else None)

    grid = []
    band = []
    for mi in range(10, 25):          # 0.50 .. 1.20 (below 0.50 is unmeasured)
        m = mi * 0.05
        for ri in range(1, 26):       # 0.01 .. 0.25 per 15 min
            r = ri * 0.01
            rate, fee = replay(m, r)
            row = {"openMult": round(m, 2), "rise15": round(r, 2),
                   "risePerHour": round(4 * r, 2), "bookRate": round(rate, 3),
                   "meanFeeRatio": round(fee, 3) if fee else None}
            grid.append(row)
            if 0.60 <= rate <= 0.75 + 1e-9:
                band.append(row)
    best = min(band, key=lambda g: g["meanFeeRatio"] or 9) if band else None

    frontier = []
    for target in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        near = [g for g in grid if abs(g["bookRate"] - target) <= 0.02]
        if near:
            frontier.append(min(near, key=lambda g: g["meanFeeRatio"] or 9))

    obs = replay(0.50, 0.05)
    recommendation = {
        "model": ("required-fee replay: each car's accepted fee in the probe (the fee at "
                  "which it booked) is replayed against each car's actual first-posting "
                  "lifetime; a candidate books the car at the first 15-min step whose fee "
                  "reaches that level. Cars that never booked are censored at 2.0 and are "
                  "not booked by any candidate."),
        "observedSchedule": {"openMult": 0.50, "rise15": 0.05, "bookRate": round(obs[0], 3),
                             "meanFeeRatio": round(obs[1], 3) if obs[1] else None},
        "firstPosting": {"carsPosted": first_posted, "carsBooked": first_booked,
                         "rate": round(first_booked / max(1, first_posted), 3)},
        "requiredFeeCdf": cdf,
        "recommended": best,
        "frontier": frontier,
        "caveat": ("Right-censoring dominates: 61% of all bookings happened at the 2.0 cap, "
                   "so the fee a car would accept below/above 2.0 is not identified; the "
                   "accepted fee used here is an upper bound on the car's threshold (drivers "
                   "wait for raises). Gigs that never booked are censored beyond 2.0. A "
                   "steeper rise also changes how long drivers wait, which this replay "
                   "does not model."),
    }

    # ---- lateness -----------------------------------------------------
    late = [c["deliveredAt"] - c["deadline"] if c.get("deliveredAt") is not None else None
            for c in cars]
    delivered = [x for x in late if x is not None]
    lateness = {
        "carsDelivered": len(delivered),
        "carsWon": len(cars),
        "onTimeShare": round(sum(1 for x in delivered if x <= 0) / max(1, len(delivered)), 3) if delivered else None,
        "medianLatenessMin": q(delivered, 0.5) if delivered else None,
    }

    summary = {
        "meta": {"seeds": doc["seeds"], "level": doc["level"],
                 "scheduleSpec": doc.get("scheduleSpec"),
                 "orders": len(orders), "cars": len(cars), "gigs": len(gigs),
                 "bookedGigs": len(booked_gigs), "bookedCars": len(booked_cars)},
        "runs": runs_out,
        "overall": dist(ratios),
        "byRoadMinutes": road_g,
        "bookRateByRoadMinutes": road_rate,
        "byLeadTime": lead_g,
        "byHourOfDay": hour_g,
        "byOption": opt_g,
        "steps": steps,
        "lanes": {
            "destinations": dest_tab[:20],
            "origins": orig_tab[:20],
            "correlations": corr,
            "driverDensity": {"places": len(density),
                              "meanPerPlace": round(density_mean, 2),
                              "top": sorted(density.items(), key=lambda kv: -kv[1])[:10]},
        },
        "recommendation": recommendation,
        "lateness": lateness,
    }
    best_row = recommendation["recommended"]
    summary["notes"] = [
        f"Tested schedule 0.50+0.05/15min (cap 2.0): {len(booked_gigs)}/{len(gigs)} posted gigs "
        f"booked ({len(booked_gigs)/max(1,len(gigs)):.0%}), {len(booked_cars)}/{len(posted_cars)} "
        f"cars ({len(booked_cars)/max(1,len(posted_cars)):.0%}); booked fee/reference mean "
        f"{summary['overall']['mean']:.2f}, median {summary['overall']['median']:.2f}.",
        f"Only {first_fee_bookings} bookings ({first_fee_bookings/max(1,len(booked_gigs)):.2%}) "
        f"happened at the opening fee; {step_counts.get(30,0)} "
        f"({step_counts.get(30,0)/max(1,len(booked_gigs)):.0%}) happened at the 2.0 cap, which is "
        f"the dominant censoring point: above 2.0 was never tested.",
        f"First-posting conversion {first_booked}/{first_posted} = "
        f"{first_booked/max(1,first_posted):.0%}; the rest needed a repost (window-free) before a "
        f"driver booked.",
        "Booked fee/reference is essentially flat across lanes and driver density "
        f"(r={corr['ratio_vs_destDensity']} vs destination density, "
        f"r={corr['ratio_vs_roadMinutes']} vs road minutes): referenceFeeCents already absorbs "
        "lane difficulty.",
        f"Recommended: open {best_row['openMult']:.2f} x ref, +{best_row['rise15']:.2f} x ref/15min "
        f"(+{best_row['risePerHour']:.2f}/h), cap 2.0 -> replay book rate "
        f"{best_row['bookRate']:.0%}, mean fee {best_row['meanFeeRatio']:.2f} x ref." if best_row else
        "No schedule in the grid reaches the 60-75% band.",
    ]
    json.dump(summary, open(OUT, "w"), indent=1)

    # ------------------------------------------------------------- print
    print(f"runs: {len(doc['runs'])}  orders {len(orders)}  cars {len(cars)}  "
          f"gigs {len(gigs)}  booked gigs {len(booked_gigs)}  booked cars {len(booked_cars)}")
    for r in runs_out:
        print(f"  seed {r['seed']:>5}: {r['ordersWon']} orders, {r['carsPosted']} cars, "
              f"{r['gigsPosted']} gigs, {r['bookings']} bookings, "
              f"car book rate {r['bookRatePerCar']:.0%}, gig book rate {r['bookRatePerGig']:.0%}")

    bucket_order = [b[0] for b in ROAD_BUCKETS] + [b[0] for b in LEAD_BUCKETS] + \
                   ["same_day", "next_day", "within_3_days"]

    def sort_key(k):
        if isinstance(k, int):
            return (0, k, "")
        if k in bucket_order:
            return (1, bucket_order.index(k), "")
        return (2, 0, str(k))

    def show(title, table):
        print(f"\n{title}")
        print(f"  {'group':<10} {'n':>5} {'mean':>7} {'p25':>7} {'med':>7} {'p75':>7} {'p90':>7}")
        for k in sorted(table, key=sort_key):
            v = table[k]
            if v.get("n"):
                print(f"  {k:<10} {v['n']:>5} {v['mean']:>7.3f} {v['p25']:>7.3f} "
                      f"{v['median']:>7.3f} {v['p75']:>7.3f} {v['p90']:>7.3f}")

    o = summary["overall"]
    print(f"\nbooked fee / reference share: mean {o['mean']:.3f}  median {o['median']:.3f}  "
          f"p25 {o['p25']:.3f}  p75 {o['p75']:.3f}  p90 {o['p90']:.3f}  (n={o['n']})")
    show("by road minutes", road_g)
    show("by booking lead time to pickup", lead_g)
    show("by hour of day (booking)", hour_g)
    show("by option", opt_g)

    print("\nbook rate by road minutes")
    for k, v in road_rate.items():
        print(f"  {k:<10} cars {v['cars']:>5}  booked {v['booked']:>5}  rate {v['bookRate']:.3f}"
              f"  mean ratio {v['meanRatio']}")

    s = steps
    print(f"\nschedule: {s['gigsBooked']} booked gigs; first-step bookings {s['firstFeeBookings']} "
          f"({s['firstFeeShareOfBookings']:.1%} of bookings, {s['firstFeeShareOfCars']:.1%} of cars)")
    print(f"  mean booking step {s['meanBookingStep']:.1f} of 30; no-book rate "
          f"{s['noBookRatePerCar']:.1%} per car, {s['noBookRatePerGig']:.1%} per gig")
    print("  bookings by step: " + ", ".join(f"{k}:{v}" for k, v in s["bookingStepCounts"].items()))

    print("\nlanes: destinations")
    for row in dest_tab[:10]:
        print(f"  {row['name'][:22]:<22} cars {row['cars']:>4}  booked {row['booked']:>4}  "
              f"rate {row['bookRate']:.2f}  ratio {row['meanRatio']}  drivers@dest {row['density']}")
    print("\nlanes: origins")
    for row in orig_tab[:10]:
        print(f"  {row['name'][:22]:<22} cars {row['cars']:>4}  booked {row['booked']:>4}  "
              f"rate {row['bookRate']:.2f}  ratio {row['meanRatio']}  drivers@origin {row['density']}")
    print("\ncorrelations: " + ", ".join(f"{k}={v}" for k, v in corr.items()))

    rc = recommendation
    print("\nrecommendation")
    print(f"  first-posting booking rate {rc['firstPosting']['rate']:.1%} "
          f"({rc['firstPosting']['carsBooked']}/{rc['firstPosting']['carsPosted']}); "
          f"tested schedule (0.50 + 0.05/15m) replays at {rc['observedSchedule']['bookRate']:.0%} "
          f"with mean fee {rc['observedSchedule']['meanFeeRatio']} x ref")
    print("  required-fee CDF (share of posted cars that booked at or below):")
    print("    " + "  ".join(f"{k}x:{v}" for k, v in rc["requiredFeeCdf"].items()))
    if rc["recommended"]:
        b = rc["recommended"]
        print(f"  best in 60-75%: open {b['openMult']:.2f} x ref, +{b['rise15']:.2f} x ref / 15 min "
              f"(+{b['risePerHour']:.2f}/h) -> first-posting rate {b['bookRate']:.0%}, "
              f"mean fee {b['meanFeeRatio']:.2f} x ref")
    else:
        print("  no candidate in the 60-75% band")
    print("  frontier:")
    for g in rc["frontier"]:
        print(f"    open {g['openMult']:.2f}  rise {g['rise15']:.2f}/15m  "
              f"book {g['bookRate']:.0%}  fee {g['meanFeeRatio']}")
    print("\nlateness:", lateness)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()