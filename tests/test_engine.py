import unittest

from complaint_core.engine import (
    SiteEvidence,
    WeatherSnapshot,
    correlate,
    haversine_km,
    jaccard,
    merge_params,
    normalize_text,
    shingles,
    text_fingerprint,
)


class EngineTest(unittest.TestCase):
    def test_normalize_text_keeps_cjk_and_digits(self):
        self.assertEqual("化工 刺鼻 3次", normalize_text("化工，刺鼻！3次"))

    def test_fingerprint_stable_and_similar_texts_overlap(self):
        fingerprint_a, pieces_a = text_fingerprint("夜里闻到很浓的化工刺鼻味道")
        fingerprint_b, pieces_b = text_fingerprint("夜里又闻到很浓的化工刺鼻味道")
        self.assertEqual(fingerprint_a, text_fingerprint("夜里闻到很浓的化工刺鼻味道")[0])
        self.assertNotEqual(fingerprint_a, fingerprint_b)
        self.assertGreater(jaccard(pieces_a, pieces_b), 0.5)

    def test_unrelated_texts_have_zero_or_low_similarity(self):
        pieces_a = shingles("化工刺鼻废气")
        pieces_b = shingles("工地半夜施工噪音")
        self.assertLess(jaccard(pieces_a, pieces_b), 0.2)

    def test_haversine_known_distance(self):
        distance = haversine_km(30.0, 120.0, 30.0, 121.0)
        self.assertAlmostEqual(distance, 96.4, delta=2.0)

    def test_correlate_ranks_running_offline_downwind_site_first(self):
        evidences = [
            SiteEvidence(site_id="strong", lat=30.001, lon=120.001,
                         production_running=True, treatment_online=False,
                         odor_keywords=("化工", "刺鼻"), site_text="化工 刺鼻 废气",
                         time_overlap=True),
            SiteEvidence(site_id="weak", lat=30.05, lon=120.05,
                         production_running=False, treatment_online=True,
                         odor_keywords=(), site_text="食品 加工", time_overlap=False),
        ]
        weather = WeatherSnapshot(wind_direction_deg=180.0, wind_speed_ms=3.0)
        result = correlate(description="化工刺鼻味道", complaint_lat=30.001, complaint_lon=120.001,
                           evidences=evidences, weather=weather,
                           params={"min_text_jaccard": 0.0, "min_score": 0.1})
        sites = [c.site_id for c in result.candidates]
        self.assertEqual(sites[0], "strong")
        strong = result.candidates[0]
        self.assertGreater(strong.score, 0.5)
        self.assertLessEqual(strong.confidence_low, strong.score)
        self.assertGreaterEqual(strong.confidence_high, strong.score)
        self.assertTrue(result.requires_site_inspection)
        factor_codes = {factor.code for factor in strong.factors}
        self.assertEqual(factor_codes,
                         {"time_overlap", "text_match", "spatial_proximity",
                          "downwind_transport", "production_running", "treatment_offline"})

    def test_confidence_interval_widens_with_missing_evidence(self):
        known = SiteEvidence(site_id="known", lat=30.0, lon=120.0,
                             production_running=True, treatment_online=False,
                             site_text="化工", time_overlap=True)
        unknown = SiteEvidence(site_id="known", lat=30.0, lon=120.0,
                               production_running=None, treatment_online=None,
                               site_text="化工", time_overlap=True)
        weather = WeatherSnapshot(wind_direction_deg=180.0, wind_speed_ms=2.0)
        params = {"min_text_jaccard": 0.0, "min_score": 0.0}
        r_known = correlate(description="化工", complaint_lat=30.0, complaint_lon=120.0,
                            evidences=[known], weather=weather, params=params)
        r_unknown = correlate(description="化工", complaint_lat=30.0, complaint_lon=120.0,
                              evidences=[unknown], weather=weather, params=params)
        width_known = r_known.candidates[0].confidence_high - r_known.candidates[0].confidence_low
        width_unknown = r_unknown.candidates[0].confidence_high - r_unknown.candidates[0].confidence_low
        self.assertGreater(width_unknown, width_known)

    def test_engine_is_pure_for_same_rule_params(self):
        evidence = SiteEvidence(site_id="s1", lat=30.0, lon=120.0,
                                production_running=True, treatment_online=False,
                                site_text="化工 刺鼻", time_overlap=True)
        weather = WeatherSnapshot(wind_direction_deg=90.0, wind_speed_ms=1.5)
        kwargs = dict(description="化工刺鼻", complaint_lat=30.0, complaint_lon=120.0,
                      evidences=[evidence], weather=weather)
        first = correlate(**kwargs).to_dict()
        second = correlate(**kwargs).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(correlate(**kwargs, params=merge_params(None)).params_hash,
                         correlate(**kwargs).params_hash)


if __name__ == "__main__":
    unittest.main()
