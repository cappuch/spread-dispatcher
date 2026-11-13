"""Tests for combine.py: synthetic lanes, the real map, and the grouping rules."""
from __future__ import annotations

import json
import os
import random
import unittest

import combine

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "data", "map.json")
STUDY_PATH = os.path.join(HERE, "study1.json")
NOW = 480  # Monday 08:00


def make_date(places, lanes, unknown=400):
    """A map dict with every pair both ways; ``lanes`` holds one direction's minutes, and pairs it
    does not name get a long, unimportant road."""
    minutes = {}
    for a in places:
        for b in places:
            if a == b:
                minutes[a, b] = 0
            elif (a, b) in lanes:
                minutes[a, b] = lanes[a, b]
            elif (b, a) in lanes:
                minutes[a, b] = lanes[b, a]
            else:
                minutes[a, b] = unknown
    return {"places": [{"id": p, "name": p} for p in places],
            "roads": [{"from": a, "to": b, "km": m * 1.2, "minutes": m}
                      for (a, b), m in minutes.items()]}


def cand(order_id, place_a, place_b, leg=0, pickup=(NOW, 1440), delivery=(NOW, 2640)):
    return {"orderId": order_id, "leg": leg,
            "pickup": {"placeId": place_a,
                       "window": {"from": pickup[0], "to": pickup[1]},
                       "hours": None},
            "delivery": {"placeId": place_b,
                         "window": {"from": delivery[0], "to": delivery[1]},
                         "hours": None},
            "serves": [(order_id, leg)]}


def ids(gig):
    return [f"{c['orderId']}.{c['leg']}" for c in gig]


class Synthetic(unittest.TestCase):
    def setUp(self):
        # X<->Y is 60 minutes, Y<->Z is 20, X<->Z is 80 through Y.
        self.roads = combine.Roads(make_date(["X", "Y", "Z"],
                                            {("X", "Y"): 60, ("Y", "Z"): 20, ("X", "Z"): 80}))
        self.now = NOW

    def test_a_same_lane_pair_combines(self):
        """Two lanes on the same town pair, same direction, share one driver and one empty return."""
        cars = [cand("R1", "X", "Y"), cand("R2", "X", "Y")]
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        plan = combine.group_plan(groups[0], self.now, roads=self.roads)
        # X->Y, back to X empty, X->Y again: three lane drives, one of the two solo returns gone.
        self.assertEqual(plan.drive, 180)
        self.assertAlmostEqual(plan.saving, 60)
        self.assertGreater(plan.saving, combine.MIN_SAVING_MINUTES)

    def test_b_opposite_directions_combine(self):
        """A round trip on one town pair: the second car's pickup is where the first delivery ends."""
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "X")]
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual([ids(g) for g in groups], [["R1.0", "R2.0"]])
        plan = combine.group_plan(groups[0], self.now, roads=self.roads)
        self.assertEqual(plan.drive, 120)  # X->Y->X, no empty hop at all
        self.assertAlmostEqual(plan.saving, 120)
        # The hop between the cars is zero minutes and the plan arrives without waiting.
        self.assertEqual([s[3] for s in plan.stops], ["X", "Y", "Y", "X"])
        self.assertEqual([s[4] for s in plan.stops], [480, 540, 540, 600])

    def test_chain_of_three_is_ordered_by_town(self):
        # A triangle of lanes: one driver goes X->Y, Y->Z, Z->X and never drives empty.
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "Z"), cand("R3", "Z", "X")]
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)
        plan = combine.group_plan(groups[0], self.now, roads=self.roads)
        self.assertEqual(plan.drive, 160)  # 60 + 20 + 80, no hop costs anything
        self.assertAlmostEqual(plan.saving, 160)  # 2 * 160 - 160
        self.assertEqual(len(plan.stops), 6)

    def test_c_over_twelve_hours_is_rejected(self):
        long = combine.Roads(make_date(["A", "B"], {("A", "B"): 500}))
        cars = [cand("R1", "A", "B"), cand("R2", "B", "A")]
        self.assertIsNone(combine.best_plan(cars, self.now, roads=long))
        groups = combine.group(cars, self.now, roads=long)
        self.assertEqual([[c["orderId"] for c in g] for g in groups], [["R1"], ["R2"]])

    def test_c_twelve_hours_exactly_is_accepted(self):
        edge = combine.Roads(make_date(["A", "B", "C"], {("A", "B"): 350, ("B", "C"): 370}))
        ok = [cand("R1", "A", "B"), cand("R2", "B", "C")]
        plan = combine.best_plan(ok, self.now, roads=edge)
        self.assertEqual(plan.drive, 720)
        self.assertEqual(len(combine.group(ok, self.now, roads=edge)), 1)

        over = combine.Roads(make_date(["A", "B", "D"], {("A", "B"): 350, ("B", "D"): 371}))
        self.assertIsNone(combine.best_plan(ok[:1] + [cand("R2", "B", "D")], self.now, roads=over))

    def test_c_drive_cap_is_a_parameter(self):
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "X")]
        self.assertEqual(len(combine.group(cars, self.now, roads=self.roads)), 1)
        tight = combine.group(cars, self.now, max_drive_minutes=100, roads=self.roads)
        self.assertEqual(len(tight), 2)

    def test_d_window_conflict_is_rejected(self):
        """The second car cannot be reached inside its pickup window after the first delivery."""
        cars = [cand("R1", "X", "Y", pickup=(540, 560)),
                cand("R2", "Y", "Z", pickup=(600, 620))]
        far = combine.Roads(make_date(["X", "Y", "Z"], {("X", "Y"): 100, ("Y", "Z"): 400}))
        # First car collected at 540, delivered 640; 400 more minutes arrives at 1040.
        self.assertIsNone(combine.best_plan(cars, self.now, roads=far))
        self.assertEqual(len(combine.group(cars, self.now, roads=far)), 2)

    def test_d_window_conflict_between_deliveries(self):
        """Either order delivers one of the two cars after its deadline."""
        cars = [cand("R1", "X", "Y", delivery=(NOW, 560)),
                cand("R2", "Y", "X", pickup=(NOW, 520), delivery=(NOW, 560))]
        self.assertTrue(combine.windows_overlap(cars))  # the pickups do overlap
        self.assertIsNone(combine.best_plan(cars, self.now, roads=self.roads))
        self.assertEqual(len(combine.group(cars, self.now, roads=self.roads)), 2)

    def test_d_disjoint_pickup_windows_do_not_group(self):
        cars = [cand("R1", "X", "Y", pickup=(480, 600)),
                cand("R2", "Y", "X", pickup=(700, 900))]
        self.assertFalse(combine.windows_overlap(cars))
        self.assertEqual(len(combine.group(cars, self.now, roads=self.roads)), 2)
        self.assertTrue(combine.windows_overlap([cand("R1", "X", "Y", pickup=(480, 900)),
                                                 cand("R2", "Y", "X", pickup=(700, 1200))]))

    def test_a_single_car_saves_nothing(self):
        plan = combine.group_plan([cand("R1", "X", "Y")], self.now, roads=self.roads)
        self.assertEqual(plan.saving, 0)  # the baseline: one car, one driver
        self.assertEqual(plan.drive, 60)
        self.assertEqual(len(combine.group([cand("R1", "X", "Y")], self.now, roads=self.roads)), 1)

    def test_waiting_at_a_pickup_is_allowed(self):
        cars = [cand("R1", "X", "Y", pickup=(600, 900))]
        plan = combine.group_plan(cars, self.now, roads=self.roads)
        self.assertEqual(plan.stops[0][4], 600)  # never before the window opens
        self.assertEqual(plan.stops[1][4], 660)

    def test_plates_and_charging_cost_time_but_not_driving(self):
        plain = cand("R1", "X", "Y", delivery=(480, 600))
        self.assertEqual(combine.group_plan([plain], self.now, roads=self.roads).drive, 60)
        car = cand("R1", "X", "Y", delivery=(480, 600))
        car["withPlates"] = True
        car["withCharging"] = True
        # 30 + 40 minutes at the pickup push the delivery past 600, but they are not driving.
        self.assertIsNone(combine.group_plan([car], self.now, roads=self.roads))
        roomy = cand("R1", "X", "Y", delivery=(480, 700))
        roomy["withPlates"] = True
        roomy["withCharging"] = True
        plan = combine.group_plan([roomy], self.now, roads=self.roads)
        self.assertAlmostEqual(plan.drive, 60)
        self.assertEqual(plan.stops[1][4], 610)

    def test_e_deterministic_and_order_independent(self):
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "X"), cand("R3", "X", "Z"),
                cand("R4", "Z", "Y"), cand("R5", "Y", "Z", pickup=(900, 1200))]
        first = combine.group(cars, self.now, roads=self.roads)
        again = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual([[ids(g) for g in groups] for groups in (first, again)],
                         [[ids(g) for g in first], [ids(g) for g in first]])
        baseline = [ids(g) for g in first]
        rng = random.Random(7)
        for _ in range(5):
            shuffled = list(cars)
            rng.shuffle(shuffled)
            self.assertEqual([ids(g) for g in combine.group(shuffled, self.now, roads=self.roads)],
                             baseline)

    def test_each_candidate_appears_exactly_once(self):
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "X"), cand("R3", "X", "Y", leg=1),
                cand("R4", "Z", "X")]
        groups = combine.group(cars, self.now, roads=self.roads)
        seen = [id(c) for g in groups for c in g]
        self.assertEqual(sorted(seen), sorted(id(c) for c in cars))
        self.assertEqual(len(seen), len(set(seen)))

    def test_max_cars_is_respected(self):
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "Z"), cand("R3", "Z", "X"),
                cand("R4", "X", "Z")]
        for size in (2, 3):
            for gig in combine.group(cars, self.now, max_cars=size, roads=self.roads):
                self.assertLessEqual(len(gig), size)

    def test_stops_carry_the_kit_shape(self):
        cars = [cand("R1", "X", "Y"), cand("R2", "Y", "X")]
        gig = combine.group(cars, self.now, roads=self.roads)[0]
        stops = combine.gig_stops(gig)
        self.assertEqual([(s["orderId"], s["leg"], s["kind"]) for s in stops],
                         [("R1", 0, "pickup"), ("R1", 0, "delivery"),
                          ("R2", 0, "pickup"), ("R2", 0, "delivery")])
        self.assertNotIn("window", stops[0])
        self.assertIn("window", combine.gig_stops(gig, with_windows=True)[0])


class RealMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roads = combine.load_map(MAP_PATH)
        cls.now = NOW

    def test_road_helpers(self):
        self.assertEqual(combine.road_minutes(self.roads, "helsinki", "tampere"), 106)
        self.assertEqual(combine.road_minutes(self.roads, "tampere", "helsinki"), 106)
        self.assertAlmostEqual(combine.road_km(self.roads, "helsinki", "tampere"), 172.4)
        # The map's own place-to-itself road is a 25-minute drive across town, not zero.
        self.assertEqual(combine.road_minutes(self.roads, "tampere", "tampere"), 25)
        with self.assertRaises(KeyError):
            combine.road_minutes(self.roads, "helsinki", "no-such-place")

    def test_round_trip_on_a_real_lane_combines(self):
        cars = [cand("R1", "helsinki", "tampere"), cand("R2", "tampere", "helsinki")]
        plan = combine.best_plan(cars, self.now, roads=self.roads)
        self.assertEqual(plan.drive, 237)  # 106 + 25 across Tampere + 106
        self.assertAlmostEqual(plan.saving, 2 * 212 - 237)
        self.assertLessEqual(plan.drive, combine.MAX_DRIVE_MINUTES)
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual([sorted(ids(g)) for g in groups], [["R1.0", "R2.0"]])

    def test_real_lanes_over_twelve_hours_are_rejected(self):
        cars = [cand("R1", "rovaniemi", "helsinki"), cand("R2", "helsinki", "oulu")]
        self.assertIsNone(combine.best_plan(cars, self.now, roads=self.roads))
        self.assertEqual(len(combine.group(cars, self.now, roads=self.roads)), 2)

    def test_three_real_lanes_chain(self):
        cars = [cand("R1", "helsinki", "lahti"), cand("R2", "lahti", "kouvola"),
                cand("R3", "kouvola", "kotka")]
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual(len(groups), 1)
        plan = combine.group_plan(groups[0], self.now, roads=self.roads)
        self.assertEqual(ids(groups[0]), ids(plan.order))
        self.assertLessEqual(plan.drive, combine.MAX_DRIVE_MINUTES)
        self.assertGreater(plan.saving, combine.MIN_SAVING_MINUTES)

    def test_real_requests_from_the_study(self):
        with open(STUDY_PATH, encoding="utf-8") as handle:
            requests = json.load(handle)["requests"][:20]
        cars = combine.candidates_from_requests(requests)
        groups = combine.group(cars, self.now, roads=self.roads)
        self.assertEqual(sorted(id(c) for g in groups for c in g), sorted(id(c) for c in cars))
        for gig in groups:
            if len(gig) == 1:
                continue
            plan = combine.group_plan(gig, self.now, roads=self.roads)
            self.assertIsNotNone(plan)
            self.assertGreater(plan.saving, combine.MIN_SAVING_MINUTES)
            self.assertLessEqual(plan.drive, combine.MAX_DRIVE_MINUTES)


if __name__ == "__main__":
    unittest.main(verbosity=2)