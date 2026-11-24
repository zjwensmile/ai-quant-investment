import unittest

from ai_quant.evidence import (
    filter_future_agent_evidence,
    normalize_duplicate_evidence_ids,
    validate_agent_evidence,
)


class EvidencePolicyTests(unittest.TestCase):
    def output(self):
        return {
            "as_of_time": "2026-08-23T08:30:00+08:00",
            "decisions": [{
                "supporting_evidence": [{
                    "evidence_id": "e1", "source": "https://www.sse.com.cn/test",
                    "published_at": "2026-08-23T08:00:00+08:00",
                    "available_at": "2026-08-23T08:01:00+08:00",
                    "quality": "official_primary",
                }],
                "opposing_evidence": [],
            }],
        }

    def test_exact_url_and_past_evidence_are_accepted(self):
        validate_agent_evidence(self.output(), "2026-08-23T08:30:00+08:00")

    def test_future_evidence_is_rejected(self):
        output = self.output()
        output["decisions"][0]["supporting_evidence"][0]["published_at"] = (
            "2026-08-23T09:00:00+08:00"
        )
        with self.assertRaisesRegex(ValueError, "after"):
            validate_agent_evidence(output, "2026-08-23T08:30:00+08:00")

    def test_future_evidence_discards_only_affected_decision(self):
        output = self.output()
        output["rejected_candidates"] = []
        output["overall_data_quality"] = "valid"
        output["overall_summary"] = "Two decisions were assessed."
        future = output["decisions"][0]
        future["decision_id"] = "future-decision"
        future["subject"] = "未来信息标的"
        future["supporting_evidence"][0]["published_at"] = (
            "2026-08-23T09:00:00+08:00"
        )
        valid = self.output()["decisions"][0]
        valid["decision_id"] = "valid-decision"
        output["decisions"].append(valid)

        filtered, dropped = filter_future_agent_evidence(
            output, "2026-08-23T08:30:00+08:00"
        )

        self.assertEqual(1, dropped)
        self.assertEqual(
            ["valid-decision"],
            [item["decision_id"] for item in filtered["decisions"]],
        )
        self.assertEqual("insufficient_evidence", filtered["overall_data_quality"])
        self.assertIn("未来信息标的", filtered["rejected_candidates"][0])
        self.assertEqual(2, len(output["decisions"]))
        validate_agent_evidence(filtered, "2026-08-23T08:30:00+08:00")

    def test_future_news_is_filtered_without_removing_valid_news(self):
        output = {
            "as_of_time": "2026-08-23T08:30:00+08:00",
            "core_news": [
                {
                    "news_id": "future",
                    "title": "未来新闻",
                    "url": "https://example.com/future",
                    "published_at": "2026-08-23T09:00:00+08:00",
                    "available_at": "2026-08-23T09:01:00+08:00",
                },
                {
                    "news_id": "valid",
                    "title": "已知新闻",
                    "url": "https://example.com/valid",
                    "published_at": "2026-08-23T08:00:00+08:00",
                    "available_at": "2026-08-23T08:01:00+08:00",
                },
            ],
            "rejected_noise": [],
            "data_quality": "valid",
            "summary": "News review.",
        }

        filtered, dropped = filter_future_agent_evidence(
            output, "2026-08-23T08:30:00+08:00"
        )

        self.assertEqual(1, dropped)
        self.assertEqual(["valid"], [item["news_id"] for item in filtered["core_news"]])
        self.assertIn("未来新闻", filtered["rejected_noise"][0])
        self.assertEqual("insufficient_evidence", filtered["data_quality"])

    def test_non_url_source_is_rejected(self):
        output = self.output()
        output["decisions"][0]["supporting_evidence"][0]["source"] = "上交所"
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_agent_evidence(output, "2026-08-23T08:30:00+08:00")

    def test_duplicate_model_evidence_ids_are_normalized_without_losing_items(self):
        output = self.output()
        duplicate = self.output()["decisions"][0]
        duplicate["supporting_evidence"][0]["claim"] = "Second use of the source."
        output["decisions"].append(duplicate)

        normalized, renamed = normalize_duplicate_evidence_ids(output)

        self.assertEqual(1, renamed)
        ids = [
            item["evidence_id"]
            for decision in normalized["decisions"]
            for item in decision["supporting_evidence"]
        ]
        self.assertEqual(["e1", "e1-2"], ids)
        validate_agent_evidence(normalized, "2026-08-23T08:30:00+08:00")

    def test_future_evidence_discards_affected_concept(self):
        output = {
            "as_of_time": "2026-08-23T08:30:00+08:00",
            "concepts": [
                {
                    "concept_id": "semiconductor",
                    "supporting_evidence": [{
                        "evidence_id": "future-concept",
                        "source": "https://example.com/future-concept",
                        "published_at": "2026-08-23T09:00:00+08:00",
                        "available_at": "2026-08-23T09:00:00+08:00",
                        "quality": "major_media",
                    }],
                },
                {"concept_id": "dividend", "supporting_evidence": []},
            ],
            "summary": "Concept selection.",
        }

        filtered, dropped = filter_future_agent_evidence(
            output, "2026-08-23T08:30:00+08:00"
        )

        self.assertEqual(1, dropped)
        self.assertEqual(["dividend"], [x["concept_id"] for x in filtered["concepts"]])
        self.assertIn("Safety filter removed", filtered["summary"])

    def test_candidate_evidence_uses_same_url_and_timestamp_policy(self):
        output = {
            "as_of_time": "2026-08-23T08:30:00+08:00",
            "candidates": [{
                "symbol": "600000",
                "supporting_evidence": [{
                    "evidence_id": "candidate-evidence",
                    "source": "not-an-url",
                    "published_at": "2026-08-23T08:00:00+08:00",
                    "available_at": "2026-08-23T08:01:00+08:00",
                    "quality": "company_primary",
                }],
            }],
        }
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_agent_evidence(output, "2026-08-23T08:30:00+08:00")

    def test_duplicate_evidence_is_normalized_across_discovery_items(self):
        output = {
            "as_of_time": "2026-08-23T08:30:00+08:00",
            "concepts": [{
                "concept_id": "semiconductor",
                "supporting_evidence": [{
                    "evidence_id": "shared",
                    "source": "https://example.com/concept",
                    "published_at": "2026-08-23T08:00:00+08:00",
                    "available_at": "2026-08-23T08:01:00+08:00",
                    "quality": "secondary",
                }],
            }],
            "candidates": [{
                "symbol": "600000",
                "supporting_evidence": [{
                    "evidence_id": "shared",
                    "source": "https://example.com/candidate",
                    "published_at": "2026-08-23T08:00:00+08:00",
                    "available_at": "2026-08-23T08:01:00+08:00",
                    "quality": "company_primary",
                }],
            }],
        }
        normalized, renamed = normalize_duplicate_evidence_ids(output)
        self.assertEqual(1, renamed)
        validate_agent_evidence(normalized, "2026-08-23T08:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
