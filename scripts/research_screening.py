"""Prepare reproducible candidates for manual chart review from saved facts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from math import isfinite, prod
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.research_rps import select_pool_rps
from scripts.shared import PROJECT_ROOT, expected_trade_date, normalize_date_arg


VERSION = 'manual_preselection_v2'
SCORE_MIN = 65
SECTOR_SESSIONS = 3
SECTOR_RANK_LIMIT = 0.25
SECTOR_MA20_MIN = 50
SECTOR_MAX_PER_INDUSTRY = 2
SECTOR_MAX_TOTAL = 6
BUY_SIGNALS = {'PULLBACK_BUY', 'BREAKOUT_BUY', 'RETEST_BUY'}
BLOCKED_BLOOM = {'RISK_BLOCKED', 'DATA_ISSUE', 'INVALID', 'EXIT'}
DECISIONS = {'重点研究', '条件观察', '突破/强势后跟踪', '待核'}
REVIEW_FIELDS = ('code', 'wave_start', 'price_volume', 'support_supply',
                 'decision', 'reason', 'comparison_reason', 'priority_rank')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def score_gaps(q):
    if not q:
        return ['Quant缺失']
    details = q.get('structure_score_details') or {}
    gaps = []
    score, total = number(q.get('structure_score')), number(details.get('total'))
    if score is None:
        gaps.append('结构分缺失或非法')
    if q.get('structure_scoring_status') != 'COMPLETE' or details.get('status') != 'COMPLETE':
        gaps.append('评分明细未完成')
    if not isinstance(details.get('components'), dict) or total is None:
        gaps.append('评分分项缺失')
    if score is not None and total is not None and abs(score - total) > 0.011:
        gaps.append('结构分与明细总分不一致')
    for key in ('contraction_quality', 'terminal_micro'):
        bonus = details.get(key) or {}
        value = number(bonus.get('score'))
        if bonus.get('status') != 'COMPLETE' or value is None or not isinstance(bonus.get('hit'), bool):
            gaps.append(f'{key}明细缺失')
        elif bonus['hit'] != (value > 0):
            gaps.append(f'{key}命中与分值不一致')
    return gaps


def prebreak(q):
    return (q.get('model2_include') is True and q.get('structure_valid') is True
            and q.get('structure_type') == 'VCP'
            and q.get('post_breakout_state') == 'PRE_BREAKOUT')


def has_qualified_impulse(q):
    impulse = (q.get('structure_score_details') or {}).get('impulse') or {}
    return impulse.get('status') == 'COMPLETE' and (number(impulse.get('contribution')) or 0) > 0


def hard_risk(q, b, risk_rules):
    flags = set(q.get('structure_risk_flags') or [])
    limit = number(risk_rules.get('risk_block_min_score'))
    risk_score = number(q.get('structure_risk_score'))
    return (b.get('bloom_status') == 'RISK_BLOCKED' or b.get('risk_level') == 'HARD'
            or bool(flags & set(risk_rules.get('hard_flags') or []))
            or (limit is not None and risk_score is not None and risk_score >= limit))


def regular_exclusion(code, pool, quant, bloom, held, risk_rules):
    q, b = quant.get(code) or {}, bloom.get(code) or {}
    if code in held:
        return '持仓'
    if code not in pool:
        return 'Pool外'
    if not q:
        return 'Quant缺失'
    if q.get('adjustment_status') == 'CONFLICT':
        return '复权冲突'
    if b.get('bloom_status') in BLOCKED_BLOOM:
        return 'Bloom_' + b['bloom_status']
    if not q.get('model2_include') or not q.get('structure_valid'):
        return '结构无效或未纳入'
    if not b:
        return 'Bloom缺失'
    if hard_risk(q, b, risk_rules):
        return '硬风险'
    if score_gaps(q):
        return '评分明细待核'
    return None


def _fact_row(code, q, b, plans, origins, groups, exclusion, held, risk_rules):
    details = q.get('structure_score_details') or {}
    impulse = details.get('impulse') or {}
    quality = details.get('contraction_quality') or {}
    terminal = details.get('terminal_micro') or {}
    post = q.get('post_breakout_state')
    context = ('POST_BREAKOUT' if post and post != 'PRE_BREAKOUT'
               else 'CURRENT_VCP' if prebreak(q) else 'UNRECOGNIZED')
    route = 'CHART_REVIEW'
    if code in held:
        route = 'HOLDING_REVIEW'
    elif (q.get('adjustment_status') == 'CONFLICT' or q.get('structure_type') == 'DATA_ISSUE'
          or (q and score_gaps(q))
          or b.get('bloom_status') == 'DATA_ISSUE'):
        route = 'DATA_REVIEW'
    elif hard_risk(q, b, risk_rules):
        route = 'RESTRICTED_REVIEW'
    return {
        'code': code, 'name': q.get('name') or b.get('name') or code,
        'sources': origins, 'groups': groups, 'route': route,
        'structure_score': number(q.get('structure_score')),
        'score_context': context, 'structure_stage': q.get('structure_stage'),
        'structure_valid': q.get('structure_valid'), 'post_breakout_state': post,
        'score_components': details.get('components'), 'score_gaps': score_gaps(q),
        'impulse': impulse, 'contraction_quality': quality, 'terminal_micro': terminal,
        'contraction_group': q.get('contraction_group') or [],
        'breakout_date': q.get('structure_breakout_date'),
        'frozen_breakout_score': q.get('structure_breakout_score'),
        'setup_signal': q.get('setup_signal'), 'setup_quality': q.get('setup_quality'),
        'plans': plans, 'bloom_status': b.get('bloom_status'),
        'adjustment_status': q.get('adjustment_status'),
        'structure_risk_flags': q.get('structure_risk_flags') or [],
        'regular_exclusion': exclusion,
    }


def select_sector_supplement(sector_history, pool, strengths, quant, existing, excluded, order):
    dates = sector_history['trade_dates']
    audit = {'status': 'UNAVAILABLE', 'trade_dates': dates, 'reason': sector_history['reason'],
             'industry_data_gaps': {}, 'industries': []}
    if audit['reason']:
        return set(), audit

    by_industry = defaultdict(list)
    for row in sector_history['rows']:
        by_industry[row['block_name']].append(row)
    qualifying = []
    numeric_fields = ('rank_pct_5', 'above_ma20_ratio', 'relative_strength_1', 'median_return_1')
    for name, history in sorted(by_industry.items()):
        days = {row['trade_date']: row | {key: number(row[key]) for key in numeric_fields} for row in history}
        gaps = [day for day in dates if day not in days or days[day]['data_status'] != 'READY'
                or days[day]['history_basis'] != 'point_in_time'
                or any(days[day][key] is None for key in numeric_fields)]
        if gaps:
            audit['industry_data_gaps'][name] = gaps
            continue
        history = [days[date] for date in dates]
        ranks = [row['rank_pct_5'] for row in history]
        breadth = [row['above_ma20_ratio'] for row in history]
        compounded = (prod(1 + row['median_return_1'] / 100 for row in history) - 1) * 100
        if (ranks[0] >= ranks[1] >= ranks[2] and ranks[0] > ranks[2]
                and ranks[2] <= SECTOR_RANK_LIMIT
                and breadth[0] <= breadth[1] <= breadth[2] and breadth[0] < breadth[2]
                and breadth[2] >= SECTOR_MA20_MIN and compounded > 0
                and sum(row['relative_strength_1'] > 0 for row in history) >= 2):
            qualifying.append((ranks[2], name, history, round(compounded, 6)))

    audit['status'] = 'PARTIAL' if audit['industry_data_gaps'] else 'READY'
    members = defaultdict(list)
    for code in pool:
        members[strengths.get(code, {}).get('sw_l2_name')].append(code)
    audit['pool_codes_missing_industry'] = sorted(members.get(None, []) + members.get('', []))
    selected = set()
    for _, name, history, compounded in sorted(qualifying):
        eligible, exclusions = [], {}
        for code in sorted(members.get(name, [])):
            q = quant.get(code) or {}
            reason = excluded(code)
            if reason is None and not prebreak(q):
                reason = '缺当前有效突破前VCP'
            if reason is None and not has_qualified_impulse(q):
                reason = '缺有效推进贡献'
            if reason:
                exclusions[code] = reason
            else:
                eligible.append(code)
        eligible.sort(key=order)
        unmatched = [code for code in eligible if code not in existing]
        available_slots = max(0, SECTOR_MAX_TOTAL - len(selected))
        added = unmatched[:min(SECTOR_MAX_PER_INDUSTRY, available_slots)]
        selected.update(added)
        audit['industries'].append({
            'name': name, 'history': history, 'three_session_return': compounded,
            'eligible_codes': eligible, 'exclusions': exclusions,
            'overlap_codes': [code for code in eligible if code in existing],
            'added_codes': added,
        })
    return selected, audit


def build_preselection(pool_codes, strengths, quant, bloom, plans, *, analysis_date,
                       followups=None, deferred_codes=(), extra_codes=(), held_codes=(),
                       risk_rules=None, provenance=None, sector_history=None):
    pool, held = set(pool_codes), set(held_codes)
    followups, risk_rules = followups or {}, risk_rules or {}
    deferred, extra = set(deferred_codes), set(extra_codes)
    excluded = lambda code: regular_exclusion(code, pool, quant, bloom, held, risk_rules)
    raw = {
        '结构分': {c for c, q in quant.items() if prebreak(q)
                   and number(q.get('structure_score')) is not None
                   and number(q['structure_score']) >= SCORE_MIN},
        '当日买点': {c for c, q in quant.items() if q.get('setup_signal') in BUY_SIGNALS},
        '次日计划': set(plans),
        '成熟结构': {c for c, q in quant.items() if prebreak(q)
                     and q.get('structure_stage') in {'VCP_MATURE', 'VCP_TIGHT'}},
    }
    sources = {name: {c for c in codes if excluded(c) is None}
               for name, codes in raw.items()}
    quantitative = set().union(*sources.values())
    rps = select_pool_rps(pool, strengths)
    sources['RPS5'], sources['RPS20'] = set(rps['rps5_top10']), set(rps['rps20_top10'])
    rps_codes = set(rps['union_codes'])
    formal = quantitative | rps_codes
    supplements = set(followups) | deferred | extra
    component = set()
    for code in supplements - formal:
        q = quant.get(code) or {}
        details = q.get('structure_score_details') or {}
        score = number(q.get('structure_score'))
        positive_bonus = any((details.get(key) or {}).get('hit') is True
                             and (number((details.get(key) or {}).get('score')) or 0) > 0
                             for key in ('contraction_quality', 'terminal_micro'))
        if (excluded(code) is None and prebreak(q) and score is not None and score < SCORE_MIN
                and has_qualified_impulse(q) and positive_bonus):
            component.add(code)
    sources['分项补充'] = component
    original = formal | component

    def order(code):
        score = number((quant.get(code) or {}).get('structure_score'))
        return (score is None, -score if score is not None else 0, code)

    sector, sector_audit = select_sector_supplement(
        sector_history or {'trade_dates': [], 'reason': '未提供板块历史'},
        pool, strengths, quant, original, excluded, order)
    sources['板块延续补充'] = sector
    selected = original | sector
    universe = selected | supplements
    sector_order = [code for industry in sector_audit['industries'] for code in industry['added_codes']]

    records = []
    for code in sorted(universe, key=order):
        origins = [name for name, codes in sources.items() if code in codes]
        origins += [name for name, codes in [('跨日跟进', set(followups)),
                    ('暂缓复核', deferred), ('明确补充', extra)] if code in codes]
        groups = [name for name, codes in [('quantitative', quantitative),
                  ('component_supplement', component), ('rps', rps_codes),
                  ('sector_supplement', sector)] if code in codes]
        row = _fact_row(code, quant.get(code) or {}, bloom.get(code) or {}, plans.get(code) or [],
                        origins, groups, excluded(code), held, risk_rules)
        row['selected_for_chart'] = code in selected
        row['sector_name'] = strengths.get(code, {}).get('sw_l2_name') or None
        row['prior_followup'] = followups.get(code)
        records.append(row)
    chart_codes = [r['code'] for r in records
                   if r['code'] in original and r['route'] != 'HOLDING_REVIEW'] + sector_order
    return {
        'version': VERSION, 'analysis_date': analysis_date,
        'purpose': '量化预筛的看图对象，不是研究重点、买点或交易授权',
        'provenance': provenance or {},
        'groups': {'quantitative': sorted(quantitative), 'component_supplement': sorted(component),
                   'rps': sorted(rps_codes), 'sector_supplement': sorted(sector)},
        'source_codes': {k: sorted(v) for k, v in sources.items()},
        'source_exclusions': {name: {c: excluded(c) for c in sorted(codes) if excluded(c)}
                              for name, codes in raw.items()},
        'counts': {'pool': len(pool), 'quantitative': len(quantitative), 'rps': len(rps_codes),
                   'quantitative_rps_overlap': len(quantitative & rps_codes),
                   'component_supplement': len(component), 'sector_supplement': len(sector),
                   'selected': len(selected),
                   'chart_review': len(chart_codes), 'maintenance': len(universe - selected),
                   'universe': len(universe)},
        'rps': rps, 'sector_supplement': sector_audit, 'pool_missing_quant': sorted(pool - set(quant)),
        'chart_review_codes': chart_codes,
        'holding_review_codes': sorted(selected & held),
        'maintenance_codes': sorted(universe - selected, key=order),
        'records': records,
    }


def review_template(selection):
    return {'preselection_sha256': fingerprint(selection), 'reviews': [
        {key: code if key == 'code' else None for key in REVIEW_FIELDS}
        for code in selection['chart_review_codes']]}


def check_review(selection, review):
    errors, pending = [], []
    if review.get('preselection_sha256') != fingerprint(selection):
        errors.append('预筛包指纹不匹配')
    expected = selection['chart_review_codes']
    rows = review.get('reviews')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return {'complete': False, 'errors': errors + ['reviews必须是记录列表'], 'pending_codes': []}
    codes = [r.get('code') for r in rows]
    if codes != expected:
        errors.append('复核代码须按预筛顺序完整覆盖，不能遗漏、重复或加入未知代码')
    facts = {r['code']: r for r in selection['records']}
    focuses = []
    for row in rows:
        code = row.get('code')
        if set(row) - set(REVIEW_FIELDS):
            errors.append(f'{code}: 只允许填写人工字段，不得改写评分事实')
        if code not in facts or code not in expected:
            continue
        decision = row.get('decision')
        if decision not in DECISIONS:
            errors.append(f'{code}: 缺少有效分流结论')
            continue
        if decision != '重点研究' and row.get('priority_rank') is not None:
            errors.append(f'{code}: 仅重点研究填写最终顺位')
        if not isinstance(row.get('reason'), str) or not row['reason'].strip():
            errors.append(f'{code}: 缺少分流依据')
        if decision == '待核':
            pending.append(code)
            continue
        if facts[code]['route'] == 'DATA_REVIEW':
            errors.append(f'{code}: 数据问题尚未解决，须标待核')
        for key in ('wave_start', 'price_volume', 'support_supply'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f'{code}: 缺少{key}')
        try:
            start = normalize_date_arg(row.get('wave_start'))
            if start > selection['analysis_date']:
                errors.append(f'{code}: 推进起点晚于分析日')
        except (TypeError, ValueError):
            errors.append(f'{code}: 推进起点日期无效')
        if decision == '重点研究':
            if facts[code]['route'] == 'RESTRICTED_REVIEW':
                errors.append(f'{code}: 硬风险受限对象不得升级重点')
            focuses.append(row)
    ranks = [row.get('priority_rank') for row in focuses]
    if any(rank is not None for rank in ranks):
        if (any(not isinstance(rank, int) or isinstance(rank, bool) for rank in ranks)
                or sorted(ranks) != list(range(1, len(ranks) + 1))):
            errors.append('最终重点顺位须全部填写且从1连续、不重复')
        else:
            focuses.sort(key=lambda row: row['priority_rank'])
    nonfocus_scores = [facts[r['code']]['structure_score'] for r in rows
                       if r.get('code') in facts and r.get('decision') in DECISIONS - {'重点研究'}
                       and facts[r['code']]['structure_score'] is not None]
    for index, row in enumerate(focuses):
        score = facts[row['code']]['structure_score']
        later_scores = [facts[r['code']]['structure_score'] for r in focuses[index + 1:]
                        if facts[r['code']]['structure_score'] is not None]
        passed_scores = nonfocus_scores + later_scores
        if passed_scores and (score is None or max(passed_scores) > score):
            if not isinstance(row.get('comparison_reason'), str) or not row['comparison_reason'].strip():
                errors.append(f"{row['code']}: 低分提前须解释与未选高分对象的比较依据")
    return {'complete': not errors and not pending, 'errors': errors,
            'pending_codes': pending, 'focus_codes': [r['code'] for r in focuses]}


def _csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _codes(value):
    codes = {c.strip().strip('="') for c in value.split(',') if c.strip()}
    if any(len(c) != 6 or not c.isdigit() for c in codes):
        raise ValueError('股票代码必须为6位数字')
    return codes


def _trim_quant(q):
    keys = ('code', 'name', 'run_date', 'strategy_version', 'structure_type', 'structure_stage',
            'model2_include', 'structure_valid', 'structure_score', 'structure_scoring_status',
            'post_breakout_state', 'structure_breakout_date', 'structure_breakout_score',
            'contraction_group', 'setup_signal', 'setup_quality', 'structure_risk_flags',
            'structure_risk_score', 'adjustment_status')
    result = {k: q.get(k) for k in keys}
    details = q.get('structure_score_details') or {}
    result['structure_score_details'] = {k: details.get(k) for k in (
        'status', 'total', 'components', 'impulse', 'terminal_micro', 'contraction_quality')}
    return result


def load_sector_history(root, date):
    market_db = root / 'cache/market_data/market_data.sqlite'
    state_db = root / 'cache/market_regime/market_regime.sqlite'
    result = {'trade_dates': [], 'rows': [], 'reason': None}
    missing = [str(path.relative_to(root)) for path in (market_db, state_db) if not path.is_file()]
    if missing:
        result['reason'] = '缺本地板块窗口输入: ' + ', '.join(missing)
        return result
    try:
        with closing(sqlite3.connect(market_db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            dates = [row[0] for row in conn.execute(
                """SELECT trade_date FROM daily_bars WHERE code='IDX:000001' AND trade_date<=?
                   ORDER BY trade_date DESC LIMIT ?""", (date, SECTOR_SESSIONS))][::-1]
        result['trade_dates'] = dates
        if len(dates) != SECTOR_SESSIONS or dates[-1] != date:
            result['reason'] = '上证综指日线缺分析日或不足三个交易日'
            return result
        with closing(sqlite3.connect(state_db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            result['rows'] = [dict(row) for row in conn.execute(
                """SELECT trade_date,block_name,member_count,rank_5,rank_pct_5,rank_20,
                          above_ma20_ratio,advance_ratio,relative_strength_1,median_return_1,
                          sector_phase,volume_activity,data_status,history_basis
                   FROM sector_daily_metrics WHERE block_kind='industry_sw_l2'
                   AND trade_date BETWEEN ? AND ? ORDER BY block_name,trade_date""",
                (dates[0], dates[-1])) if row['trade_date'] in dates]
        if not result['rows']:
            result['reason'] = '窗口缺申万二级行业记录'
    except sqlite3.Error as exc:
        result['reason'] = f'板块窗口读取失败: {exc}'
    return result


def load_inputs(root, date, followups_path=None, deferred_codes=None, extra_codes=()):
    suffix = date.replace('-', '')[2:]
    pool_path = root / f'pool/pool_{suffix}.csv'
    strength_path = root / f'market/stock_strength_{suffix}.csv'
    risk_path = root / 'strategies/04-bloom.json'
    if followups_path is not None and not followups_path.is_file():
        raise FileNotFoundError(f'指定的跟踪输入不存在: {followups_path}')
    followups_path = followups_path or root / 'daily_research/current_research_followups.json'
    files = [pool_path, strength_path, risk_path]
    followups_data = {}
    if followups_path.is_file():
        files.append(followups_path)
        followups_data = json.loads(followups_path.read_text())
        followups_data = followups_data.get('followups', followups_data)
    market_date = followups_data.get('market_data_as_of')
    if market_date and market_date > date:
        raise ValueError('跟踪表含分析日之后的行情，请显式提供对应历史快照')
    pool = {r['股票代码'].strip('="') for r in _csv_rows(pool_path)}
    strength_rows = _csv_rows(strength_path)
    if any(r.get('date') != date for r in strength_rows):
        raise ValueError('强度文件日期不一致')
    holdings_path = root / 'position/current_holdings_discussion.csv'
    held = set()
    if holdings_path.is_file():
        files.append(holdings_path)
        held = {r['code'] for r in _csv_rows(holdings_path) if (number(r.get('shares')) or 0) > 0}
    followups = {r['code']: r for r in followups_data.get('items', [])}
    if len(followups) != len(followups_data.get('items', [])):
        raise ValueError('跟踪输入存在重复代码')
    deferred = (followups_data.get('last_reviewed_deferred_codes', [])
                if deferred_codes is None else deferred_codes)
    risk = json.loads(risk_path.read_text())['risk_rules']
    db = root / 'cache/strategy/strategy_data.sqlite'
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('BEGIN')
        ids = dict(conn.execute('select module,document_id from current_documents where trade_date=?', (date,)))
        required = ('quant', 'bloom', 'signal_plan')
        if any(k not in ids for k in required):
            raise ValueError('分析日Quant/Bloom/Signal Plan权威文档不齐，不回退其他日期')
        documents = {}
        for module in required:
            row = conn.execute('select module,trade_date,payload_hash,strategy_version from strategy_documents where document_id=?', (ids[module],)).fetchone()
            if not row or row[:2] != (module, date):
                raise ValueError('权威文档日期或模块不一致')
            documents[module] = {'document_id': ids[module], 'trade_date': date,
                                 'payload_hash': row[2], 'strategy_version': row[3]}
        values = {}
        for table, module in [('vcp_structure_snapshots', 'quant'), ('bloom_daily_snapshots', 'bloom')]:
            values[module] = {}
            for row_date, code, payload in conn.execute(f'select trade_date,code,payload_json from {table} where document_id=?', (ids[module],)):
                if row_date != date:
                    raise ValueError('策略快照日期不一致')
                q = json.loads(payload)
                if module == 'quant' and (q.get('code') != code or q.get('run_date', date) != date):
                    raise ValueError('Quant内容代码或运行日期不一致')
                values[module][code] = _trim_quant(q) if module == 'quant' else q
        plans = defaultdict(list)
        for code, payload in conn.execute('select code,payload_json from signal_plans where document_id=?', (ids['signal_plan'],)):
            plans[code].append(json.loads(payload))
    sector_history = load_sector_history(root, date)
    return {
        'pool_codes': pool, 'strengths': {r['code']: r for r in strength_rows},
        'quant': values['quant'], 'bloom': values['bloom'], 'plans': dict(plans),
        'analysis_date': date, 'followups': followups, 'deferred_codes': deferred,
        'extra_codes': extra_codes, 'held_codes': held, 'risk_rules': risk,
        'sector_history': sector_history,
        'provenance': {'documents': documents, 'followups_updated_on': followups_data.get('updated_on'),
                       'sector_history_sha256': fingerprint(sector_history),
                       'input_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}},
    }


def write_package(selection, output):
    names = ('preselection.json', 'facts.csv', 'review.json')
    if any((output / name).exists() for name in names):
        raise FileExistsError('输出包已存在，请指定新目录，保留历史版本')
    output.mkdir(parents=True, exist_ok=True)
    for name, value in [('preselection.json', selection), ('review.json', review_template(selection))]:
        with (output / name).open('x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
    fields = ('code', 'name', 'selected_for_chart', 'sources', 'groups', 'route', 'sector_name',
              'structure_score', 'score_context', 'structure_stage', 'post_breakout_state',
              'score_components', 'impulse', 'contraction_quality', 'terminal_micro',
              'setup_signal', 'setup_quality', 'plans', 'score_gaps', 'regular_exclusion')
    with (output / 'facts.csv').open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in selection['records']:
            writer.writerow({k: json.dumps(row[k], ensure_ascii=False, sort_keys=True)
                             if isinstance(row[k], (dict, list)) else row[k] for k in fields})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--date', default=expected_trade_date())
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--followups', type=Path)
    prepare.add_argument('--deferred-codes')
    prepare.add_argument('--extra-codes', default='')
    check = commands.add_parser('check-review')
    check.add_argument('--selection', type=Path, required=True)
    check.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'check-review':
        result = check_review(json.loads(args.selection.read_text()), json.loads(args.review.read_text()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['complete'] else 2
    data = load_inputs(Path(PROJECT_ROOT), normalize_date_arg(args.date), args.followups,
                       None if args.deferred_codes is None else _codes(args.deferred_codes),
                       _codes(args.extra_codes))
    selection = build_preselection(**data)
    write_package(selection, args.output)
    print(json.dumps({'counts': selection['counts'], 'output': str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
