import unittest

from scripts.research_rps import select_pool_rps


class ResearchRpsTests(unittest.TestCase):
    def test_independent_windows_and_union_without_refill(self):
        values = {
            '000001': {'rps5_market': 99, 'rps20_market': 10},
            '000002': {'rps5_market': 10, 'rps20_market': 99},
            '000003': {'rps5_market': 98, 'rps20_market': 98},
            '000004': {'rps5_market': 1, 'rps20_market': 1},
        }
        result = select_pool_rps(values, values, values, top_n=2)
        self.assertEqual(result['rps5_top10'], ['000001', '000003'])
        self.assertEqual(result['rps20_top10'], ['000002', '000003'])
        self.assertEqual(result['union_codes'], ['000001', '000002', '000003'])
        self.assertEqual(result['overlap_codes'], ['000003'])
        self.assertNotIn('000004', result['union_codes'])

    def test_ties_keep_existing_rps20_order_and_use_rps20_for_rps5(self):
        values = {
            '000003': {'rps5_market': 90, 'rps20_market': 80, 'rps60_market': 50},
            '000002': {'rps5_market': 90, 'rps20_market': 80, 'rps60_market': 50},
            '000001': {'rps5_market': 90, 'rps20_market': 70, 'rps60_market': 99},
        }
        result = select_pool_rps(values, values, values)
        self.assertEqual(result['rps5_top10'], ['000002', '000003', '000001'])
        self.assertEqual(result['rps20_top10'], ['000002', '000003', '000001'])

    def test_missing_primary_only_affects_its_window_and_zero_is_valid(self):
        values = {
            '000001': {'rps5_market': 'nan', 'rps20_market': '0'},
            '000002': {'rps5_market': '95', 'rps20_market': ''},
            '000003': {'rps5_market': float('inf'), 'rps20_market': None},
        }
        result = select_pool_rps(values, values, values)
        self.assertEqual(result['rps5_top10'], ['000002'])
        self.assertEqual(result['rps20_top10'], ['000001'])
        self.assertEqual(result['missing_rps5'], ['000001', '000003'])
        self.assertEqual(result['missing_rps20'], ['000002', '000003'])

    def test_rejects_outside_pool_and_invalid_limits(self):
        with self.assertRaisesRegex(ValueError, 'outside Pool'):
            select_pool_rps(['000001', '000002'], ['000001'], {})
        for limit in [0, -1, True, 1.5]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                select_pool_rps([], [], {}, top_n=limit)


if __name__ == '__main__':
    unittest.main()
