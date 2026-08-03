#!/usr/bin/env python3
"""Focused tests for daily-mainline ranking and candidate contracts."""

import unittest

from scripts.market_regime import (
    classify_news_phase,
    core_evidence_is_eligible,
    catalyst_entities,
    daily_mainline_search_topics,
    daily_mainline_candidates,
    daily_mainline_fallback,
    finalize_sector_rankings,
    link_mainline_stocks,
    select_daily_mainline_news,
)


def sector(name, rel1, rel5, rel20, breadth, volume, density, kind="gn"):
    return {
        "block_type": kind,
        "block_name": name,
        "relative_strength_1": rel1,
        "relative_strength_5": rel5,
        "relative_strength_20": rel20,
        "median_return_1": rel1,
        "up_breadth": breadth,
        "volume_activity": volume,
        "daily_strong_density": density,
        "sector_state": "观察中",
    }


class DailyMainlineTests(unittest.TestCase):
    def test_daily_rank_is_independent_from_twenty_day_rank(self):
        rows = finalize_sector_rankings([
            sector("旧主线", -1.0, 4.0, 12.0, 35.0, 0.8, 0.0),
            sector("当日主线", 4.0, 1.0, -2.0, 95.0, 1.8, 55.0),
        ])
        by_name = {row["block_name"]: row for row in rows}
        self.assertEqual(by_name["旧主线"]["rank_20"], 1)
        self.assertEqual(by_name["旧主线"]["rank_1"], 2)
        self.assertEqual(by_name["当日主线"]["rank_20"], 2)
        self.assertEqual(by_name["当日主线"]["rank_1"], 1)
        self.assertGreater(by_name["当日主线"]["daily_score"], by_name["旧主线"]["daily_score"])
        self.assertEqual(by_name["旧主线"]["sector_state"], "观察中")

    def test_stock_candidate_keeps_all_related_blocks(self):
        rows = finalize_sector_rankings([
            sector("AI应用", 4.0, 3.0, 2.0, 95.0, 1.8, 50.0),
            sector("软件服务", 3.0, 2.0, 1.0, 90.0, 1.6, 40.0, "industry_sw_l2"),
        ])
        stock = {"code": "300001", "name": "示例股份", "return_1": 10.0, "rps1_market": 99.0,
                 "rps5_market": 95.0, "volume_ratio_20": 2.0, "daily_strength_score": 96.0, "rank": 1}
        report = {"daily_sector_leaders": {
            "gn:AI应用": [stock],
            "industry_sw_l2:软件服务": [stock],
        }}
        candidates = daily_mainline_candidates(report, rows)
        self.assertEqual(candidates["stocks"][0]["candidate_block_ids"], ["industry_sw_l2:软件服务", "gn:AI应用"])

    def test_fallback_strips_nested_leader_lists(self):
        block = {"block_id": "gn:AI应用", "block_type": "gn", "block_name": "AI应用", "daily_score": 99.0,
                 "relative_strength_1": 5.0, "up_breadth": 95.0, "volume_activity": 1.8,
                 "daily_strong_density": 50.0, "rank_1": 1, "median_return_1": 6.0, "qualified": True,
                 "leaders": [{"code": "300001"}]}
        result = daily_mainline_fallback({"blocks": [block], "stocks": []}, {"status": "skipped"}, "test")
        self.assertNotIn("leaders", result["strong_blocks"][0])

    def test_news_phase_respects_a_share_session_timeline(self):
        as_of = "2026-07-31"
        self.assertEqual(classify_news_phase("2026-07-30 23:59:00", as_of), "pre_open")
        self.assertEqual(classify_news_phase("2026-07-31 09:29:59", as_of), "pre_open")
        self.assertEqual(classify_news_phase("2026-07-31 09:30:00", as_of), "intraday")
        self.assertEqual(classify_news_phase("2026-07-31 14:59:59", as_of), "intraday")
        self.assertEqual(classify_news_phase("2026-07-31 15:00:00", as_of), "post_close")
        self.assertEqual(classify_news_phase("2026-08-01 08:00:00", as_of), "future")
        self.assertEqual(classify_news_phase("2026-07-31", as_of), "unknown")
        self.assertEqual(classify_news_phase("2026-07-31 00:00:00", as_of, "A股复盘笔记"), "post_close")

    def test_news_selection_keeps_pre_open_trigger_auditable(self):
        raw = [
            {"title": "盘后复盘", "date": "2026-07-31 17:00:00", "source": "测试", "content": "复盘内容"},
            {"title": "微软财报", "date": "2026-07-30 22:00:00", "source": "测试", "content": "AI商业化数据"},
            {"title": "智谱旧闻", "date": "2026-07-20 08:00:00", "source": "测试", "content": "较早背景"},
            {"title": "盘中政策", "date": "2026-07-31 11:00:00", "source": "测试", "content": "政策内容"},
        ]
        items = select_daily_mainline_news(raw, "2026-07-31", 12)
        evidence_map = {item["title"]: item for item in items}
        self.assertEqual(items[0]["title"], "微软财报")
        self.assertTrue(core_evidence_is_eligible(["微软财报", "盘中政策"], evidence_map))
        self.assertFalse(core_evidence_is_eligible(["盘中政策", "微软财报"], evidence_map))
        self.assertFalse(core_evidence_is_eligible(["智谱旧闻", "微软财报"], evidence_map))

    def test_fragmented_ai_blocks_add_application_search_topic(self):
        topics = daily_mainline_search_topics(["AI营销", "智谱AI", "ChatGPT", "软件服务"])
        self.assertEqual(topics[:2], ["AI应用", "AI商业化"])
        self.assertNotIn("AI应用", daily_mainline_search_topics(["白酒概念", "预制菜"]))

    def test_catalyst_entities_rank_by_context_frequency(self):
        raw = [
            {"date": "2026-07-31 12:00:00", "title": "微软财报", "content": "微软Azure与Microsoft Copilot超预期"},
            {"date": "2026-07-31 13:00:00", "title": "华为动态", "content": "华为发布产品"},
        ]
        self.assertEqual(catalyst_entities(raw, "2026-07-31")[:2], ["微软", "华为"])

    def test_selected_stocks_must_cover_every_selected_block(self):
        block_map = {
            "gn:AIGC": {"block_name": "AIGC"},
            "gn:AI营销": {"block_name": "AI营销"},
            "gn:多模态": {"block_name": "多模态"},
        }
        stock_map = {
            "300001": {"code": "300001", "name": "甲", "candidate_block_ids": ["gn:AIGC", "gn:多模态"]},
            "300002": {"code": "300002", "name": "乙", "candidate_block_ids": ["gn:AI营销"]},
        }
        rows, uncovered = link_mainline_stocks(list(block_map), list(stock_map), block_map, stock_map)
        self.assertFalse(uncovered)
        self.assertEqual(rows[0]["selected_block_names"], ["AIGC", "多模态"])
        _, uncovered = link_mainline_stocks(list(block_map), ["300001"], block_map, stock_map)
        self.assertEqual(uncovered, {"gn:AI营销"})


if __name__ == "__main__":
    unittest.main()
