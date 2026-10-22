"""Ready-mix plants near a job, and area prices only once they can't point at anyone.

The map, the web search, Supabase and the store are stubbed; what's checked
is which places count as a plant, that a quiet map falls back to the web,
that a month's cache is used, and that a price range is shown only once there
are enough quotes from enough contractors.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

HERE = (37.209, -93.292)   # Springfield, MO


def el(name, lat=37.25, lon=-93.30, **tags):
    return {"type": "node", "lat": lat, "lon": lon, "tags": {"name": name, **tags}}


class PlantTests(unittest.TestCase):
    def rows(self, *els):
        return ls._supplier_rows(list(els), *HERE)

    def test_a_ready_mix_name_or_a_concrete_works_counts(self):
        got = self.rows(el("Ozark Ready Mix", phone="417-555-0100", website="ozarkreadymix.com",
                           **{"addr:housenumber": "12", "addr:street": "Kearney St", "addr:city": "Springfield"}),
                        el("Plant 7", man_made="works", product="concrete"))
        self.assertEqual([r["name"] for r in got], ["Ozark Ready Mix", "Plant 7"])
        self.assertEqual(got[0]["website"], "https://ozarkreadymix.com")
        self.assertEqual(got[0]["address"], "12 Kearney St, Springfield")

    def test_concrete_things_that_are_not_ready_mix_are_not_plants(self):
        self.assertEqual(self.rows(el("ClearCreek Concrete Siding", man_made="works", product="concrete siding"),
                                   el("Ozark Precast Ready Mix"), el("Midwest Concrete Block")), [])

    def test_a_concrete_contractor_is_not_a_plant(self):
        self.assertEqual(self.rows(el("Smith Concrete Construction"), el("Ready Mix Concrete Pumping"),
                                   el("Joe's Concrete Inc")), [])

    def test_nearest_first_and_too_far_left_out(self):
        got = self.rows(el("Far Redi-Mix", lat=38.5), el("Near Ready-Mix", lat=37.21),
                        el("Mid Readymix", lat=37.6))
        self.assertEqual([r["name"] for r in got], ["Near Ready-Mix", "Mid Readymix"])

    def test_one_plant_mapped_twice_is_listed_once(self):
        a = el("Ozark Ready Mix")
        b = {"type": "way", "center": {"lat": 37.2502, "lon": -93.3001}, "tags": {"name": "Ozark Ready Mix"}}
        self.assertEqual(len(self.rows(a, b)), 1)


class NearbyTests(unittest.TestCase):
    def setUp(self):
        self.store, self.overpass_calls = {}, []
        self.elements = [el("Ozark Ready Mix"), el("Springfield Redi-Mix", lat=37.3)]
        self.osm, self.ai, self.ai_asked, self.job_geo = [], [], [], None
        self._p = [
            patch.object(ls, "_geo_from_city", return_value={"lat": HERE[0], "lon": HERE[1],
                                                             "city": "Springfield", "state": "MO"}),
            patch.object(ls, "_overpass", side_effect=lambda q, deadline=None: (self.overpass_calls.append(q), self.elements)[1]),
            patch.object(ls, "_nominatim_plants", side_effect=lambda lat, lon: list(self.osm)),
            patch.object(ls, "_ai_plants", side_effect=lambda place, st, lat, lon, dl: (self.ai_asked.append(place), list(self.ai))[1]),
            patch.object(ls, "_nominatim_place", side_effect=lambda place, st: self.job_geo),
            patch.object(ls, "_brave_search", return_value=[
                {"url": "https://www.yelp.com/x", "content": "list"},
                {"url": "https://www.citymix.com/", "content": "Ready mix delivered"}]),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_plants_come_back_and_are_cached_for_the_area(self):
        self.elements.append(el("Third Ready Mix", lat=37.1))
        got = ls._suppliers_near("Springfield", "MO")
        self.assertEqual([p["name"] for p in got["plants"]],
                         ["Ozark Ready Mix", "Springfield Redi-Mix", "Third Ready Mix"])
        self.assertEqual(got["web"], [])          # three plants: no web search
        ls._suppliers_near("Springfield", "MO")
        self.assertEqual(len(self.overpass_calls), 1)

    def test_a_quiet_map_falls_back_to_the_web_without_directories(self):
        self.elements = []
        got = ls._suppliers_near("Springfield", "MO")
        self.assertEqual([w["site"] for w in got["web"]], ["citymix.com"])

    def test_a_failed_map_read_is_tried_again_in_hours_not_a_month(self):
        self.elements = None
        ls._suppliers_near("Springfield", "MO")
        self.elements = [el("Ozark Ready Mix")]
        self.assertEqual(len(ls._suppliers_near("Springfield", "MO")["plants"]), 0)   # within the hours
        for v in self.store.values():
            v["at"] = time.time() - 7 * 3600
        self.assertEqual(len(ls._suppliers_near("Springfield", "MO")["plants"]), 1)

    def test_when_the_map_is_down_the_other_sources_still_answer(self):
        self.elements = None
        self.osm = [{"name": "Rost Ready Mix", "lat": 37.25, "lon": -93.1, "miles": 11.0, "phone": "", "website": "",
                     "email": "", "address": "Rogersville, Missouri", "source": "OpenStreetMap"}]
        self.ai = [{"name": "Rost Ready-Mix", "lat": None, "lon": None, "miles": None, "phone": "417-555-0142",
                    "website": "https://rostreadymix.example", "email": "", "address": "Rogersville, MO", "source": "web"},
                   {"name": "Ozark Mountain Concrete", "lat": 37.3, "lon": -93.4, "miles": 8.0, "phone": "417-555-0199",
                    "website": "", "email": "", "address": "Springfield, MO", "source": "web"}]
        got = ls._suppliers_near("Springfield", "MO")
        names = [p["name"] for p in got["plants"]]
        self.assertEqual(names, ["Ozark Mountain Concrete", "Rost Ready Mix"])     # the same plant once
        rost = got["plants"][1]
        self.assertEqual((rost["phone"], rost["website"]), ("417-555-0142", "https://rostreadymix.example"))

    def test_the_web_search_is_only_asked_when_the_map_knows_few(self):
        self.elements += [el("Third Ready Mix", lat=37.1), el("Fourth Redi Mix", lat=37.15)]
        ls._suppliers_near("Springfield", "MO")
        self.ai = [{"name": "Should Not Show", "lat": None, "lon": None, "miles": None, "phone": "4175550100",
                    "website": "https://x.example", "email": "", "address": "", "source": "web"}]
        self.assertNotIn("Should Not Show", [p["name"] for p in ls._suppliers_near("Springfield", "MO")["plants"]])

    def test_plants_are_searched_around_the_job_site_when_it_places(self):
        self.job_geo = (37.60, -93.30)          # 27 miles north of town
        got = ls._suppliers_near("Springfield", "MO", near="Elm St from 1st St to 5th St")
        self.assertTrue(got["near_job"])
        self.assertEqual((got["lat"], got["lon"]), (37.60, -93.30))
        self.assertIn("37.60000,-93.30000", self.overpass_calls[0])

    def test_a_job_address_that_lands_far_off_is_ignored(self):
        self.job_geo = (40.0, -90.0)            # another state's Elm St
        got = ls._suppliers_near("Springfield", "MO", near="Elm St")
        self.assertFalse(got["near_job"])
        self.assertEqual(got["lat"], HERE[0])

    def test_an_old_cache_is_read_again(self):
        ls._suppliers_near("Springfield", "MO")
        for v in self.store.values():
            v["at"] = time.time() - 40 * 86400
        ls._suppliers_near("Springfield", "MO")
        self.assertEqual(len(self.overpass_calls), 2)


class JobLocationTests(unittest.TestCase):
    """A statewide bid is placed by the site its own name gives."""
    EDMOND = (35.66, -97.46)

    def setUp(self):
        self.store, self.asked = {}, []
        self._p = [
            patch.object(ls, "OPENAI_API_KEY", "k"),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_geo_from_city", side_effect=lambda c, st: {"lat": 35.65, "lon": -97.48, "city": c, "state": st}
                         if c in ("Edmond", "Aurora") else None),
            patch.object(ls, "_nominatim_place", side_effect=lambda place, st: (self.asked.append(place), {
                "Kickingbird Golf Course, Edmond": self.EDMOND, "Elm St, Aurora": (35.651, -97.481)}.get(place))[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def model_says(self, found):
        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return ls.json.dumps({"choices": [{"message": {"content": ls.json.dumps(found)}}]}).encode()
        return patch.object(ls.urllib.request, "urlopen", return_value=Resp())

    def test_a_statewide_bid_is_placed_at_the_site_its_name_gives(self):
        with self.model_says({"place": "Kickingbird Golf Course", "city": "Edmond", "state": "OK"}):
            got = ls._job_location("", "OK", title="Kickingbird Golf Course Parking Lot Bid",
                                   url="https://www.edmondok.gov/Bids.aspx?bidID=88")
        self.assertEqual(got, {"lat": 35.66, "lon": -97.46, "label": "Kickingbird Golf Course, Edmond, OK", "how": "bid"})

    def test_the_answer_is_kept_so_the_bid_is_only_read_once(self):
        with self.model_says({"place": "Kickingbird Golf Course", "city": "Edmond"}):
            ls._job_location("", "OK", title="Kickingbird Golf Course Parking Lot Bid")
        with patch.object(ls.urllib.request, "urlopen", side_effect=AssertionError("asked twice")):
            self.assertEqual(ls._job_location("", "OK", title="Kickingbird Golf Course Parking Lot Bid")["how"], "bid")

    def test_only_the_town_known_places_the_job_in_that_town(self):
        with self.model_says({"place": "", "city": "Edmond", "state": "OK"}):
            got = ls._job_location("", "OK", title="Parking lot overlay", url="https://edmondok.gov/x")
        self.assertEqual((got["label"], got["how"]), ("Edmond, OK", "bid"))

    def test_another_state_from_the_model_is_not_used(self):
        with self.model_says({"place": "Kickingbird Golf Course", "city": "Edmond", "state": "TX"}):
            self.assertIsNone(ls._job_location("", "OK", title="Kickingbird Golf Course Parking Lot Bid"))

    def test_an_address_the_contractor_gave_comes_first(self):
        with patch.object(ls.urllib.request, "urlopen", side_effect=AssertionError("model asked")):
            got = ls._job_location("Aurora", "OK", near="Elm St", title="Sidewalks")
        self.assertEqual((got["label"], got["how"]), ("Elm St, Aurora", "job"))

    def test_a_whole_state_is_not_a_town(self):
        self.assertEqual(ls._supplier_where({"city": "Oklahoma", "state": ""}), ("", "OK"))
        self.assertEqual(ls._supplier_where({"city": "Edmond", "state": "ok"}), ("Edmond", "OK"))
        self.assertEqual(ls._supplier_where({"city": "Edmond", "state": "Nowhere"}), ("Edmond", ""))

    def test_the_locate_endpoint_answers_for_a_statewide_bid(self):
        with patch.object(ls, "_license_is_active", return_value=True), patch.object(ls, "_ip_rate_ok", return_value=True), \
             self.model_says({"place": "Kickingbird Golf Course", "city": "Edmond"}):
            d = ls.app.test_client().post("/bid/locate", json={"city": "Oklahoma", "title": "Kickingbird Golf Course Parking Lot Bid"}).get_json()
        self.assertEqual((d["ok"], d["how"], d["label"]), (True, "bid", "Kickingbird Golf Course, Edmond, OK"))


class WebPlantTests(unittest.TestCase):
    """The web search's answer is checked, not trusted."""

    def run_ai(self, text):
        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return ls.json.dumps({"output": [{"type": "web_search_call"}, {"type": "message", "content": [
                {"type": "output_text", "text": text}]}]}).encode()
        with patch.object(ls, "OPENAI_API_KEY", "k"), patch.object(ls.urllib.request, "urlopen", return_value=Resp()), \
             patch.object(ls, "_zippopotam_city", side_effect=lambda c, st: {"Rogersville": (37.117, -93.055),
                                                                            "Chicago": (41.88, -87.63)}.get(c)):
            return ls._ai_plants("Springfield, MO", "MO", *HERE, time.time() + 60)

    def test_only_reachable_plants_inside_the_radius_come_back(self):
        got = self.run_ai('Here you go: [{"name": "Rost Ready Mix", "city": "Rogersville", "state": "MO", '
                          '"phone": "(417) 753-2101", "website": "https://rostreadymix.example"},'
                          '{"name": "Windy City Ready Mix", "city": "Chicago", "state": "IL", "website": "https://w.example"},'
                          '{"name": "No Way To Reach", "city": "Rogersville", "state": "MO"},'
                          '{"name": "Smith Concrete Construction", "city": "Rogersville", "phone": "4175550100"}]')
        self.assertEqual([r["name"] for r in got], ["Rost Ready Mix"])
        self.assertEqual(got[0]["miles"], 14.5)
        self.assertEqual(got[0]["source"], "web")

    def test_an_answer_that_is_not_a_list_gives_nothing(self):
        self.assertEqual(self.run_ai("I couldn't find any."), [])


class AreaQuoteTests(unittest.TestCase):
    def quotes(self, rows):
        with patch.object(ls, "_supabase_admin_request", return_value=rows):
            return ls._area_quotes(*HERE)

    def test_too_few_quotes_or_contractors_shows_no_range(self):
        two = [{"user_id": "a", "lat": 37.2, "lon": -93.3, "price_per_cy": 140}] * 2
        self.assertEqual(self.quotes(two), {"enough": False, "count": 2})
        one_user = [{"user_id": "a", "lat": 37.2, "lon": -93.3, "price_per_cy": p} for p in (130, 140, 150)]
        self.assertFalse(self.quotes(one_user)["enough"])

    def test_enough_quotes_give_a_range_and_median(self):
        rows = [{"user_id": u, "lat": 37.2, "lon": -93.3, "price_per_cy": p}
                for u, p in (("a", 130), ("b", 150), ("a", 142), ("c", 900 / 6))]
        rows.append({"user_id": "d", "lat": 40.0, "lon": -93.3, "price_per_cy": 300})   # 190 mi away
        got = self.quotes(rows)
        self.assertEqual((got["count"], got["low"], got["high"], got["median"]), (4, 130.0, 150.0, 146.0))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def test_nearby_needs_a_license(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.client.post("/suppliers/nearby", json={"city": "Springfield", "state": "MO"}).status_code, 403)

    def test_nearby_returns_plants_and_quotes(self):
        with patch.object(ls, "_license_is_active", return_value=True), \
             patch.object(ls, "_ip_rate_ok", return_value=True), \
             patch.object(ls, "_suppliers_near", return_value={"lat": 1, "lon": 2, "plants": [{"name": "X"}], "web": []}), \
             patch.object(ls, "_area_quotes", return_value={"enough": False, "count": 1}):
            d = self.client.post("/suppliers/nearby", json={"city": "Springfield", "state": "mo"}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual((d["state"], d["plants"][0]["name"], d["quotes"]["count"]), ("MO", "X", 1))

    def test_a_quote_needs_a_signed_in_user(self):
        with patch.object(ls, "_supabase_user", return_value=None):
            r = self.client.post("/suppliers/quote", json={"city": "Springfield", "state": "MO",
                                                          "price_per_cy": 140, "yards": 30})
        self.assertEqual(r.status_code, 403)

    def test_a_total_typed_as_a_per_yard_price_is_refused(self):
        with patch.object(ls, "_supabase_user", return_value={"id": "u1"}):
            r = self.client.post("/suppliers/quote", json={"city": "Springfield", "state": "MO",
                                                          "price_per_cy": 4200, "yards": 30})
        self.assertEqual(r.status_code, 400)

    def test_a_quote_is_stored_with_a_rounded_place(self):
        saved = []
        with patch.object(ls, "_supabase_user", return_value={"id": "u1"}), \
             patch.object(ls, "_ip_rate_ok", return_value=True), \
             patch.object(ls, "_geo_from_city", return_value={"lat": 37.2091, "lon": -93.2923, "city": "Springfield"}), \
             patch.object(ls, "_supabase_admin_request", side_effect=lambda path, method="GET", data=None: (saved.append(data), True)[1]):
            d = self.client.post("/suppliers/quote", json={"city": "Springfield", "state": "MO", "price_per_cy": "142.5",
                                                          "yards": 30, "delivery_fee": 95, "psi": 4000}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(saved[0], {"user_id": "u1", "lat": 37.2, "lon": -93.3, "state": "MO", "price_per_cy": 142.5,
                                    "yards": 30.0, "delivery_fee": 95.0, "short_load_fee": None, "psi": 4000})


if __name__ == "__main__":
    unittest.main()
