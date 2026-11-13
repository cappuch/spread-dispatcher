# Spread — team jaffa

## 1. Result
Level-8 practice worlds, our final policy: **€5.3k–€8.6k margin per week**, **94.6–98.3% on time**,
all ranked, on `tune-a..d` and `study-1`. Starter on the same level: about **−€72k**.
Our first ranking run: **€4,183, 172 orders, 97.7% on time**. House best on that world: **€16,371, 465 orders, 93.3%**.

## 2. Win fewer, better cars
We do not win every request. We quote `k = 1.34 × list` for each option (next_day −0.05,
within3 −0.12), and decline by quoting 2.6× list, which costs no goodwill.
The 1.34 prior is measured: 1,510 clean level-8 quotes, four worlds, 50% dealer acceptance at
**1.24 / 1.32 / 1.33 / 1.38**. Uniform pricing beat lane-specific priors: lane estimates from
5–20 samples are noise.

## 3. Price the risk, not only the revenue
A request is declined when `ref/list > 1.05`, the pickup town has <20 drivers within 30 minutes,
the lane is long into a thin destination, a private stop is in a thin town, or a >€200 lane has
<12h of lead. A per-pickup-town daily win cap stops us from flooding a region.
A hard brake stops new wins when observed on-time falls below 92% — 90% is the ranking cliff.

## 4. Flat fee, private escalation
Drivers learn raises. The starter's 1.0→1.5× ref ramp booked **0.4%** of gigs at the opening fee
and 60% at the cap. We post one fee `f0 = ref × (1 + 0.16e^(−SUP/30)) × 0.9`, then reprice only
unbooked gigs at `lastPickupAt−8h` (1.15 f0) and `−5h` (1.30 f0). Flat-fee probe: **98% of cars book
at about 1.0× ref** when the fee does not rise in front of the driver.

## 5. Windows and notes
Every stop carries an explicit window: delivery ends 60 min before the deadline, pickup at
`lastPickupAt`. Without them the world lets a driver book a car days late — our first version had
53 deliveries more than 24h late. The note reader (fi/sv/en) handles ready/release times, pickup
and delivery hours, tighter deadlines, manual licence, plates and summer tyres.

## 6. Combine, but do not delay
Same-batch cars with a feasible ≤660-minute route and ≥40 minutes saved go in one gig
(`combine.py`). Holding orders to find partners loses more bookings than it saves in fees; the
best grouping policy was to post within 10 minutes.

## 7. Adapt to the unknown world
Level-8 worlds differ. We detect a **price war** or a cheap competitor from the day's rolling win
rate (<16% after 30 resolved quotes) and cut that day's quote by 20–28%; a weak competitor
(>78% wins) lifts it 8%. The next day resets. This turned a board-world Monday that won 1% of
requests into a normal day.

## 8. Robustness and what did not work
The final runner rebuilds from `snapshot()` + `events()` on every reconnect and restart; a mid-week
rejoin test finished ranked. What did not work: calibration from all quotes (decline quotes biased
it down), merging new orders into already-posted gigs (duplicate fees), delaying orders to group
them (lost bookings), and deep group discounts (undelivered cars).
