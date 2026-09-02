#!/usr/bin/env python3
"""Focused tests for daily-mainline ranking and candidate contracts."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import market_regime

from scripts.market_regime import (
    classify_news_phase,
    completion_content,
    completion_requires_nonthinking_retry,
    core_evidence_is_eligible,
    catalyst_entities,
    daily_mainline_search_topics,
    daily_mainline_trigger_topics,
    daily_mainline_candidates,
    daily_mainline_fallback,
    expand_mainline_block_ids,
    finalize_sector_rankings,
    confirm_market_state,
    classify_market_liquidity,
    classify_market_state,
    link_mainline_stocks,
    market_structure_tag,
    market_liquidity_state_meaning,
    selective_opportunity_evidence,
    reported_prior_catalyst_date,
    resolve_market_snapshot,
    select_daily_mainline_news,
    state_label,
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
    def test_market_liquidity_overlay_is_separate_from_market_state(self):
        self.assertEqual(classify_market_liquidity(1.15, 65, 58)["overlay_label"], "放量扩散")
        self.assertEqual(classify_market_liquidity(1.15, 40, 42)["overlay_label"], "放量承压")
        self.assertEqual(classify_market_liquidity(0.85, 65, 58)["overlay_label"], "缩量修复")
        self.assertEqual(classify_market_liquidity(0.85, 40, 42)["overlay_label"], "缩量弱势")

    def test_liquidity_meaning_respects_confirmed_state_boundary(self):
        report = {
            "state": {"confirmed_state": "CONSOLIDATING"},
            "market_liquidity": {"overlay_label": "缩量偏强"},
        }
        meaning = market_liquidity_state_meaning(report)
        self.assertIn("弱势震荡", meaning)
        self.assertIn("尚不足以强化状态切换", meaning)
        self.assertIn("正式状态和确认进度保持不变", meaning)

        report["market_liquidity"] = {"available": False, "overlay_label": "量能待确认"}
        self.assertIn("数据不足", market_liquidity_state_meaning(report))

    def test_selective_market_requires_local_opportunity(self):
        args = dict(
            trend_score=55, volatility_score=50, breadth_score=55, rotation_score=50,
            above20=4, above60=4, advance_ratio=48,
        )
        self.assertEqual(classify_market_state(**args, local_opportunity=True), "SELECTIVE")
        self.assertEqual(classify_market_state(**args, local_opportunity=False), "CONSOLIDATING")

    def test_selective_market_requires_medium_term_index_base(self):
        args = dict(
            trend_score=32, volatility_score=84, breadth_score=52, rotation_score=36,
            above20=5, above60=2, advance_ratio=29, local_opportunity=True,
        )
        self.assertEqual(classify_market_state(**args), "CONSOLIDATING")
        args["above60"] = 3
        self.assertEqual(classify_market_state(**args), "SELECTIVE")

    def test_offensive_market_rejects_fast_rotation(self):
        args = dict(
            trend_score=70, volatility_score=50, breadth_score=65,
            above20=6, above60=6, advance_ratio=70, local_opportunity=True,
        )
        self.assertEqual(classify_market_state(**args, rotation_score=40), "OFFENSIVE")
        self.assertEqual(classify_market_state(**args, rotation_score=50), "SELECTIVE")

    def test_cross_level_strength_is_local_opportunity_evidence(self):
        rows = [
            {"block_type": "industry_sw_l2", "block_name": "医疗服务", "sector_phase": "转强", "data_status": "READY", "relative_strength_20": 2, "relative_strength_5": 1, "above_ma20_ratio": 70},
            {"block_type": "gn", "block_name": "创新药", "sector_phase": "主线", "data_status": "READY", "relative_strength_20": 3, "relative_strength_5": 2, "above_ma20_ratio": 75},
        ]
        evidence = selective_opportunity_evidence(rows, [])
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["basis"], "cross_level_strength")

    def test_single_level_strength_does_not_open_selective_market(self):
        rows = [
            {"block_type": "gn", "block_name": "创新药", "sector_phase": "转强", "data_status": "READY", "relative_strength_20": 2, "relative_strength_5": 1, "above_ma20_ratio": 70},
            {"block_type": "gn", "block_name": "医疗改革", "sector_phase": "主线", "data_status": "READY", "relative_strength_20": 3, "relative_strength_5": 2, "above_ma20_ratio": 75},
        ]
        self.assertFalse(selective_opportunity_evidence(rows, [])["qualified"])

    def test_persistent_concept_alone_does_not_open_selective_market(self):
        rows = [
            {"block_type": "gn", "block_name": "创新药", "sector_phase": "主线", "data_status": "READY", "relative_strength_20": 3, "relative_strength_5": 2, "above_ma20_ratio": 75},
        ]
        self.assertFalse(selective_opportunity_evidence(rows, ["gn:创新药"])["qualified"])

    def test_selective_market_uses_one_primary_state_with_secondary_tag(self):
        report = {"state": {"confirmed_state": "SELECTIVE", "rotation_score": 36, "persistent_mainline_count": 1}}
        self.assertEqual(state_label("SELECTIVE", report), "结构行情")
        self.assertEqual(market_structure_tag(report), "主线集中")

        report["state"]["rotation_score"] = 70
        self.assertEqual(state_label("SELECTIVE", report), "结构行情")
        self.assertEqual(market_structure_tag(report), "快速轮动")

    def test_market_index_preserves_other_published_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            month = root / "202605"
            month.mkdir()
            for kind in ("signals", "vcp", "backtest", "valuation"):
                (month / f"{kind}_context_260506.js").write_text("", encoding="utf-8")
            with patch.object(market_regime, "DASHBOARD_DATA_DIR", root):
                market_regime.write_dashboard_index("260506", ["260506"])
            text = (root / "index.js").read_text(encoding="utf-8")
            payload = json.loads(text.split(" = ", 1)[1].rsplit(";", 1)[0])

        self.assertEqual(payload["market"]["available"], ["260506"])
        self.assertEqual(payload["backtest"]["available"], ["260506"])

    def test_market_state_confirmation_uses_two_of_three_not_consecutive_days(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE market_state_history(
            trade_date TEXT PRIMARY KEY, raw_state TEXT, confirmed_state TEXT,
            candidate_days INTEGER, confirmation_days INTEGER
        )""")
        sequence = [
            ("2026-06-30", "DEFENSIVE"),
            ("2026-07-01", "SELECTIVE"),
            ("2026-07-02", "DEFENSIVE"),
            ("2026-07-03", "SELECTIVE"),
        ]
        reports = []
        for trade_date, raw_state in sequence:
            report = {"state": {"raw_state": raw_state}}
            confirm_market_state(conn, trade_date, report)
            reports.append(report)
        self.assertEqual(reports[1]["state"]["confirmed_state"], "DEFENSIVE")
        self.assertEqual(reports[3]["state"]["candidate_days"], 2)
        self.assertEqual(reports[3]["state"]["confirmed_state"], "SELECTIVE")
        conn.close()

    def test_historical_snapshot_fallback_is_explicit_and_labeled(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE universe_members(trade_date TEXT);
            CREATE TABLE block_members(snapshot_date TEXT);
            CREATE TABLE stock_industries(snapshot_date TEXT);
        """)
        for table, field in (("universe_members", "trade_date"), ("block_members", "snapshot_date"), ("stock_industries", "snapshot_date")):
            conn.execute(f"INSERT INTO {table}({field}) VALUES('2026-07-01')")
        with self.assertRaisesRegex(RuntimeError, "成分快照缺失"):
            resolve_market_snapshot(conn, "2026-06-30")
        self.assertEqual(
            resolve_market_snapshot(conn, "2026-06-30", allow_fallback=True),
            ("2026-07-01", "current_snapshot_backfill"),
        )
        conn.close()

    def test_completion_rejects_reasoning_budget_exhaustion(self):
        payload = {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"completion_tokens": 3200, "completion_tokens_details": {"reasoning_tokens": 3200}},
        }
        with self.assertRaisesRegex(ValueError, "reasoning_tokens=3200"):
            completion_content(payload)

    def test_completion_accepts_nonempty_stopped_content(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": '  {"status": "ok"}  '}}]}
        self.assertEqual(completion_content(payload), '{"status": "ok"}')

    def test_only_completion_exhaustion_switches_off_thinking(self):
        self.assertTrue(completion_requires_nonthinking_retry(ValueError("LLM completion exhausted (finish_reason=length)")))
        self.assertTrue(completion_requires_nonthinking_retry(ValueError("LLM completion content is empty (finish_reason=stop)")))
        self.assertFalse(completion_requires_nonthinking_retry(ValueError("selected blocks lack representative stocks")))

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

    def test_intraday_report_can_audit_explicit_prior_catalyst(self):
        content = "核电概念走强，受7月31日国常会核准四个核电项目的消息提振。"
        self.assertEqual(reported_prior_catalyst_date(content, "2026-08-03"), "2026-07-31")
        raw = [{"title": "核电开盘速递", "date": "2026-08-03 10:05:00", "source": "测试", "content": content}]
        item = select_daily_mainline_news(raw, "2026-08-03", 12)[0]
        self.assertTrue(item["core_eligible"])
        self.assertEqual(item["core_timing_basis"], "reported_prior_event")
        self.assertEqual(item["event_date"], "2026-07-31")

    def test_preopen_article_preserves_explicit_prior_event_date(self):
        raw = [{"title": "核电盘前", "date": "2026-08-03 08:05:00", "source": "测试",
                "content": "7月31日国务院常务会议核准四个核电项目。"}]
        item = select_daily_mainline_news(raw, "2026-08-03", 12)[0]
        self.assertEqual(item["core_timing_basis"], "article_pre_open")
        self.assertEqual(item["event_date"], "2026-07-31")

    def test_intraday_report_without_dated_event_stays_ineligible(self):
        raw = [{"title": "核电盘中走强", "date": "2026-08-03 10:05:00", "source": "测试", "content": "核电概念盘中大涨。"}]
        item = select_daily_mainline_news(raw, "2026-08-03", 12)[0]
        self.assertFalse(item["core_eligible"])

    def test_fragmented_ai_blocks_add_application_search_topic(self):
        topics = daily_mainline_search_topics(["AI营销", "智谱AI", "ChatGPT", "软件服务"])
        self.assertEqual(topics[:2], ["AI应用", "AI商业化"])
        self.assertNotIn("AI应用", daily_mainline_search_topics(["白酒概念", "预制菜"]))

    def test_trigger_topics_prioritize_cross_covered_blocks(self):
        blocks = [
            {"block_id": "gn:风电", "block_name": "风电", "daily_score": 99.0, "qualified": True},
            {"block_id": "gn:核电", "block_name": "核电", "daily_score": 92.0, "qualified": True},
            {"block_id": "gn:电网", "block_name": "电网", "daily_score": 91.0, "qualified": True},
            {"block_id": "gn:综合", "block_name": "综合", "daily_score": 100.0, "qualified": True},
        ]
        stocks = [
            {"candidate_block_ids": ["gn:核电", "gn:电网"]},
            {"candidate_block_ids": ["gn:核电", "gn:电网"]},
            {"candidate_block_ids": ["gn:风电"]},
        ]
        self.assertEqual(daily_mainline_trigger_topics({"blocks": blocks, "stocks": stocks}, 3), ["核电", "电网", "风电"])

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

    def test_concept_only_selection_adds_cross_covered_industries(self):
        block_map = {
            "gn:核电": {"block_id": "gn:核电", "block_type": "gn", "block_name": "核电", "daily_score": 92.0, "qualified": True},
            "gn:核变": {"block_id": "gn:核变", "block_type": "gn", "block_name": "核变", "daily_score": 91.0, "qualified": True},
            "industry_sw_l2:电网": {"block_id": "industry_sw_l2:电网", "block_type": "industry_sw_l2", "block_name": "电网", "daily_score": 94.0, "qualified": True},
            "industry_sw_l1:电力": {"block_id": "industry_sw_l1:电力", "block_type": "industry_sw_l1", "block_name": "电力", "daily_score": 90.0, "qualified": True},
            "industry_sw_l1:建筑": {"block_id": "industry_sw_l1:建筑", "block_type": "industry_sw_l1", "block_name": "建筑", "daily_score": 99.0, "qualified": True},
        }
        stock_map = {
            "1": {"candidate_block_ids": ["gn:核电", "gn:核变", "industry_sw_l2:电网", "industry_sw_l1:电力", "industry_sw_l1:建筑"]},
            "2": {"candidate_block_ids": ["gn:核电", "gn:核变", "industry_sw_l2:电网", "industry_sw_l1:电力"]},
        }
        result = expand_mainline_block_ids(["gn:核电", "gn:核变"], ["1", "2"], block_map, stock_map)
        self.assertEqual(result, ["gn:核电", "gn:核变", "industry_sw_l2:电网", "industry_sw_l1:电力"])

    def test_mixed_level_selection_is_not_expanded(self):
        block_map = {
            "gn:核电": {"block_id": "gn:核电", "block_type": "gn"},
            "industry_sw_l2:电网": {"block_id": "industry_sw_l2:电网", "block_type": "industry_sw_l2"},
        }
        result = expand_mainline_block_ids(list(block_map), [], block_map, {})
        self.assertEqual(result, list(block_map))


if __name__ == "__main__":
    unittest.main()
