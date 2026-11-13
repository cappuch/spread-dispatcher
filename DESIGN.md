# DESIGN — level-8 dispatch policy: beat House best (€12.1k/run)

Scope: one policy, quantified, that maximises gross margin subject to ≥90% on-time
(target 94–96%), for the level-8 week described in `HANDBOOK.md` / `RULES.md`.
Everything here is measurable from the world itself; nothing needs a hidden value.

Evidence base (all numbers below are computed from these files, not guessed):

| Input | What it gives |
|---|---|
| `study1.json` | 1038 level-8 price requests, a no-action week (`nreq=1038`, `nlegs=1052`, 14 trade-ins, 40 dealers) |
| `data/map.json` | 30 places, 900 road rows (km + minutes) |
| `data/roster.json` | 805 drivers, home town, rating, licence, drops (one practice world) |
| `data/pricing_probe.json` → `data/lane_prior.json` | 4 level-8 worlds, 6976 randomised quotes: **the dealer acceptance curve** |
| `data/fee_probe.json` → `data/fee_summary.json` | 2 level-8 worlds, 2500 gigs / 1676 bookings: **the driver fee market** |
| Leaderboard (given) | House best 6,044,870 c over 5 runs = **1,208,974 c/run**, 92–98% on-time, 198–465 orders/run |

---

## 0. Recommended configuration (the one to build)

| Lever | Value | Why (see §) |
|---|---|---|
| Quote, base ratio | `k = 1.303 × m_lane × m_day × (1 + 0.01·trust_d)` (×list of the option) | 50% win at 1.303, measured (§2.1) |
| Quote, same_day | `+0.00` vs base ratio | dealer takes same_day 73% at Δ=0 (§2.3) |
| Quote, next_day | `−0.05` vs base ratio | moves ~13 pts of mix to the cheap-to-serve option |
| Quote, within_3_days | `−0.12` vs base ratio | 3 days of slack is our cheapest supply |
| Decline | quote `2.6 × list` on every option | losing costs nothing; not quoting costs goodwill |
| Accept test | expected margin/order > 0; in a region over its guard, also margin/driver-hour ≥ 350 c/h | §3.1, §3.3 |
| Opening driver fee | `f0 = ref × (1.00 + 0.16·e^(−SUP/30))`, cap 1.25 | Monday market books at 0.90–1.13×ref (§4.1) |
| Fee schedule | **2 steps only**: `f0` now, `1.15·f0` at `T−5h` (T = `lastPickupAt − now`) | long ramps teach drivers to wait: 1.40→2.00×ref Mon→Fri (§4.1) |
| Public fee cap | `1.35 × ref` | above it, escalate with **direct offers**, not public raises |
| Escalation | T−8h reprice 1.25·f0; T−5h 1.35·ref; T−3h `deadline_close` → 3 direct offers at 1.5–1.8·ref | §4.4 |
| Combine | multi-car gig when drive ≤ 660 min and saving ≥ 40 min | 70% of same-batch pairs qualify (§5) |
| Trust | put 55–65% of wins through D01–D08; never be late there | +25% accepted price ⇒ +53% gross margin (§1.4) |
| Predicted | **~1.74 M c/run** (€17.4k), 481 orders, 5.6% late, ~2,300 driver-hours | §9 (simulation on `study1.json`) |

Predicted vs House best: **1.74 M vs 1.21 M c** (+44%), 481 vs ≤465 orders, on-time 94.4% vs
92–98%. The prediction is conditional on `μ` (§4: realised fee/ref) landing at ≈1.08; each +0.10 on
`μ` costs €3–4k/run, so `μ` is the single most important thing to measure in the first hour.

---

## 1. Where the money is — four measured levers

### 1.1 Price geometry is deterministic (free information)

Fitting `listPriceCents` against road km over the 1464 single-leg option instances:

```
next_day      list = 2003 + 25.0·km     (residual sd 29 c, n=727)
same_day      list = 2156 + 27.0·km     (residual sd 29 c, n=656)
within_3_days list = 1867 + 23.2·km     (residual sd 29 c, n=81)
```

`referenceFeeCents ≈ 1769 + 38.3·road_minutes` (median 52 c/min, p10 39, p90 78).
So from the map alone we know every option's list price to ±1% and the lane's km — the list
price is *not* a market signal, only a distance. The market signal is the ratio we quote.

The economically decisive statistic is **r = ref / list** (our cost per euro of revenue):

```
r: p10 0.70 | p25 0.82 | median 0.91 (request-weighted 0.936) | p75 0.99 | p90 1.10
```

`r < 0.85` is where margin exists; `r > 1.05` (153 of 1038 requests) is where it dies (§3).

### 1.2 The dealer acceptance curve (measured, 1510 clean single-option quotes, 4 worlds)

Measured win rate by quote/list bin (1510 clean single-option quotes, 57–81 per bin), with the
logistic `p(win) = σ(b0 + b1·ln k)`, `b1 = −10.0` fitted through them:

```
k (quote/list)  0.78 0.88 0.98 1.03 1.08 1.13 1.18 1.23 1.28 1.33 1.38 1.43 1.48 1.53 1.58 1.63 1.68
p(win)          1.00 1.00 0.94 0.93 0.87 0.81 0.69 0.61 0.58 0.42 0.39 0.39 0.24 0.20 0.13 0.07 0.05
```

**50% crossing at k = 1.303** (SE 0.010; per-world 1.241 / 1.318 / 1.334 / 1.384). Slope −10 per
ln means a **10% quote error costs ~24 points of win probability** — pricing must be calibrated,
not guessed. Quoting near the crossing also keeps the competitor's price response neutral: it cuts
where we win most of a dealer's requests and raises where we win few, so a ~50% win share is the
cheapest point that is not read as dominance.

### 1.3 The fee market and the learning trap (measured, 1676 bookings)

The probe posted every gig at `0.50 × ref`, +5% of ref per 15 min, cap 2.0. Result:

| day | gigs | booked | median booked fee/ref | median time-to-book |
|---|---|---|---|---|
| Mon | 366 | 85.0% | **1.40** | 234 min |
| Tue | 531 | 67.2% | 1.70 | 260 |
| Wed | 536 | 65.1% | **2.00** | 294 |
| Thu | 445 | 60.2% | 2.00 | 359 |
| Fri | 550 | 62.7% | 2.00 | 925 |

and inside Monday: 08h → 0.90×ref, 10h → 1.13, 12h → 1.07, 14h → 1.55, 16h → **2.00**.

Only 0.4% of bookings happened at the opening fee; 60% at the cap. That is not a market
constant, it is the equilibrium of *our own* schedule: `RULES.md` says each driver who viewed a
gig learns "the booked fee over the first fee that a driver viewed", and `HANDBOOK.md` says "a
system that always raises its fees teaches drivers to wait". Opening at 0.5× ref taught the market
a 4× rise, and by Wednesday drivers demanded the cap.

**Consequence (the core strategic claim):** fee level is a *state variable we set on Monday
morning*, not a per-gig optimisation. Opening near the clearing fee (1.0–1.15× ref) and capping
the public rise at ~1.15× keeps `μ` (mean booked fee / ref) near 1.1 all week instead of 1.67.
On the mean order (list 8,524 c, ref 7,979 c) a drop of 0.47 in `μ` is
**+3,750 c per order** = +1.7 M c over ~460 orders.

### 1.4 Trust compounding (level-8 rule)

`RULES.md`: each on-time order raises the prices the dealer accepts by 1%, up to 25 orders
(+25%); a late order halves the count. `study1.json`: 40 dealers, D01 = 309 requests (30%),
top-5 = 57%, top-10 = 74%.

Simulated on `study1.json` with the measured acceptance curve (threshold × `(1+τ)`):
trust off 1.14 M c, trust on 1.74 M c → **+53% gross** (net of the competitor's "few steps" cut,
budget +30–35%). A single late order at a 25-trust dealer halves the count to 12 (−13% of accepted price
on every later win there). For D01 (309 requests, ~60 remaining wins after a Wednesday slip) that
is ≈ **700 €**; for a 40-request dealer ≈ 150 €; against a 10% refund of ~90 €. Trust is a *risk*
variable, not a reward: it argues for paying drivers generously and never gambling on a thin fee.

### 1.5 Supply is local, not global

`SUP(lane) = N30(pickup) + 0.5·N30(delivery)`, where `N30(p) = drivers whose home is within 30 km
of p` — the notification radius from `RULES.md` ("A driver's app notifies them of a new gig when
its first pickup is within 30 km of their home"). From `data/roster.json`:

```
helsinki 403  espoo 403  vantaa 403  turku 165  raisio 165  jyvaskyla 52  kuopio 21  seinajoki 21
hyvinkaa 21  lahti 18  tampere 14  lempaala 14  oulu 13  vaasa 12  kotka 8  pori 7  kokkola 7
hameenlinna 6  kajaani 6  rovaniemi 5  joensuu 4  mikkeli 4  salo 4  rauma 4  porvoo 3  ylivieska 3
kouvola 2  lappeenranta 1  kemi 1  savonlinna 0
```

(helsinki, espoo and vantaa share one 403-driver pool — the same drivers, counted once each.)

Requests vs supply (pickups): hyvinkaa 234, vantaa 137, espoo 137, oulu 55, helsinki 51, lempaala 41,
rovaniemi 36, vaasa 35, lahti 33, kuopio 33. So the demand is spread over places that hold 1–13
drivers while 70% of drivers sit in two clusters. Aggregate capacity is *not* the constraint:
805 drivers × ~30% working ≈ 242 driver-days/day ≈ 1,700/week and ~9,300 driver-hours/week, while
500 orders × ~4.7 h = 2,300 driver-hours (§9) — **35% of the pool overall, and more than 100% in
Oulu, Rovaniemi, Kajaani and the Vaasa–Kokkola corridor. The constraint is geographic and
temporal.**

---

## 2. Quote policy

### 2.1 The model

For each option of a request:

```
k        = quoteCents / listPriceCents            (our ratio, the decision variable)
theta_d  = 1.303 × m_lane × m_day × (1 + 0.01·min(25, trust[d]))
p(win | option chosen) = σ( -10.0 · ln(k / theta_d) )
```

* `m_lane` — from `data/lane_prior.json`: per-lane multiplier when `n ≥ 8` (86 lanes), else
  `sqrt(m_origin · m_dest)` from the place tables (25 origins, 26 destinations), renormalised so
  the request-weighted mean over the week is 1.00. Observed spread (fixed slope −8): origins
  0.90–1.04 (vantaa 1.041 … pori 0.898), destinations 0.92–1.21 (mikkeli 1.209, ylivieska 1.112,
  hyvinkaa 0.923, porvoo 0.916), lanes 0.79–1.23 (vantaa>jyvaskyla 1.229, hyvinkaa>lempaala 0.837).
  The destination carries most of the signal: far-away destinations accept dearer quotes, because
  list grows with km while the competitor's lane factor grows faster on hard lanes.
* `m_day` — start at 1.00; measured day effects were Mon 1.385 / Tue 1.387 / Wed 1.340 /
  Thu 1.193 / Fri 1.276. Update daily from the morning's wins (§7).
* `trust[d]` — count of on-time deliveries to dealer `d` this week, from the ledger (§7).

Parameter ranges to sweep: base 1.27–1.35, slope −8…−12, `m_day` 0.90–1.10.

### 2.2 Which options to quote

Quote **every option the dealer offers** (`study1.json`: same_day on 662 requests, next_day on
735, within_3_days on only 81; 303 requests are same_day-only, 344 next_day-only). Never leave an
option unquoted — a quote that loses costs nothing, and fewer options only shrinks the chance the
dealer picks one of ours. On a request where we do not want the work, quote `2.6 × list` on all
options (that is ~0.03 win probability and zero goodwill cost).

### 2.3 Pricing the three options — steering the dealer's choice

Dealers weigh our prices against the value of speed. Measured on 1576 won requests where we quoted
both same_day and next_day, with `Δ = k_same_day − k_next_day`:

```
Δ        ≤−0.2  −0.1   0.0   +0.1  +0.2  +0.3  +0.4  ≥+0.5
P(same_day) 0.96  0.95  0.73  0.47  0.27  0.10  0.10  0.03
```

A 0.1 change in Δ moves the mix ~25 points. Because the option mix is *ours to choose*, price it
by our own cost of supply:

| option | our supply | list vs same_day | quoted ratio | note |
|---|---|---|---|---|
| `same_day` | due today 20:00; a request at 15:00 leaves ~5 h to book | 1.00 | `k` | only offered when the car can already arrive 2 h before the deadline |
| `next_day` | +24 h of booking window, driver can plan | 0.93 | `k − 0.05` | mixes ~60/40 toward next_day |
| `within_3_days` | +2 business days; can be chained with the next day's work | 0.86 | `k − 0.12` | when offered (7.8% of requests) it is our cheapest car to move |

Worked example R00001: porvoo→hyvinkaa 58 min, list 4000/3700, ref 4435 (r=1.11 → decline), vs
R00500: vantaa→espoo 25 min, list 2900, ref 1817 (**r=0.63**) → quote 1.30·2900 = 3770, fee
1.05·1817 = 1908, margin **1862 c on a 25-minute drive** (74 c/driver-minute — the best class in
the book).

### 2.4 Expected margin per order

`E[margin] = p(win) · (k·L − μ·ref)`, with `μ` the *realised* fee/ref (§4.2: 1.08 × `m0`). At
`μ=1.08` and `k=1.303`:

```
r         0.60   0.70   0.80   0.90   1.00   1.10
margin/L  0.65   0.55   0.44   0.33   0.22   0.11     (×L, at 50% win)
```

Full-population expected values (1024 single-leg requests, measured curve, all quotes at `k`):

| μ (fee/ref) | k=1.20 | k=1.30 | k=1.40 | k=1.50 |
|---|---|---|---|---|
| 1.10 | 941k c / 616 wins | **1,061k c / 483 wins** | 947k / 322 | 727k / 196 |
| 1.20 | 540k / 505 | 715k / 444 | 704k / 311 | 576k / 192 |
| 1.30 | 268k / 334 | 425k / 367 | 483k / 284 | 432k / 185 |
| 1.67 (probe equilibrium) | 30k / 66 | 44k / 70 | 58k / 74 | 72k / 74 |

Read this table as the whole business case: **k≈1.30 and μ≈1.05–1.10 is the peak**; the same
system at the probe's fee equilibrium earns 4% of it. It also reproduces House best
(1.21 M, ≤465 wins) at μ≈1.10 — and shows the headroom is in μ and trust, not in quoting higher.

### 2.5 The unannounced events

* **Price war** (one working day, 08:00–18:00, competitor −20% everywhere): `theta_d` drops 20%,
  so our usual quote wins ~10% instead of 50%. Detect it (rolling win rate over the last 20
  resolved quotes < 20% while `k ≈ theta`) and mark the day `m_day = 0.82` for the rest of the
  10-hour window; do **not** chase below `k = 1.05` (a 5% margin at 90% win is worse than 33% at
  50% when drivers are scarce). Restore `m_day` the next morning.
* **Outage** (competitor down 08:00–18:00 in a town + 80 km; dealers there use a backup ~30%
  dearer): during the outage, quote `1.30 × theta` in that region — a 30% higher accepted price at
  the same win probability. Detect from a burst of wins at unchanged `k` for one region (≥8 wins in
  20 quotes) and lift `m_day` to 1.3 for that region's requests only.
* **Flu / bulk order**: they hit the *fee* side, not the quote side (§3.4, §6).
* Long horizons: `within_3_days` and Friday's `next_day` (deadline = last Monday, 259 of 735
  next_day options) are insensitive to a one-day outage or war — prefer them on war/outage days.

---

## 3. Order selection

### 3.1 The accept test (evaluated per request, before quoting)

```
for each option o:
    L, ref, mins  = o.list, request.ref, lane minutes
    k_o           = theta_d (adjusted by §2.3 for the option kind)
    fee           = 1.08 · f0(SUP)                     # realised fee, §4.2
    rev           = L · k_o                             # k_o already carries (1+0.01·trust[d])
    m             = rev − fee − E[late cost]           # E[late] ≈ 0.05·0.35·rev (§6)
    p             = σ(−10·ln(k_o/theta_d))
    e             = p · m
accept  iff  max_o e > 0
         and (region not over the §3.3 guard  or  m / driver_hours(o) ≥ 350 c/h)
         and P(no driver | SUP, T) < m / (2L + m)      # ≈ 12–15%, §6
         and not declined-by-policy (§3.2)
driver_hours(o) = 1.75 · lane_minutes / 60
```

Why `e > 0` is the global test and `c/h` is regional: driver-hours are 35% used nationally (§1.5),
so a global floor only throws away profit; but the distribution of margin per driver-hour is wide
(p5 100, p25 329, median 543, p75 814, p90 1,686 c/h), and in a tight region the low end crowds out
the high end. Metro short hops earn ~42 c/driver-minute, the median accepted order ~9 c/min.
So: accept on `e > 0`; when a region passes the `open/drivers > 0.6` guard, keep only its
`≥ 350 c/h` quarter and let the freed drivers take the rest of the day's book.

Why `P(no driver) < m/(2L+m)`: an undelivered car earns nothing and costs *twice its price*
(`RULES.md` account table). With `m = 0.33L` the break-even failure probability is 14%; below that,
taking a thin order is +EV, above it, a single failure wipes out four wins.

### 3.2 What to decline

Decline = quote `2.6 × list` on every option of the request (never leave it unquoted: "a request
that you do not quote costs a little goodwill"). Sources of declines, in order of frequency:

1. `r = ref/list > 1.05` (**153 requests, 1.53 M c of list**, median list 9.6k, median 208 min,
   median lane ends in a town with < 30 drivers within 30 km). Break-even is `r = k/μ`: at μ=1.08
   that is 1.204, but these lanes carry `m0 ≈ 1.07` ⇒ μ ≈ 1.16, so a 1.10 lane still clears ~290 c
   on six driver-hours (~50 c/h). They are declined because that is the worst driver-hour in the
   book, not because the order loses money.
2. `SUP < 20` **and** lane > 240 min (**64 requests**): a long run into or out of a town with almost
   no drivers — rovaniemi (5 within 30 km), joensuu (4), mikkeli (4), rauma (4), porvoo (3),
   ylivieska (3), kouvola (2), lappeenranta (1), kemi (1), savonlinna (0) — plus oulu (13) on its
   30 longest lanes. These are the bottom decile of margin per driver-hour.
3. The two classes above overlap in 15 requests; the union is **202 requests (19%)**, which is the
   ceiling on what the policy declines. Anything not in it is contested.
4. Premium car (`car.premium`, 54 legs, 5%) with `r > 0.75`: only 64 of 805 drivers have
   rating ≥4.8 and ≥50 gigs, and a violation is a €150 fine.
5. Electric car (`146 legs`) with `<30%` charge on a >200 km route unless we post `withCharging`
   (40 min) — otherwise 90 min late + €100 fine.
6. Note with a tighter deadline (`noteDeadline`, 52 notes) that breaks our buffer (§6).

The `e ≤ 0` test alone declines 86 of the 1024 single-leg requests; classes 1–3 add the ones with
positive but poor margin per driver-hour, and only where a region is tight. Everything else is
contested.

### 3.3 Capacity budget and the weekly target

```
driver-days/day = 805 drivers × 0.30 = 242
gig-hours/day   = 242 × ~5.5 h ≈ 1,330 h  →  ~9,300 h/week
our need        = orders/day × (1.75 × mean_lane_minutes / 60)
```

Recommended target: **95–110 orders/day, 480–520/week** (the simulation gives 455–491 wins with
2,100–2,400 driver-hours, i.e. 33–40% of the pool), leaving ~2.5× headroom for the flu day (−30%
drivers), the bulk batch and the regional overhang. Daily shape from the request flow (Mon 219, Tue 242, Wed 191, Thu 207, Fri 179)
and the trust ramp: front-load **Monday–Tuesday** (target 110/day) because trust, once earned,
pays on every later quote to that dealer, and back off on Friday except for Friday-deadline cars
whose margin is highest (r is lowest on the metro hops).

Regional budget (hard guard): keep `open unbooked cars in region / drivers in region ≤ 0.6`.
Regions = Helsinki metro (esp+van+hel+hyv+porvoo), Turku (turku+raisio+salo), Jyvaskyla, Oulu,
Vaasa, Kuopio, Lahti, Tampere, "remote east/north" (kajaani, rovaniemi, joensuu, savonlinna,
mikkeli, lappeenranta, kemi, ylivieska, kokkola). When a region is over the guard, that region's
new requests are declined and the existing gigs get the escalation ladder early.

### 3.4 Bulk batches

`study1.json`: 128 batches cover 496 requests (48%); 122 of 128 come from a single yard.

* **B0128, the level-6 bulk order**: 38 cars, all espoo, all `same_day`, deadline Wed 20:00,
  list sum 287,000 c, ref sum 250,829 c (r = 0.87), drive median 92 min, max 405. Plan: 19
  two-car gigs (or 13×3 where the drive sum stays ≤ 660 min), pairwise by destination (7→hyvinkaa,
  3→vantaa, 3→helsinki, 3→turku+raisio, 3→oulu) — one driver serves a yard and one direction, so
  each gig saves a return trip. Fee per gig = sum of the cars' ref shares × μ; reject cars whose
  destination is > 300 min away unless the fee is offered directly to a driver already ending there.
* **B0001** (24 cars from Kajaani → Joensuu/Oulu/Ylivieska, next_day, Wed deadline, list 206,800,
  ref 195,370, r = 0.94): **Kajaani has 6 drivers within 30 km.** Take at most 6–8 of these cars
  (one wave, multi-car gigs of 2, fees 1.5–1.7× ref, direct offers), decline the rest, and expect
  the 3 Oulu cars to be the expensive tail (405 min drive). This is the pattern that turns a bulk
  order from a jackpot into a delivery-minimum failure.

Rule: a bulk batch is a **capacity event**, priced as one: quote the batch at `k = theta` but cut
the count to what the region can move today + tomorrow, not to what the batch offers.

### 3.5 Trade-ins

14 requests (1.3%): two legs, one option only, one price for both. `study1.json`: 13 of 14 fit the
12-hour rule (`drive(car1) + gap(delivery1→pickup2) + drive(car2) ≤ 720`); R00925 needs 793 min and
must be split into two gigs. Post as one gig with the old car's pickup **immediately after** the new
car's delivery (`RULES.md`: the driver then collects the old car in the same visit, and that pickup
does not limit when a driver can book). If split, the old car alone must be collected inside the
customer's slot and the slot never extends — so a split trade-in with a tight slot is a decline.
Fee: `f0(SUP) × 1.05` (the trade-in surcharge of §4.2) applied to the order's `ref`. The two legs
share one driver and one return trip, so the fee is far below 2× a single leg's fee — that is where
the margin is on the 14 trade-ins.

---

## 4. Driver fee policy

### 4.1 What the market actually charges (measured)

From `data/fee_probe.json` (2500 gigs): hazard of booking per gig-hour by visible fee/ref —

```
fee/ref   0.5   0.6   0.7   0.8   0.9   1.0   1.1   1.2   1.3   1.4   1.5   1.6   1.8   2.0
λ (/h)    .014  .041  .042  .085  .099  .089  .078  .067  .066  .079  .091  .054  .049  .026
```

The instantaneous booking rate peaks at **0.9–1.0× ref** and does not improve above ~1.2. Paying
2× ref does not buy a faster booking; it buys the *residual* car that nobody would take cheaply.
Book rate per gig was 63–69% (reposts recovered the rest), median time-to-book 345 min.

So: open at the peak of the hazard, and reserve the large fees for the residual. Combined with §1.3
(learning), the optimal public schedule is short and low.

### 4.2 Opening fee formula

```
SUP      = N30(pickup) + 0.5·N30(delivery)
m0(SUP)  = 1.00 + 0.16·exp(−SUP/30)            # SUP=0 →1.16, 20 →1.08, 200 →1.00
f0       = ref × m0(SUP) × t_hour × t_risk
t_hour   = 1.00 + 0.08·[pickup ≥17:00] + 0.10·[Saturday] + 0.05·[ready 07:00]   (cap 1.15)
t_risk   = 1.10·[private pickup or delivery] + 1.05·[trade-in] + 1.15·[premium car]
                                                             + 1.05·[manual-licence only]
cap      : f0 ≤ 1.25 × ref   (never open higher — it teaches the market we pay 1.25)
```

Measured support: Monday's bookings cleared at 0.90 (dense) to 1.13 (thin) × ref; the 08h Monday
median was 0.90. `t_risk` terms are the cost items `RULES.md` lists for drivers (evening, weekend,
private address, trade-ins, plates) with the premium/manual terms sized by the audience loss
(premium → 8% of drivers; manual needed only for `licence: manual` cars, 86% of drivers can).

Expected realised `μ` (fee actually paid / ref) = **1.08 × m0**, i.e. 1.03–1.10 for metro lanes and
1.12–1.25 for thin ones, because about half the gigs take the single 15% step of §4.3. If the first
40 bookings show a median booked ratio > 1.25, add +0.10 to `m0(SUP)` for every later gig (this
world's drivers are dearer than the probe's) and re-run the accept test (§3.1) — the selection then
shrinks automatically.

### 4.3 Schedule shape and repricing

`RULES.md` gives 1–48 steps; fees never fall inside a schedule; a raise sends **no notification**;
a reposted car keeps the order's age and its first viewed fee. Therefore:

* schedule = **2 steps**: `[t0: f0, t0+Δ: 1.15·f0]` where `Δ = min(300, (T−240)·0.5)` minutes and
  `T = lastPickupAt − now`. Nothing above `1.35·ref` ever appears in the public schedule.
* **at most two reprices** per gig: `T = 8h` → `1.25·f0`, `T = 5h` → `1.35·ref`. The sequence
  `1.00 → 1.25 → 1.35` teaches a rise of 1.35, which is the most the market should ever learn.
* a `gig_expired` repost uses the same stops but `f0' = 1.3·f0` (a repost at a higher fee counts as
  a raise: "a driver hears of each order once", and the order keeps its age — so the repost is a
  *late* raise).
* never reprice a gig that has views but no booking at a fee already ≥1.2× ref: the viewers have
  judged it. Spend a direct offer instead.

### 4.4 Escalation ladder (the only place big fees are allowed)

| when | action | fee |
|---|---|---|
| T+0 | post (window `from` = max(readyAt, site opens); `to` = deadline cut to hours) | `f0` |
| T ≤ 8h | reprice if unbooked | `1.25·f0` |
| T ≤ 5h | reprice to cap, or withdraw+repost with `f0'` | `1.35·ref` |
| T ≤ 3h | `deadline_close` arrives ⇒ **3 direct offers**, 10 min each, to the 3 best drivers | `1.5–1.8·ref` |
| T ≤ 1.5h | last offers to the 2 nearest drivers; accept the lateness if they take it | `1.8·ref` |

Choosing the 3 offer targets (the only place we spend attention per driver):
`score(d) = exp(−d_home_to_pickup/40) + 0.6·exp(−d_lastgigend_to_pickup/40) + 0.3·[rating ≥ 4.8]`
restricted to `drops / max(1, completedGigs) < 0.10` and `licence` fit. Offers reach drivers a
notification does not (`HANDBOOK.md`) — this is the tool for pickups outside the 30 km ring
(oulu, rovaniemi, kajaani, savonlinna). Limits: 3 offers/driver/day, 3 open per gig.

### 4.5 Release policy

Release only to trade a bad booking for a better one, never for tidiness:

```
release iff  remaining_risk(new driver) < remaining_risk(old) − release_fee
release_fee = 25% (>24h to pickup) | 50% (<24h) | 100% (<2h) of the booked fee
```

Facts: `driver_late` can be released **free** until they leave; at a private pickup a
`driver_late` becomes `no_show` (no free window). So: for `driver_late` at a dealer site, decide
within the hour (a `pickupAt` that still beats the window end is usually fine — keep the driver);
for a `no_show` at a private slot, the slot never extends, so re-post immediately with `f0' = 1.4·f0`
and offers at 1.8 — the alternative is a late order.

### 4.6 Guardrails on the fee

* Mean booked fee ratio `μ̂ = Σ bookedFee / Σ ref` over a rolling 30 bookings. Target ≤ 1.15.
  If `μ̂ > 1.30` for 2 hours: drop `f0` by 10% for the next 20 posts (the market has learned a
  higher price than we intend) and route escalation through offers only.
* Never let a public fee exceed `1.35·ref`, and never let one step rise more than 1.25× (drivers
  learn the ratio, not the amount).
* Per-gig fee ceiling `2.0·ref` only after `deadline_close`; above that, a delivery-minimum
  decision (§6), not a fee decision.

---

## 5. Multi-car gigs

Rules: a gig with >1 car may plan ≤ 12 h (720 min) of driving including the drive between cars;
plates/charging minutes do not count; a trade-in's old-car pickup goes right after the new car's
delivery; a single car has no limit. Build with `combine.py` (saving model: `saving =
Σ direct_i − Σ connect_i`, `MAX_DRIVE_MINUTES=720`, `MAX_SPAN_MINUTES=1440`,
`MIN_SAVING_MINUTES=20`, which we override to 40).

**When to combine** (recommended): same day, same direction, drive sum ≤ **660 min** (60 min of
buffer under the rule), and `saving ≥ 40 min`. Observed opportunity in `study1.json`: 2019
same-batch pairs, **1407 (70%) satisfy ≤660 min**, 475 are identical lanes, median combined drive
362 min.

**How many cars**: 2 in the default case; 3 only when the yard is the same and all destinations are
within 120 min of each other (bulk batches). Never 4+ — one failed visit or closed site then
poisons the whole chain, and a drop costs every car in it.

**Why it pays beyond the fee**: in a shortage the scarce resource is *driver-days*, not fee. Two
cars in one gig need one notification, one commute to the pickup and one ride home; the saving is
one driver-day slot. Price a 2-car gig at `0.85–0.90 × (ref1+ref2) × μ` — the driver's extra cost
is only the hop between the two deliveries.

**Stop ordering**: for a same-yard pair, deliver the far car first if the near car's window is long;
always keep the *tighter* deadline last within its window. A gig's stop window may end after the
order's window (only a private pickup may not), so a late car can still ride in a multi-car gig —
never let a chain push a car past its own deadline though; that is what `combine.py`'s window check
is for.

**Trade-ins**: one gig, new car then old car pickup in the same visit; if drive > 720, split (13 of
14 fit, R00925 does not).

**Stop-ordering guard**: if the chain's last delivery lands after `deadline − 45 min`, post solo.

---

## 6. On-time risk management

On-time = every leg delivered by its deadline; late bands cost 10% (≤2 h), 25% (2–24 h),
50% (>24 h, ≤2 business days), 100% (beyond), and **undelivered at the end costs twice the price**.
A car left at a closed site counts as delivered when the site next opens (`RULES.md` rule 4), and
Saturday/Sunday do not count as business days.

### 6.1 The dominant hazard: Friday → Monday

`study1.json` deadline days: same_day → Friday 249, next_day → last Monday 259. A car due Friday
20:00 delivered Monday 08:00 is 60 h late = **50% refund**, even though the drive was fine. Cars
due the last Monday are safer: the world runs to Wednesday 22:00, and a late Monday car is typically
25% late, not 50%.

Policy: a Friday-deadline car must have a driver booked by **Friday 13:00** (T−7h); if not, it goes
straight to the T−5h cap and offers at 1.6–1.8× ref. Take no Friday-deadline order with `SUP < 40`
unless the fee budget supports 1.7× ref (i.e. `r ≤ 0.72`).

### 6.2 Early-warning thresholds

`deadline_close` comes only at T−3h, which is too late to be the first signal. Use our own clock
against `lastPickupAt` (given in `order_won`):

| clock | condition | action |
|---|---|---|
| post + 90 min | fewer than 5 views | wrong audience or place: re-post with `audience=everyone`, check `withPlates`/`manualLicence`, add `t_risk` |
| T−12h | no booking | no reprice yet — re-evaluate `SUP` (a flu day shows up as a 40% drop in views) and pre-select the 3 offer targets |
| T−8h | no booking | reprice #1 to `1.25·f0` |
| T−5h | no booking | reprice #2 to `1.35·ref`; 3 direct offers at 1.5–1.8× ref |
| T−3h | `deadline_close` | offers at 1.8× ref; if the offer is declined, **decide**: accept a 10% late (still on-time-credible) or, for a Friday car, pay 1.9 and keep trust |
| T−1h | still unbooked | the car will be late: log it, do not rebook more orders in that region today |

Booking in the *legal* window ends at `lastPickupAt` (the drive must still fit): a 6 h drive whose
delivery window ends at 20:00 can get no driver after 14:00, so the ladder above is measured from
`lastPickupAt`, never from the deadline.

### 6.3 Slack policy at accept time

Accept only if `deadline − max(readyAt, hours_open) − drive_minutes ≥ 90 min` **and** the note's
`noteDeadline` (if any) is ≥ `deadline − 45 min`. 1464 option instances were checked against
hours/weekdays: none is geometrically infeasible, but 2 have < 120 min of span; those are the ones
a note or a slow driver turns into a refund.

### 6.4 Notes and private addresses (the real failure source)

450 of 1038 requests (43%) carry a note. Classified: tyre-set chatter 124 (harmless), pickup hours
58, private access/keys 54, deadline statements 52, summer-tyres 37, ref codes 35, ready-later 19,
manual gearbox 12, plates 12, payment release 6.

Rules of engagement:

* **Any note that names a time or a day** → parse it into `pickupFrom` / `deliveryBy` and post the
  gig with those stop windows. Most failed visits come from a note the gig did not follow
  (`HANDBOOK.md`), and a failed visit costs **half the car's share of the fee, min €25**.
* Hours in a note override the stated hours: a driver who arrives when the site is really closed
  waits up to 2 h at €15/h and the delivery then counts at the next opening — that is a
  Friday→Monday 50% refund in disguise.
* Ready-later / payment-release notes: post the pickup window from the note's time, not from
  `readyAt`; the deadline moves with it (`order_won` already shows the moved deadline) — re-check
  `lastPickupAt` after every `release` / `order_changed`.
* Private pickups (61) and private deliveries (56): the window is booked and never extends; a
  `no_show` there is a gig back in the app with no free-release option. Give private stops their
  own window, `t_risk` +10%, and never chain them behind a long first leg.
* `summerTyres` + a snow `forecast` at either end: the car cannot be collected → decline unless the
  forecast clears before the pickup (the dealer moves the deadline one day, which *helps*).

### 6.5 Rebooking

* `gig_dropped` → re-post immediately (the gig re-notifies) with the schedule's next step; the fee
  ceiling rises to 1.5× ref for a re-post after a drop.
* `driver_late` at a dealer site → keep the driver if `pickupAt` beats the window end; otherwise
  release free and re-post + offers.
* `visit_failed` → re-post the single car without the cause (`withPlates`, manual audience,
  pickup window inside the real hours); half-fee loss is already booked, do not add a second one by
  re-posting with the same defect.
* `order_changed` → recompute `lastPickupAt`; if the new place is farther, the fee rises
  proportionally to the extra drive minutes (we pay the driver for extra driving anyway).

---

## 7. Learning during the week

State to keep, and the update rule for each:

| state | observed from | update |
|---|---|---|
| `theta_d` (world level × lane) | `order_lost` / `order_won` at the quoted `k` | rolling logistic per (lane, day): after each 20 resolved quotes, re-fit `b0` with slope fixed at −10 (`theta = exp(−b0/b1)`); shrink 50% toward the lane prior |
| `m_day` | same, grouped by day | multiplicative residual per day, clipped [0.85, 1.15] |
| `trust[d]` | ledger: revenue line with no refund | +1 on time, `//2` on late; recompute `1+0.01·min(25,·)` before every quote |
| realised `μ` | `gig_booked.bookedFee / ref` per lane bucket | EWMA (α=0.2) over the last 30 bookings per bucket; feeds `m0(SUP)` for new gigs only |
| book latency | `gig_booked.at − post.at` per bucket | if P75 latency > T−6h for a bucket, add +0.05 to that bucket's `f0` |
| drops / no-shows | `gig_dropped`, `no_show` per lane | blacklist a driver with `drops/gigs > 0.15`; add +0.03 to `f0` on lanes with a drop rate > 15% |
| war / outage | rolling win rate and win bursts (§2.5) | `m_day` overrides, with a 10-hour expiry |
| note reader accuracy | practice `reveal` (`noteDeadline`, real hours) | count misreads per phrasing class; any class with > 10% error gets the "decline if it names a time" rule |

**Priors for the final** (a fresh world, so these are all we have at 08:00):

```
theta_base       1.30   (the 4 measured worlds crossed at 1.241/1.318/1.334/1.384 — mean 1.32,
                        below the 1.40 centre of the documented 1.26–1.54 world level, so the
                        honest prior is 1.30 ± 0.06 and Monday's wins must move it)
slope            −10 per ln
m_lane           lane_prior.json table, renormalised to mean 1.00
m_day            1.00 for Mon–Tue; apply Thu 0.92 once the midweek pattern shows
realised μ       §4.2 prior; expect 1.03–1.25 (1.08 × m0)
trust            0 for every dealer at 08:00 — this is the reason for front-loading Mon/Tue
```

Deliberate choice for the final: **spend the first 90 minutes measuring, not maximising.** Quote
`k = 1.24` (a 70% win) on the first ~25 requests across ≥5 lanes and ≥4 dealers; that buys (a) a
`theta_d` fix per lane before any volume decision, (b) the first 25 trust points per dealer, (c) a
`μ̂` reading from the Monday market before fees are set for the week.

The probe is cheap because the profit surface is flat around the optimum: on `study1.json`, quoting
1.303 when the world's true level is 1.24 still earns 95% of the optimum (890k vs 933k c), and
quoting 1.24 when the level is 1.38 earns 92% (1,389k vs 1,503k c). What the probe buys back is
win share — 468 wins instead of 365 in the first case, 697 instead of 617 in the second — which is
exactly what builds trust. **Under-quote on Monday, converge by Tuesday noon.**

---

## 8. Failure modes and guardrails

1. **Over-winning** (the level-8 trap: "a system that wins every car it can is not ranked"). Hard
   cap: `open_unbooked_cars × 4.7 h < remaining_driver_hours_in_region`. Soft cap: the §3.1
   accept test already drops ~10% of requests.
2. **Delivery minimum**: maintain `on_time_ratio = on_time/orders` continuously; below 93% at any
   point, raise every region's `c/h` floor to 600 and stop taking new Friday-deadline work until it
   recovers. 90% is the cliff, not the target.
3. **Undelivered cars** (2× price): a car with no driver at T−90 min is worth one last 1.8× ref
   offer; never post a car whose window already ended (§6.2).
4. **Cancellations** (`order_cancelled`): no penalty from us, but each live gig is
   `withdrawn/released/shortened/continues` — on `withdrawn`, re-post the *other* cars of that gig
   immediately (they lost their ride); on `shortened` the driver keeps the booked fee.
5. **Fee inflation** (§4.6): `μ̂ > 1.30` → cool down. This is the only failure mode that cannot be
   recovered late in the week.
6. **Repost loops**: an expired gig keeps the order's age and its first-viewed fee; three reposts at
   rising fees teach "this order rises 3×". Max one repost per car, then offers.
7. **Rule traps**: >12 h drive in a multi-car gig (refused), a stop window already ended (refused),
   a premium car given to a non-experienced audience (€150), plates not fetched (`no_plates`),
   charging omitted (90 min + €100), a release after `gig_started` (refused by the world).
8. **Rate limits in the final**: 20 actions/s, 50 reads/s; batch 50 actions; act on
   `price_request` only after the quote for it is ready (15 world minutes ≈ 4.5 real seconds).
   Never let the action queue build during 09:00–18:00.
9. **Determinism / reproducibility**: every parameter above lives in one config object; a run must
   be replayable from (seed, config) so two versions can be compared on the same seeds —
   `advance(until=kinds, max=minute, actions=[...])` one request per step, as the starter does.

---

## 9. Predicted performance and calibration plan

Simulation on `study1.json` (1024 single-leg requests; quotes at `k = theta` on the best option —
i.e. the §2.3 steering gains and the §5 combine economies are *not* in these numbers, so they are a
floor; measured acceptance curve; `m_lane` noise σ=0.06; fee = `esc · f0(SUP)` with `f0` from §4.2;
`P(on-time)=0.94`; 10 seeds):

```
config                              margin/run  orders  late%  driver-hours
fee = f0,        trust off          1.41 M c     478    5.6%      ~2,400
fee = 1.08·f0,   trust off          1.14 M c     471    6.1%      ~2,165
fee = f0,        trust on           2.00 M c     491    4.9%      ~2,414
fee = 1.08·f0,   trust on           1.74 M c     481    5.6%      ~2,288   ← recommended
fee = 1.15·f0,   trust on           1.39 M c     455    4.6%      ~2,082
fee = 1.25·f0,   trust on           1.09 M c     427    5.9%      ~1,793
House best  (reference)             1.21 M c    ≤465    2–8%        n/a
```

Both axes of the table are policy choices, and both are worth more than any pricing trick:
trust effort (concentration + on-time) is +53% gross, and holding the realised fee at 1.08× the
opening fee instead of 1.25 is +60%.

Calibration order (each step is a sweep of ~6 seeds through `sim.py`, same seeds for both arms):

1. **Fee market**: `openMult ∈ {0.95, 1.05, 1.15}`, `rise15 ∈ {0, 2%, 5%}`, `cap ∈ {1.25, 1.35,
   1.5}` → pick the row with the highest margin at ≥94% on-time. Expect the flat/low-cap arm to win;
   if it does not, the world's drivers are dearer than the probe suggests and `μ` moves up (and the
   accept test shrinks the book — recompute §3.1 thresholds with the new `μ`).
2. **Quote level**: `k ∈ {1.20, 1.25, 1.30, 1.35}` × `m_day` handling. Check the realised win share
   against the curve; a systematic gap means the slope or the base is wrong (§2.1).
3. **Steering**: Δ(next_day) ∈ {0, −0.05, −0.10} → measure the option mix and the margin per
   driver-hour of the mix (the choice curve in §2.3 predicts the mix).
4. **Combine**: `min_saving ∈ {20, 40, 60}` min, `max_cars ∈ {2,3}` → on-time and fee per car.
5. **Trust allocation**: dealers by request count, top-5 vs top-10 vs all → margin/run; watch the
   competitor's price response (win share at unchanged `k` falling for a concentrated dealer).
6. **Escalation timing**: `T_cap ∈ {6h, 5h, 4h}`, offers at `{1.5, 1.7, 1.9}× ref`.

---

## 10. Alternatives (and why they lose)

**A. Win-rate maximiser** — quote 1.05–1.15 everywhere (≈90% win, ~900 orders = 4,300
driver-hours). Margin per order collapses to near zero on `r > 0.9` lanes; the 900 orders cannot be
moved by the thin regions (Oulu 13 drivers within 30 km, Rovaniemi 5, Kajaani 6) even though the
national pool has slack; and the fees needed to move them on time push `μ` to the cap — which is
the probe's 44–62% on-time and the starter's −€72k. Rejected: the level-8 scoring is margin under a
hard on-time constraint, not volume.

**B. Fee-hawk** — open at 0.7× ref, ramp slowly, "let the market come to us". Measured: the median
booked ratio climbs 1.40 → 2.00 in three days, book rate falls 85% → 60%, and 60% of bookings land
at the cap. At `μ=1.67` even `k=1.50` earns 72k c/run (§2.4). Rejected — this is the failure the
probe demonstrates.

**C. Uniform national pricing (no lane multipliers)** — one `k` for every lane. Measured cost on
`study1.json`: only ~2% of margin (1,182k vs 1,206k c), because the profit surface is flat near the
optimum — but a 5% error in the *world level* costs 5–8%, and it costs win share (365 vs 468 wins
at a 1.24 world), which costs trust. Recommended as the *fallback* when a lane has `n < 8` and no
confident place multiplier: use `k = 1.30` flat rather than a thin lane estimate.

**D. Trust-first but volume-heavy** (take 600 orders, accept 92% on-time): the trust cap is 25
*per dealer*, so concentration is the whole point. 8 dealers × 25 = 200 orders reaches the cap at
those dealers; 600 orders spread over 40 dealers averages 15 per dealer ⇒ τ ≈ 0.15 instead of 0.25
on the dealers that matter, and each late order halves a count (−13 points of price on every later
win to that dealer). Rejected: fewer, better, on-time orders to fewer dealers pay more than more
orders to more dealers.

**E. Long-horizon only** (quote only `next_day`/`within_3_days`, decline all `same_day`). It
removes the tightest supply, but `same_day`-only requests are 303 of 1038 and carry **36.7% of the
expected margin**; the option also pays an 8% list premium and the dealer prefers it (73% at equal
ratios, §2.3). Cost ≈ −37% margin for a supply saving the combine and offer machinery already
delivers. Use only as a midweek fallback when the driver book is over the §8.2 guard.