import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.research_screening import (
    build_preselection, check_review, fingerprint, load_inputs, load_sector_history,
    review_template, write_package,
)


def quant(code, score=65, *, quality=False, terminal=False, post='PRE_BREAKOUT'):
    bonus = lambda hit: {'status': 'COMPLETE', 'hit': hit, 'score': 6 if hit else 0}
    return {
        'code': code, 'name': code, 'model2_include': True, 'structure_valid': True,
        'structure_type': 'VCP', 'structure_stage': 'VCP_FORMING',
        'structure_score': score, 'structure_scoring_status': 'COMPLETE',
        'post_breakout_state': post, 'structure_risk_flags': [], 'structure_risk_score': 0,
        'adjustment_status': 'NO_ACTION', 'setup_signal': 'NONE', 'setup_quality': 'D',
        'structure_score_details': {
            'status': 'COMPLETE', 'total': score, 'components': {'impulse': 10},
            'impulse': {'status': 'COMPLETE', 'contribution': 10},
            'contraction_quality': bonus(quality), 'terminal_micro': bonus(terminal),
        },
    }


def selection(records, **kwargs):
    return build_preselection(
        kwargs.pop('pool_codes', records), kwargs.pop('strengths', {}), records,
        kwargs.pop('bloom', {c: {'bloom_status': 'FORMING', 'risk_level': 'LOW'} for c in records}),
        kwargs.pop('plans', {}), analysis_date='2026-10-26',
        followups=kwargs.pop('followups', {c: {} for c in records}), **kwargs)


def completed_review(selected, decision='条件观察'):
    review = review_template(selected)
    for row in review['reviews']:
        row.update(wave_start='2026-09-01', price_volume='推进保留但回撤仍需验证',
                   support_supply='上沿有供应，承接需继续验证', decision=decision,
                   reason='当前参与位置尚未确认')
    return review


def sector_window():
    return {'trade_dates': ['2026-10-22', '2026-10-23', '2026-10-26'],
            'rows': [], 'reason': None}


def add_sector(window, name, *, ranks=(0.8, 0.6, 0.08), breadth=(40, 40, 70)):
    window['rows'].extend(
        {'trade_date': day, 'block_name': name, 'rank_pct_5': rank,
         'above_ma20_ratio': coverage, 'relative_strength_1': relative,
         'median_return_1': change, 'data_status': 'READY', 'history_basis': 'point_in_time'}
        for day, rank, coverage, relative, change in zip(
            window['trade_dates'], ranks, breadth, (0.3, 1.9, 4.4), (-1.3, 0, 2.3))
    )


class ResearchScreeningTests(unittest.TestCase):
    def test_score_threshold_keeps_all_without_requiring_bonuses(self):
        result = selection({'000001': quant('000001', 65),
                            '000002': quant('000002', 64.99),
                            '000003': quant('000003', 80)})
        self.assertEqual(result['groups']['quantitative'], ['000001', '000003'])
        self.assertEqual(result['chart_review_codes'], ['000003', '000001'])
        self.assertEqual(result['maintenance_codes'], ['000002'])

    def test_either_component_can_supplement_but_does_not_add_to_score(self):
        records = {'000001': quant('000001', 55, quality=True),
                   '000002': quant('000002', 54, terminal=True),
                   '000003': quant('000003', 53, quality=True, terminal=True),
                   '000004': quant('000004', 60)}
        result = selection(records)
        self.assertEqual(result['groups']['component_supplement'], ['000001', '000002', '000003'])
        self.assertEqual([r['structure_score'] for r in result['records']], [60, 55, 54, 53])
        self.assertEqual(result['chart_review_codes'], ['000001', '000002', '000003'])

    def test_components_only_expand_explicit_supplement_universe(self):
        result = selection({'000001': quant('000001', 55, quality=True)}, followups={})
        self.assertEqual(result['chart_review_codes'], [])
        self.assertEqual(result['records'], [])

    def test_old_breakout_score_and_bonus_cannot_qualify_new_platform(self):
        old = quant('000001', 80, quality=True, terminal=True, post='POST_BREAKOUT_HOT')
        old['structure_breakout_score'] = 95
        result = selection({'000001': old})
        self.assertEqual(result['chart_review_codes'], [])
        self.assertEqual(result['records'][0]['score_context'], 'POST_BREAKOUT')

    def test_missing_or_contradictory_score_evidence_cannot_supplement(self):
        records = {f'{i:06d}': quant(f'{i:06d}', 55, quality=True) for i in range(1, 5)}
        records['000001']['structure_score_details']['impulse']['contribution'] = 0
        records['000002']['structure_score_details']['impulse']['status'] = 'NO_IMPULSE'
        records['000003']['structure_score_details']['contraction_quality']['score'] = 0
        records['000004'].pop('structure_score_details')
        self.assertEqual(selection(records)['chart_review_codes'], [])

    def test_score_fact_conflict_blocks_regular_entry_but_not_rps_ranking(self):
        q = quant('000001', 75)
        q['structure_score_details']['total'] = 55
        result = selection({'000001': q}, strengths={'000001': {'rps5_market': 99}})
        self.assertEqual(result['groups']['quantitative'], [])
        self.assertEqual(result['groups']['rps'], ['000001'])
        self.assertEqual(result['records'][0]['route'], 'DATA_REVIEW')

    def test_low_score_buy_plan_and_mature_sources_are_not_threshold_filtered(self):
        buy, planned, mature = (quant(f'{i:06d}', 50) for i in range(1, 4))
        buy['setup_signal'] = 'PULLBACK_BUY'
        mature['structure_stage'] = 'VCP_MATURE'
        result = selection({'000001': buy, '000002': planned, '000003': mature},
                           plans={'000002': [{'target_quality': 'B'}]})
        self.assertEqual(result['groups']['quantitative'], ['000001', '000002', '000003'])

    def test_rps_is_pool_bounded_independent_and_not_refilled_for_holdings(self):
        pool = [f'{i:06d}' for i in range(1, 13)]
        strengths = {c: {'rps5_market': i, 'rps20_market': i} for i, c in enumerate(pool)}
        strengths['999999'] = {'rps5_market': 100, 'rps20_market': 100}
        invalid = quant('000012', 40)
        invalid['structure_valid'] = False
        result = selection({'000012': invalid}, pool_codes=pool, strengths=strengths,
                           bloom={'000012': {'bloom_status': 'EXIT'}}, held_codes={'000011'})
        self.assertEqual(len(result['groups']['rps']), 10)
        self.assertIn('000012', result['chart_review_codes'])
        self.assertIn('000010', result['chart_review_codes'])  # No Quant coverage.
        self.assertNotIn('999999', result['groups']['rps'])
        self.assertNotIn('000002', result['groups']['rps'])
        self.assertEqual(result['holding_review_codes'], ['000011'])
        self.assertEqual(len(result['chart_review_codes']), 9)

    def test_risk_gate_applies_to_supplements_but_rps_retains_restriction(self):
        q = quant('000001', 55, quality=True)
        q['structure_risk_flags'] = ['HARD_FLAG']
        risk = {'hard_flags': ['HARD_FLAG'], 'risk_block_min_score': 99}
        self.assertEqual(selection({'000001': q}, risk_rules=risk)['chart_review_codes'], [])
        result = selection({'000001': q}, risk_rules=risk,
                           strengths={'000001': {'rps20_market': 99}})
        self.assertEqual(result['records'][0]['route'], 'RESTRICTED_REVIEW')
        review = completed_review(result, '重点研究')
        self.assertFalse(check_review(result, review)['complete'])

    def test_sector_supplement_keeps_original_order_and_requires_current_structure(self):
        history = sector_window()
        add_sector(history, '白色家电')
        add_sector(history, '云服务', ranks=(0.8, 0.9, 0.08), breadth=(40, 30, 70))
        for row in history['rows']:
            for field in ('rank_pct_5', 'above_ma20_ratio', 'relative_strength_1', 'median_return_1'):
                row[field] = str(row[field])
        records = {f'{i:06d}': quant(f'{i:06d}', score)
                   for i, score in enumerate((70, 64, 63, 62, 61, 60, 59, 58, 57, 65), 1)}
        records['000002']['structure_score_details']['impulse']['contribution'] = 0
        records['000003']['post_breakout_state'] = 'POST_BREAKOUT_HOT'
        records['000006']['adjustment_status'] = 'CONFLICT'
        names = {c: {'sw_l2_name': '云服务' if c == '000009' else '白色家电'} for c in records}
        result = selection(records, strengths=names, pool_codes=set(records) - {'000010'},
                           held_codes={'000007'}, sector_history=history)
        self.assertEqual(result['chart_review_codes'], ['000001', '000004', '000005'])
        self.assertEqual(result['groups']['sector_supplement'], ['000004', '000005'])
        industry, = result['sector_supplement']['industries']
        self.assertEqual(industry['name'], '白色家电')
        self.assertEqual(industry['eligible_codes'], ['000001', '000004', '000005', '000008'])
        self.assertEqual(industry['overlap_codes'], ['000001'])
        self.assertEqual(industry['exclusions']['000006'], '复权冲突')
        self.assertEqual(industry['exclusions']['000007'], '持仓')
        self.assertTrue(check_review(result, completed_review(result))['complete'])

    def test_sector_supplement_needs_point_in_time_history_and_respects_total_limit(self):
        history = sector_window()
        for name, rank in zip(('第一', '第二', '第三', '第四', '缺口', '回填'),
                              (0.05, 0.10, 0.16, 0.20, 0.03, 0.02)):
            add_sector(history, name, ranks=(0.8, 0.6, rank))
        history['rows'] = [row for row in history['rows'] if not (
            row['block_name'] == '缺口' and row['trade_date'] == '2026-10-23')]
        history['rows'][-1]['history_basis'] = 'current_snapshot_backfill'
        records = {f'{i:06d}': quant(f'{i:06d}', 40 + i) for i in range(1, 13)}
        names = {c: {'sw_l2_name': ('第一', '第二', '第三', '第四', '缺口', '回填')[i // 2]}
                 for i, c in enumerate(records)}
        result = selection(records, strengths=names, sector_history=history)
        self.assertEqual(result['chart_review_codes'],
                         ['000002', '000001', '000004', '000003', '000006', '000005'])
        audit = result['sector_supplement']
        self.assertEqual(audit['status'], 'PARTIAL')
        self.assertEqual(audit['industry_data_gaps'],
                         {'缺口': ['2026-10-23'], '回填': ['2026-10-26']})
        self.assertEqual(audit['industries'][-1]['eligible_codes'], ['000008', '000007'])
        self.assertEqual(audit['industries'][-1]['added_codes'], [])

    def test_unavailable_sector_window_keeps_base_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            history = load_sector_history(Path(folder), '2026-10-26')
        result = selection({'000001': quant('000001')}, sector_history=history)
        self.assertEqual(result['sector_supplement']['status'], 'UNAVAILABLE')
        self.assertEqual(result['chart_review_codes'], ['000001'])

    def test_selection_is_independent_of_mapping_order_and_has_no_mutations(self):
        records = {'000002': quant('000002', 75), '000001': quant('000001', 75)}
        before = copy.deepcopy(records)
        a, b = selection(records), selection(dict(reversed(list(records.items()))))
        self.assertEqual(a, b)
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertEqual(records, before)
        self.assertEqual(a['chart_review_codes'], ['000001', '000002'])

    def test_package_preserves_previous_output(self):
        result = selection({'000001': quant('000001')})
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            write_package(result, output)
            old = (output / 'preselection.json').read_bytes()
            with self.assertRaises(FileExistsError):
                write_package(selection({}), output)
            self.assertEqual((output / 'preselection.json').read_bytes(), old)

    def test_missing_explicit_history_and_future_market_context_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            missing = root / 'missing_history.json'
            with self.assertRaises(FileNotFoundError):
                load_inputs(root, '2026-10-23', missing)
            future = root / 'future.json'
            future.write_text(json.dumps({'market_data_as_of': '2026-10-26', 'items': []}))
            with self.assertRaisesRegex(ValueError, '分析日之后'):
                load_inputs(root, '2026-10-23', future)

    def test_review_requires_coverage_two_assessments_and_valid_wave_date(self):
        result = selection({'000001': quant('000001')})
        review = completed_review(result)
        self.assertTrue(check_review(result, review)['complete'])
        for mutate in (
            lambda r: r['reviews'].clear(),
            lambda r: r['reviews'].append(copy.deepcopy(r['reviews'][0])),
            lambda r: r['reviews'][0].update(price_volume=''),
            lambda r: r['reviews'][0].update(support_supply=None),
            lambda r: r['reviews'][0].update(wave_start='2026-10-27'),
            lambda r: r.update(preselection_sha256='stale'),
            lambda r: r['reviews'][0].update(structure_score=100),
        ):
            changed = copy.deepcopy(review)
            mutate(changed)
            self.assertFalse(check_review(result, changed)['complete'])

    def test_lower_score_focus_needs_explanation_and_pending_is_incomplete(self):
        result = selection({'000001': quant('000001', 80), '000002': quant('000002', 70)})
        review = completed_review(result)
        review['reviews'][1]['decision'] = '重点研究'
        self.assertFalse(check_review(result, review)['complete'])
        review['reviews'][1]['comparison_reason'] = '高分对象供应尚未修复，本股承接更近且有需求回应'
        self.assertTrue(check_review(result, review)['complete'])
        review['reviews'][0]['decision'] = '待核'
        checked = check_review(result, review)
        self.assertFalse(checked['complete'])
        self.assertEqual(checked['pending_codes'], ['000001'])

    def test_reordering_focuses_requires_valid_ranks_and_comparison(self):
        result = selection({'000001': quant('000001', 80), '000002': quant('000002', 70)})
        review = completed_review(result, '重点研究')
        review['reviews'][0]['priority_rank'] = 2
        review['reviews'][1]['priority_rank'] = 1
        self.assertFalse(check_review(result, review)['complete'])
        review['reviews'][1]['comparison_reason'] = '低分对象的承接及参与窗口更明确'
        checked = check_review(result, review)
        self.assertTrue(checked['complete'])
        self.assertEqual(checked['focus_codes'], ['000002', '000001'])
        review['reviews'][0]['priority_rank'] = 1
        self.assertFalse(check_review(result, review)['complete'])


if __name__ == '__main__':
    unittest.main()
