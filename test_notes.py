"""Unit tests for notes.py, the dealer-note reader.

Run with:  .venv/bin/python -m pytest test_notes.py -q
or:        .venv/bin/python -m unittest test_notes -v
"""

from __future__ import annotations

import json
import pathlib
import unittest

import notes


DAY = 1440


def at(day: int, hour: int, minute: int = 0) -> int:
    return day * DAY + hour * 60 + minute


def request(arrived: int = at(0, 8), pickup_from: int | None = None,
            options: tuple[int, ...] = (at(0, 20),)) -> dict:
    """A minimal price request: arrival, pickup window and option deadlines."""
    start = arrived if pickup_from is None else pickup_from
    return {
        "arrivedAt": arrived,
        "legs": [{
            "pickup": {"window": {"from": start, "to": at(6, 20)}},
            "delivery": {"window": {"from": start, "to": at(6, 20)}},
        }],
        "options": [{"kind": "same_day", "deadline": d} for d in options],
    }


class DayWords(unittest.TestCase):
    def test_today_fi_sv_en(self):
        r = request(at(0, 8))
        self.assertEqual(notes.parse_note(
            "Noudettavissa tänään klo 9 alkaen", "fi", r)["pickup_after"],
            at(0, 9))
        self.assertEqual(notes.parse_note(
            "Kan hämtas tidigast idag kl 9", "sv", r)["pickup_after"],
            at(0, 9))
        self.assertEqual(notes.parse_note(
            "Ready for pickup today from 9:00", "en", r)["ready_at"],
            at(0, 9))

    def test_tomorrow_fi_sv_en_and_short_forms(self):
        r = request(at(0, 8))
        for text in ("Nouto huomenna klo 9 alkaen",
                     "Hämtning i morgon kl 9 tidigast",
                     "Collect tomorrow from 9:00",
                     "Pickup tmrw 9:00 from",
                     "Hämtning imorgon kl 9 tidigast"):
            lang = "sv" if "Hämt" in text else ("fi" if "Nouto" in text else "en")
            self.assertEqual(notes.parse_note(text, lang, r)["pickup_after"],
                             at(1, 9), text)

    def test_day_after_tomorrow_fi_sv_en(self):
        r = request(at(0, 8))
        cases = (
            ("Noudettavissa ylihuomenna klo 9 alkaen", "fi", 9),
            ("Kan hämtas tidigast i övermorgon 10:00", "sv", 10),
            ("Ready for pickup the day after tomorrow from 10:00", "en", 10),
        )
        for text, lang, hour in cases:
            out = notes.parse_note(text, lang, r)
            self.assertEqual(out["pickup_after"], at(2, hour), text)

    def test_weekday_two_to_six_days_after_arrival(self):
        # Monday: Thursday is three days away.
        r = request(at(0, 8))
        self.assertEqual(notes.parse_note(
            "Perillä to. klo 15", "fi", r)["delivery_before"], at(3, 15))
        self.assertEqual(notes.parse_note(
            "deliver by Thurs 15:00", "en", r)["deadline_override"],
            at(3, 15))
        self.assertEqual(notes.parse_note(
            "perillä keskiviikkona klo 13", "fi", r)["delivery_before"],
            at(2, 13))
        self.assertEqual(notes.parse_note(
            "Perillä Thursday 15:00", "en", r)["delivery_before"],
            at(3, 15))
        self.assertEqual(notes.parse_note(
            "Framme tors kl 13", "sv", r)["delivery_before"], at(3, 13))
        self.assertEqual(notes.parse_note(
            "Nouto ke klo 10 alkaen", "fi", r)["pickup_after"], at(2, 10))

    def test_weekday_never_arrival_or_next_day(self):
        # Friday + "fri" cannot be today or tomorrow: it is the next Friday.
        r = request(at(4, 8))
        self.assertEqual(notes.parse_note(
            "Perillä perjantaina klo 12", "fi", r)["delivery_before"],
            at(11, 12))

    def test_weekday_range_is_not_a_single_day(self):
        r = request(at(0, 8))
        out = notes.parse_note("Nouto ma-pe 8-16", "fi", r)
        self.assertNotIn("delivery_before", out)
        self.assertEqual(out["pickup_before"], at(0, 16))


class ClockTimes(unittest.TestCase):
    def test_digits_are_24_hour(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "Perillä klo 15", "fi", r)["delivery_before"], at(0, 15))
        self.assertEqual(notes.parse_note(
            "klo 2000 mennessä perillä", "fi", r)["delivery_before"],
            at(0, 20))
        self.assertEqual(notes.parse_note(
            "perillä 2000 mennessä", "fi", r)["delivery_before"],
            at(0, 20))

    def test_am_pm(self):
        r = request()
        out = notes.parse_note(
            "Must be at destination by 3 pm", "en", r)
        self.assertEqual(out["delivery_before"], at(0, 15))
        self.assertEqual(out["deadline_override"], at(0, 15))
        self.assertEqual(notes.parse_note(
            "collect before 5 pm", "en", r)["pickup_before"], at(0, 17))
        self.assertEqual(notes.parse_note(
            "Perillä 3pm mennessä", "fi", r)["delivery_before"], at(0, 15))

    def test_klo_with_am_pm(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "Perillä klo 5 pm mennessä", "fi", r)["delivery_before"],
            at(0, 17))

    def test_half_with_a_digit(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "Perillä half 3", "en", r)["delivery_before"], at(0, 14, 30))

    def test_hour_in_words_is_a_working_hour(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "Perillä klo two", "en", r)["delivery_before"], at(0, 14))
        self.assertEqual(notes.parse_note(
            "Perillä klo kaksi", "fi", r)["delivery_before"], at(0, 14))
        self.assertEqual(notes.parse_note(
            "Framme kl tre", "sv", r)["delivery_before"], at(0, 15))

    def test_half_three(self):
        r = request()
        for text, lang in (("Perillä half three", "en"),
                           ("Perillä klo puoli kolme", "fi"),
                           ("Framme halv tre", "sv")):
            self.assertEqual(notes.parse_note(text, lang, r)["delivery_before"],
                             at(0, 14, 30), text)
        self.assertEqual(notes.parse_note(
            "Nouto puoli kolmeen asti", "fi", r)["pickup_before"],
            at(0, 14, 30))


class DailyHours(unittest.TestCase):
    def test_a_closing_hour_after_arrival_moves_to_the_next_day(self):
        r = request(at(0, 19))
        self.assertEqual(notes.parse_note(
            "Collect before 18:00", "en", r)["pickup_before"], at(1, 18))

    def test_a_delivery_closing_after_arrival_moves_to_the_next_day(self):
        r = request(at(0, 19), options=(at(1, 20),))
        self.assertEqual(notes.parse_note(
            "Vastaanotto klo 16 asti", "fi", r)["delivery_before"],
            at(1, 16))


class CorrectionsAndDenials(unittest.TestCase):
    def test_correction_of_weekday(self):
        r = request(at(0, 8))
        out = notes.parse_note(
            "in the workshop, ready thu 9:00. correction: fri.", "en", r)
        self.assertEqual(out["ready_at"], at(4, 9))
        self.assertEqual(out["pickup_after"], at(4, 9))

    def test_correction_of_time(self):
        r = request(at(0, 8))
        self.assertEqual(notes.parse_note(
            "pesu kesken, haettavissa huomenna 10 jälkeen, eikun 9.00",
            "fi", r)["pickup_after"], at(1, 9))
        self.assertEqual(notes.parse_note(
            "Noudettavissa aikaisintaan huomenna klo 8, ei kun klo 7",
            "fi", r)["pickup_after"], at(1, 7))
        self.assertEqual(notes.parse_note(
            "collect before 21:00, sorry, i mean 20:00", "en", r)
            ["pickup_before"], at(0, 20))

    def test_denied_time_does_not_count(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "We close at 14:00 (not 13:00).", "en", r)["pickup_before"],
            at(0, 14))
        self.assertEqual(notes.parse_note(
            "pitää olla perillä huomenna klo 2000 mennessä, ei klo 2100",
            "fi", r)["delivery_before"], at(1, 20))
        self.assertEqual(notes.parse_note(
            "Must be at destination by 3pm (not 4pm)", "en", r)
            ["delivery_before"], at(0, 15))

    def test_not_before_is_a_lower_bound_not_a_denial(self):
        r = request()
        out = notes.parse_note(
            "Maksun jälkeen, ei ennen klo 13.30", "fi", r)
        self.assertEqual(out["release_at"], at(0, 13, 30))
        self.assertEqual(notes.parse_note(
            "Not ready before 14:00", "en", r)["ready_at"], at(0, 14))


class Directions(unittest.TestCase):
    def test_pickup_keywords(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "Collect before 18:00.", "en", r)["pickup_before"], at(0, 18))
        self.assertEqual(notes.parse_note(
            "Haettava ennen klo 14", "fi", r)["pickup_before"], at(0, 14))
        self.assertEqual(notes.parse_note(
            "Hämtning efter kl 12", "sv", r)["pickup_after"], at(0, 12))
        self.assertEqual(notes.parse_note(
            "Ollaan täällä 15.30 saakka", "fi", r)["pickup_before"],
            at(0, 15, 30))

    def test_delivery_keywords(self):
        r = request()
        out = notes.parse_note(
            "Toimitus perille viim. huomenna klo 18", "fi", r)
        self.assertEqual(out["delivery_before"], at(1, 18))
        self.assertEqual(out["deadline_override"], at(1, 18))
        self.assertEqual(notes.parse_note(
            "Framme senast i morgon kl 13", "sv", r)["delivery_before"],
            at(1, 13))
        self.assertEqual(notes.parse_note(
            "Asiakas tulee hakemaan, perillä viim. klo 12", "fi", r)
            ["delivery_before"], at(0, 12))
        # A bare hour carried by a time word, with no "klo".
        self.assertEqual(notes.parse_note(
            "Haettava ennen 18.", "fi", r)["pickup_before"], at(0, 18))
        out = notes.parse_note(
            "Ska vara på plats senast i morgon 20", "sv", r)
        self.assertEqual(out["delivery_before"], at(1, 20))
        self.assertEqual(out["deadline_override"], at(1, 20))

    def test_closing_after_means_by_then(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "perillä liike kiinni klo 17 jälkeen", "fi", r)["delivery_before"],
            at(0, 17))
        self.assertEqual(notes.parse_note(
            "Perilllä ei ketään 18 jälkeen", "fi", r)["delivery_before"],
            at(0, 18))

    def test_we_close_is_the_sending_site(self):
        r = request()
        self.assertEqual(notes.parse_note(
            "we close at 17:00", "en", r)["pickup_before"], at(0, 17))
        self.assertEqual(notes.parse_note(
            "vi stänger kl 17", "sv", r)["pickup_before"], at(0, 17))


class OpeningHours(unittest.TestCase):
    def test_delivery_hours_tighten_the_deadline(self):
        r = request(options=(at(0, 20),))
        out = notes.parse_note("Vastaanotto arkisin 8-16", "fi", r)
        self.assertEqual(out["delivery_before"], at(0, 16))

    def test_wide_delivery_hours_do_not_tighten(self):
        r = request(options=(at(0, 20),))
        out = notes.parse_note("Vastaanotto arkisin 7.00 - 21.00", "fi", r)
        self.assertNotIn("delivery_before", out)

    def test_pickup_hours(self):
        r = request(pickup_from=at(0, 8))
        out = notes.parse_note("Nouto arkisin 7-16.30", "fi", r)
        self.assertEqual(out["pickup_before"], at(0, 16, 30))
        self.assertEqual(out["pickup_after"], at(0, 8))

    def test_generic_open_hours_are_the_dealers_own(self):
        r = request()
        out = notes.parse_note("open weekdays 9:00-16:30", "en", r)
        self.assertEqual(out["pickup_before"], at(0, 16, 30))
        self.assertEqual(out["pickup_after"], at(0, 9))

    def test_receiving_hours_are_the_destination(self):
        r = request(options=(at(1, 20),))
        out = notes.parse_note(
            "Receiving weekdays 10:00-18:00 only.", "en", r)
        self.assertEqual(out["delivery_before"], at(1, 18))

    def test_private_slot_combines_with_other_hours(self):
        r = request(at(0, 8))
        out = notes.parse_note(
            "Myyjä kotona klo 13-16. Nouto ennen 18.", "fi", r)
        self.assertTrue(out["private_slot"])
        self.assertEqual(out["pickup_after"], at(0, 13))
        self.assertEqual(out["pickup_before"], at(0, 16))

    def test_private_seller_slot(self):
        r = request(at(0, 8))
        for text, lang in (
                ("Myyjä kotona huomenna klo 7-10", "fi"),
                ("Seller home tomorrow 7:00-10:00", "en")):
            out = notes.parse_note(text, lang, r)
            self.assertTrue(out["private_slot"], text)
            self.assertEqual(out["pickup_after"], at(1, 7), text)
            self.assertEqual(out["pickup_before"], at(1, 10), text)


class FactsInOneNote(unittest.TestCase):
    def test_several_facts(self):
        r = request(at(0, 8))
        text = ("Valmis vasta huomenna klo 9. Nouto ennen klo 16. "
                "Perillä huomenna klo 18. Manuaali. "
                "Ei kilpiä, siirtokilvet mukaan. Kesärenkaat alla.")
        out = notes.parse_note(text, "fi", r)
        self.assertEqual(out["ready_at"], at(1, 9))
        self.assertEqual(out["pickup_before"], at(1, 16))
        self.assertEqual(out["delivery_before"], at(1, 18))
        self.assertTrue(out["manual"])
        self.assertTrue(out["plates"])
        self.assertTrue(out["summer_tyres"])

    def test_ready_later(self):
        r = request(at(0, 8))
        self.assertEqual(notes.parse_note(
            "Auto valmis vasta huomenna klo 11", "fi", r)["ready_at"],
            at(1, 11))
        self.assertEqual(notes.parse_note(
            "In the workshop, ready wed 14:00", "en", r)["ready_at"],
            at(2, 14))
        self.assertEqual(notes.parse_note(
            "inte klar förrän imorgon kl 9", "sv", r)["ready_at"],
            at(1, 9))

    def test_release_after_payment(self):
        r = request(at(0, 8))
        self.assertEqual(notes.parse_note(
            "Luovutetaan kun maksu näkyy, aikaisintaan huomenna klo 9.30",
            "fi", r)["release_at"], at(1, 9, 30))
        self.assertEqual(notes.parse_note(
            "Ostaja maksaa ensin, auton saa huomenna klo 11.00 alkaen",
            "fi", r)["release_at"], at(1, 11))
        self.assertEqual(notes.parse_note(
            "asiakas maksaa puoli neljältä, luovutus sen jälkeen",
            "fi", r)["release_at"], at(0, 15, 30))
        self.assertEqual(notes.parse_note(
            "kauppa kesken: luovutus vasta maksun jälkeen, klo 15.30 alkaen",
            "fi", r)["release_at"], at(0, 15, 30))


class FeatureWords(unittest.TestCase):
    def test_manual(self):
        r = request()
        for text, lang in (
                ("Manuaalivaihteinen", "fi"),
                ("Manuaali, ei automaatti", "fi"),
                ("Kuljettajalla oltava oikeus ajaa manuaalia.", "fi"),
                ("kuski tarvii manuaalikortin", "fi"),
                ("manual gearbox", "en"),
                ("It's a manual", "en"),
                ("Manuellt växlad.", "sv")):
            self.assertTrue(notes.parse_note(text, lang, r).get("manual"), text)

    def test_plates(self):
        r = request()
        for text, lang in (
                ("Auto ilman kilpiä", "fi"),
                ("kilvet poistettu, ota koekilvet", "fi"),
                ("Ei rekkareita, koekilvet tarvitaan", "fi"),
                ("Ei kilpiä, siirtokilvet mukaan", "fi"),
                ("No plates, bring transfer plates!", "en"),
                ("Saknar skyltar, ta med provskyltar", "sv")):
            self.assertTrue(notes.parse_note(text, lang, r).get("plates"), text)

    def test_summer_tyres_on_the_car(self):
        r = request()
        for text, lang in (
                ("Still on summer tyres, br, sales.", "en"),
                ("Kesärenkaat alla, ei talvirenkaita.", "fi"),
                ("Auto vielä kesärenkailla.", "fi"),
                ("har fortfarande sommardäck på", "sv")):
            self.assertTrue(
                notes.parse_note(text, lang, r).get("summer_tyres"), text)

    def test_spare_tyres_are_not_summer_tyres(self):
        r = request()
        for text, lang in (
                ("Summer tyres in the trunk, go with the car.", "en"),
                ("Kesärenkaat vanteineen takakontissa", "fi"),
                ("4 kesärengasta tavaratilassa", "fi"),
                ("Sommardäcken i bakluckan, följer med.", "sv")):
            self.assertNotIn("summer_tyres",
                             notes.parse_note(text, lang, r), text)

    def test_plain_notes_have_no_constraints(self):
        r = request()
        for text, lang in (
                ("Kiitos.", "fi"),
                ("Booking ref 236472. Thx", "en"),
                ("Renkaat kyydissä.", "fi"),
                ("Standard transfer, thanks.", "en")):
            self.assertEqual(notes.parse_note(text, lang, r), {}, text)


class OrderNotes(unittest.TestCase):
    def test_message_with_a_new_deadline_uses_the_event_day(self):
        r = request(at(0, 8))
        out = notes.parse_order_note(
            "Uusi määräaika: perillä huomenna klo 15.", "fi", r, at=at(0, 10))
        self.assertEqual(out["delivery_before"], at(1, 15))
        out = notes.parse_order_note(
            "deadline moved to Friday 18:00", "en", r, at=at(0, 10))
        self.assertEqual(out["deadline_override"], at(4, 18))

    def test_event_dict_with_a_message(self):
        event = {
            "kind": "order_changed", "at": at(0, 10),
            "message": "Uusi määräaika: perillä huomenna klo 15.",
            "order": {"legs": [{
                "pickup": {"window": {"from": at(0, 8), "to": at(6, 20)}},
                "delivery": {"window": {"from": at(0, 8), "to": at(6, 20)}},
            }]},
        }
        self.assertEqual(notes.parse_order_note(None, "fi", event)
                         ["delivery_before"], at(1, 15))

    def test_place_change_alone_is_empty(self):
        r = request()
        self.assertEqual(notes.parse_order_note(
            "Suunnitelma muuttui: auto toimitetaan kohteeseen Pori, ei Turku.",
            "fi", r, at=at(1, 9)), {})


class ApiShape(unittest.TestCase):
    def test_note_dict_and_request_dict(self):
        r = request(at(0, 8))
        out = notes.parse_note(
            {"text": "Nouto huomenna klo 9 alkaen", "language": "fi"}, "fi", r)
        self.assertEqual(out["pickup_after"], at(1, 9))

    def test_request_may_be_the_second_argument(self):
        r = request(at(0, 8))
        out = notes.parse_note("Nouto huomenna klo 9 alkaen", r)
        self.assertEqual(out["pickup_after"], at(1, 9))


class HandbookExamples(unittest.TestCase):
    def test_every_example_in_the_notes_table(self):
        r = request(at(0, 8))
        out = notes.parse_note("Nouto arkisin ennen klo 14.", "fi", r)
        self.assertEqual(out["pickup_before"], at(0, 14))
        out = notes.parse_note("Klar först på onsdag kl 12.", "sv", r)
        self.assertEqual(out["ready_at"], at(2, 12))
        out = notes.parse_note("Maksun jälkeen, ei ennen klo 13.30.", "fi", r)
        self.assertEqual(out["release_at"], at(0, 13, 30))
        out = notes.parse_note(
            "Must be at destination by tomorrow 17:00.", "en", r)
        self.assertEqual(out["deadline_override"], at(1, 17))
        out = notes.parse_note("Vastaanotto klo 16 asti.", "fi", r)
        self.assertEqual(out["delivery_before"], at(0, 16))
        self.assertTrue(notes.parse_note(
            "Manuaalivaihteisto.", "fi", r)["manual"])
        self.assertTrue(notes.parse_note(
            "Car has no plates.", "en", r)["plates"])
        self.assertTrue(notes.parse_note(
            "Auto vielä kesärenkailla.", "fi", r)["summer_tyres"])
        self.assertEqual(notes.parse_note(
            "Suunnitelma muuttui: auto toimitetaan kohteeseen Pori, ei Turku.",
            "fi", r), {})


class Study1(unittest.TestCase):
    """Every note of the recorded week parses, twice the same, without error."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("study1.json")
        data = json.loads(path.read_text())
        cls.rows = [r for r in data["requests"] if r.get("note")]

    def test_all_notes_parse_deterministically(self):
        self.assertEqual(len(self.rows), 450)
        for r in self.rows:
            note = r["note"]
            first = notes.parse_note(note["text"], note["language"], r)
            second = notes.parse_note(note["text"], note["language"], r)
            self.assertEqual(first, second, r["id"])
            for key, value in first.items():
                self.assertIn(key, notes._DOCUMENTED)
                self.assertIsInstance(value, (int, bool), (r["id"], key))

    def test_real_phrasings(self):
        rows = {r["id"]: r for r in self.rows}

        def parsed(rid):
            r = rows[rid]
            return notes.parse_note(r["note"]["text"], r["note"]["language"], r)

        # A correction with "eikun": 9.00 wins over 10.
        self.assertEqual(parsed("R00065")["pickup_after"], at(1, 9))
        # "ei klo 2100" denies 21:00; "klo 2000" stands.
        self.assertEqual(parsed("R00302")["delivery_before"], at(2, 20))
        # Two facts in two languages: Wednesday 10, and pickup before 18.
        self.assertEqual(parsed("R00132")["pickup_after"], at(2, 10))
        # A weekday short form "ke": Wednesday 10.
        self.assertEqual(parsed("R00133")["pickup_after"], at(2, 10))
        # A payment release, not a delivery.
        self.assertEqual(parsed("R00600")["release_at"], at(3, 14))
        # Delivery hours of the receiving shop.
        self.assertEqual(parsed("R00357")["delivery_before"], at(2, 15))
        # "perillä liike kiinni ... jälkeen" is a delivery closing.
        self.assertEqual(parsed("R00495")["delivery_before"], at(2, 17))
        # A private seller's slot.
        self.assertTrue(parsed("R00433")["private_slot"])
        self.assertEqual(parsed("R00433")["pickup_after"], at(2, 9))
        # Plates and summer tyres in the same week, in both languages.
        self.assertTrue(parsed("R00286")["plates"])
        self.assertTrue(parsed("R00691")["plates"])
        self.assertTrue(parsed("R00322")["summer_tyres"])
        self.assertTrue(parsed("R00324")["summer_tyres"])
        # A spare set of wheels is cargo, not a tyre restriction.
        self.assertNotIn("summer_tyres", parsed("R00227"))
        # A manual licence.
        self.assertTrue(parsed("R00350")["manual"])


if __name__ == "__main__":
    unittest.main(verbosity=2)