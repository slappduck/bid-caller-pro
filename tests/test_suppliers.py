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
        self._p = [
            patch.object(ls, "_geo_from_city", return_value={"lat": HERE[0], "lon": HERE[1],
                                                             "city": "Springfield", "state": "MO"}),
            patch.object(ls, "_overpass", side_effect=lambda q: (self.overpass_calls.append(q), self.elements)[1]),
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

    def test_a_failed_map_read_is_not_cached(self):
        self.elements = None
        ls._suppliers_near("Springfield", "MO")
        self.elements = [el("Ozark Ready Mix")]
        self.assertEqual(len(ls._suppliers_near("Springfield", "MO")["plants"]), 1)

    def test_an_old_cache_is_read_again(self):
        ls._suppliers_near("Springfield", "MO")
        for v in self.store.values():
            v["at"] = time.time() - 40 * 86400
        ls._suppliers_near("Springfield", "MO")
        self.assertEqual(len(self.overpass_calls), 2)


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
