# Spread dispatcher

Entry point: `dispatch.py`. Policy: `v1.py`. Event loop and bookkeeping: `engine.py`.
Notes reader: `notes.py`. Multi-car gig planner: `combine.py`.

## Run

```sh
export SPREAD_URL=https://spread.bahnexpress.com
export SPREAD_KEY=<team key>
python3 -m venv .venv && .venv/bin/pip install $SPREAD_URL/kit/spread-0.1.0-py3-none-any.whl
.venv/bin/python dispatch.py --level 8 --seed study-1     # practice
.venv/bin/python dispatch.py --board                      # next ranking run
.venv/bin/python dispatch.py --final                      # the final
```

The final runner streams events, rebuilds the book from `snapshot()` + `events()` on every
reconnect, and sends actions immediately.

## Decisions

1. **Quote every option.** Declining is a quote at 2.6x list (costs no goodwill). Quoted ratio
   `k = 1.34 * (1 + 0.01*min(25, on-time orders to this dealer))`; next_day `-0.05`, within3 `-0.12`.
   The 1.34 prior comes from 1,510 clean level-8 quotes across four worlds: the dealer's 50%
   acceptance crossings were 1.24/1.32/1.33/1.38. Uniform pricing beat lane-specific priors on
   validation worlds (lane priors are noisy at n=5-20).
2. **Select by supply and risk**, not by expected revenue alone. Decline `ref/list > 1.05`, a thin
   pickup town (N30 < 20 drivers), a long lane into a thin destination, private stops in thin towns,
   high-value lanes with <12h lead, and a per-pickup-town daily win cap. A hard brake stops new
   wins if observed on-time falls below 92% (90% is the ranking cliff).
3. **Flat public fee, scheduled private escalation.** Post one fee
   `f0 = ref * (1 + 0.16e^{-SUP/30}) * 0.9`, then reprice unbooked gigs at `lastPickupAt-8h`
   (1.15 f0) and `-5h` (1.30 f0). The flat opening avoids teaching drivers to wait (the rising
   starter schedule booked only 0.4% of gigs at the opening fee); the later reprices buy the residual.
   Expired cars are reposted without windows at 1.9x ref to avoid the 2x undelivered penalty.
4. **Explicit stop windows** cut late bookings: delivery window ends 60 min before the deadline,
   pickup window at `lastPickupAt`. Without them the world lets a driver book a car days late.
5. **Combine cars** from the same batch/area when the plan is <=660 min of driving, saving >=40 min.
   Group fee 0.9 * sum of the cars' reference fees.
6. **Read notes** (fi/sv/en): pickup/delivery times, ready/release, manual licence, plates, summer
   tyres, tighter deadlines.
7. **Trust compounds.** On-time deliveries raise a dealer's accepted price up to +25%; quotes use
   that count from the first on-time order, and late orders are avoided near the cliff.

## Evidence

* `data/pricing_probe.json` – 1,510 clean quotes, four level-8 worlds.
* `data/fee_probe.json` – 2,500 gigs; flat-fee probe showed 98% of gigs book at ~1.0x ref when the
  fee does not rise in front of the driver.
* `data/tune*.jsonl` – parameter sweeps; final configuration validated on `tune-a..d` and `study-1`.
* `DESIGN.md` – the quantitative policy analysis behind the parameters.
