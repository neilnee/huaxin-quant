#!/usr/bin/env python3
import unittest

from scripts import signal_fundamentals as sf


class SignalFundamentalsTests(unittest.TestCase):
    def test_period_availability_is_conservative(self):
        q1 = sf.parse_period_label("2026一季报")
        annual = sf.parse_period_label("2025年报")
        self.assertEqual(q1["report_end"], "2026-03-31")
        self.assertEqual(q1["available_from"], "2026-04-30")
        self.assertEqual(annual["available_from"], "2026-04-30")
        self.assertEqual(sf.expected_report_end("2026-08-07"), "2026-03-31")

    def test_extract_snapshot_ignores_future_report(self):
        name_map = {
            "np": "归属于母公司股东的净利润",
            "ocf": "经营活动产生的现金流量净额",
            "debt": "资产负债率",
            "rev": "营业收入同比增长率",
            "growth": "归属母公司股东的净利润同比增长率",
            "gross": "销售毛利率",
            "roe": "净资产收益率ROE(加权)",
        }
        raw_table = {
            "headName": ["2026中报", "2026一季报", "2025年报"],
            "np": [12, -2, -5], "ocf": [2, -1, 3], "debt": [40, 72, 60],
            "rev": [30, -25, 5], "growth": [50, -40, 2],
            "gross": [20, 8, 15], "roe": [3, -1, 2],
        }
        payload = {"data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{
            "code": "600123.SH", "nameMap": name_map, "rawTable": raw_table,
        }]}}}}
        snapshot = sf.extract_snapshot(payload, "600123", "2026-08-07")
        self.assertEqual(snapshot["label"], "2026一季报")
        self.assertEqual(snapshot["values"]["annual_net_profit"], -5)
        self.assertEqual(
            sf.risk_tags(snapshot["values"]),
            ["最新期亏损", "年度亏损", "高负债", "经营现金流为负", "营收明显下滑", "利润明显下滑", "毛利率偏低"],
        )

    def test_cached_snapshot_respects_signal_date(self):
        payload = {"snapshots": [
            {"report_end": "2026-03-31", "available_from": "2026-04-30"},
            {"report_end": "2026-06-30", "available_from": "2026-08-31"},
        ]}
        self.assertEqual(sf.cached_snapshot(payload, "2026-08-07")["report_end"], "2026-03-31")


if __name__ == "__main__":
    unittest.main()
