"""Capital data facade: SW mapping, source queries, validation and local cache."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from scripts.data.capital_data_sources import (
    EastmoneyIndustrySource,
    MiaoxiangCapitalSource,
    RequestBudgetExceeded,
    contract_field_matches_metric,
    normalize_stock_code,
    source_hash,
    utc_timestamp,
)
from scripts.data.capital_data_store import DB_PATH, connect_capital_db
from scripts.data.market_data_store import DB_PATH as MARKET_DB_PATH
from scripts.shared import expected_trade_date, normalize_date_arg
from scripts.strategy_config import load_strategy_config


def _quality(combined_coverage: float, combined_core: float, config: dict) -> str:
    levels = config["quality"]
    if combined_coverage >= levels["high_coverage"] and combined_core >= levels["high_core_coverage"]:
        return "high"
    if combined_coverage >= levels["usable_coverage"]:
        return "usable"
    if combined_coverage >= levels["weak_coverage"]:
        return "weak"
    return "unavailable"


def sw_level_from_code(sw_code: str) -> int:
    length = len(str(sw_code))
    if length == 3:
        return 1
    if length == 5:
        return 2
    raise ValueError(f"无法识别申万行业层级: {sw_code}")


def build_sector_mappings(sw_industries: dict, fortune_boards: dict, amount_by_code: dict, config: dict) -> list[dict]:
    """Build deterministic SW L1 to Eastmoney industry mappings from constituent overlap."""
    score_weights = config["score_weights"]
    maximum = int(config["maximum_mapped_blocks"])
    minimum_purity = float(config["minimum_board_purity"])
    minimum_increment = float(config["minimum_incremental_coverage"])
    target_coverage = float(config["target_combined_coverage"])
    core_count = int(config["core_stock_count"])
    output = []

    for sw_code, sw in sorted(sw_industries.items()):
        sw_members = set(sw["members"])
        weights = {code: max(float(amount_by_code.get(code) or 0.0), 0.0) for code in sw_members}
        if not any(weights.values()):
            weights = {code: 1.0 for code in sw_members}
        sw_total = sum(weights.values()) or 1.0
        core = sorted(sw_members, key=lambda code: (weights.get(code, 0.0), code), reverse=True)[:core_count]
        candidates = []

        for bk_code, board in sorted(fortune_boards.items()):
            bk_members = set(board["members"])
            overlap = sw_members & bk_members
            if not overlap:
                continue
            overlap_amount = sum(weights.get(code, 0.0) for code in overlap)
            board_weights = [max(float(amount_by_code.get(code) or 0.0), 0.0) for code in bk_members]
            board_total = sum(board_weights) or float(len(bk_members) or 1)
            if not sum(board_weights):
                overlap_for_purity = float(len(overlap))
            else:
                overlap_for_purity = sum(max(float(amount_by_code.get(code) or 0.0), 0.0) for code in overlap)
            coverage = overlap_amount / sw_total
            purity = overlap_for_purity / board_total
            core_coverage = len(set(core) & overlap) / float(len(core) or 1)
            jaccard = len(overlap) / float(len(sw_members | bk_members) or 1)
            score = (
                score_weights["coverage"] * coverage
                + score_weights["purity"] * purity
                + score_weights["core_coverage"] * core_coverage
            )
            candidate = {
                "bk_code": bk_code,
                "bk_name": board["name"],
                "members": bk_members,
                "coverage": coverage,
                "purity": purity,
                "core_coverage": core_coverage,
                "jaccard": jaccard,
                "mapping_score": score,
            }
            if purity >= minimum_purity:
                candidates.append(candidate)

        if not candidates:
            output.append({
                "sw_code": sw_code, "sw_name": sw["name"], "bk_code": None, "bk_name": None,
                "mapping_rank": 1, "coverage": 0.0, "incremental_coverage": 0.0,
                "combined_coverage": 0.0, "purity": 0.0, "core_coverage": 0.0,
                "jaccard": 0.0, "mapping_score": 0.0, "mapping_weight": 0.0,
                "mapping_quality": "unavailable",
            })
            continue

        candidates.sort(key=lambda row: (row["mapping_score"], row["coverage"], row["bk_code"]), reverse=True)
        selected, covered = [], set()
        while candidates and len(selected) < maximum:
            if not selected:
                candidate = candidates.pop(0)
            else:
                candidate = max(
                    candidates,
                    key=lambda row: sum(weights.get(code, 0.0) for code in (sw_members & row["members"]) - covered),
                )
                candidates.remove(candidate)
            overlap = sw_members & candidate["members"]
            incremental_members = overlap - covered
            incremental = sum(weights.get(code, 0.0) for code in incremental_members) / sw_total
            if selected and incremental < minimum_increment:
                break
            selected.append(dict(candidate, incremental_coverage=incremental))
            covered.update(overlap)
            combined = sum(weights.get(code, 0.0) for code in covered) / sw_total
            if combined >= target_coverage:
                break

        combined_coverage = sum(weights.get(code, 0.0) for code in covered) / sw_total
        combined_core = len(set(core) & covered) / float(len(core) or 1)
        quality = _quality(combined_coverage, combined_core, config)
        incremental_total = sum(row["incremental_coverage"] for row in selected) or 1.0
        for rank, row in enumerate(selected, 1):
            output.append({
                "sw_code": sw_code,
                "sw_name": sw["name"],
                "bk_code": row["bk_code"],
                "bk_name": row["bk_name"],
                "mapping_rank": rank,
                "coverage": row["coverage"],
                "incremental_coverage": row["incremental_coverage"],
                "combined_coverage": combined_coverage,
                "purity": row["purity"],
                "core_coverage": row["core_coverage"],
                "jaccard": row["jaccard"],
                "mapping_score": row["mapping_score"],
                "mapping_weight": row["incremental_coverage"] / incremental_total,
                "mapping_quality": quality,
            })
    return output


class CapitalDataService:
    """Public data interface consumed by the later capital observation module."""

    def __init__(
        self,
        config: dict | None = None,
        market_db_path: Path | str = MARKET_DB_PATH,
        capital_db_path: Path | str = DB_PATH,
        board_source=None,
        capital_source=None,
    ):
        self.config = config or load_strategy_config("capital-data.json")[0]
        self.market_db_path = Path(market_db_path)
        self.capital_db_path = Path(capital_db_path)
        eastmoney = self.config["eastmoney"]
        self.board_source = board_source or EastmoneyIndustrySource(
            page_size=eastmoney["page_size"],
            timeout=eastmoney["timeout_seconds"],
            maximum_retries=eastmoney["maximum_retries"],
        )
        self.capital_source = capital_source or MiaoxiangCapitalSource()

    def ensure_sector_mapping(
        self, as_of: str | None = None, sw_level: int = 1, force_rebuild: bool = False
    ) -> dict:
        target = normalize_date_arg(as_of) if as_of else expected_trade_date()
        sw_level = int(sw_level)
        level_config = self.config["mapping"]["levels"].get(str(sw_level))
        if not level_config:
            raise ValueError(f"不支持的申万行业层级: {sw_level}")
        fortune_level = int(level_config["fortune_level"])
        with connect_capital_db(self.capital_db_path) as conn:
            current = self._latest_mapping_version(conn, target, sw_level)
            if current and not force_rebuild:
                return self._mapping_summary(conn, current["mapping_version"], reused=True)

        fortune_snapshot = self._ensure_board_snapshot(target, fortune_level, force_rebuild)
        sw_snapshot, sw_industries, amounts = self._load_sw_inputs(target, sw_level)
        fortune_boards = self._load_fortune_inputs(fortune_snapshot, fortune_level)
        mappings = build_sector_mappings(sw_industries, fortune_boards, amounts, self.config["mapping"])
        created_at = utc_timestamp()
        version = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        detail = {
            "sw_level": sw_level,
            "fortune_level": fortune_level,
            "sw_industry_count": len(sw_industries),
            "fortune_board_count": len(fortune_boards),
            "mapping_row_count": len(mappings),
        }
        with connect_capital_db(self.capital_db_path) as conn:
            previous = conn.execute(
                """SELECT mapping_version,valid_from FROM mapping_versions
                   WHERE sw_level=? AND valid_to IS NULL ORDER BY created_at DESC LIMIT 1""",
                (sw_level,),
            ).fetchone()
            if previous:
                if previous["valid_from"] < target:
                    valid_to = (datetime.strptime(target, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
                    conn.execute(
                        "UPDATE mapping_versions SET valid_to=? WHERE mapping_version=?",
                        (valid_to, previous["mapping_version"]),
                    )
                else:
                    conn.execute(
                        "UPDATE mapping_versions SET status='SUPERSEDED',valid_to=? WHERE mapping_version=?",
                        (target, previous["mapping_version"]),
                    )
            conn.execute(
                "INSERT INTO mapping_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version, sw_level, fortune_level, sw_snapshot, fortune_snapshot,
                    target, target, None, "READY", json.dumps(detail, ensure_ascii=False), created_at,
                ),
            )
            conn.executemany(
                """INSERT INTO sector_mappings VALUES(
                   :mapping_version,:sw_level,:fortune_level,:sw_code,:sw_name,:bk_code,:bk_name,:mapping_rank,:coverage,
                   :incremental_coverage,:combined_coverage,:purity,:core_coverage,:jaccard,
                   :mapping_score,:mapping_weight,:mapping_quality)""",
                [dict(row, mapping_version=version, sw_level=sw_level, fortune_level=fortune_level) for row in mappings],
            )
            conn.commit()
            return self._mapping_summary(conn, version, reused=False)

    def mapping_status(self, as_of: str | None = None, sw_level: int = 1) -> dict:
        target = normalize_date_arg(as_of) if as_of else expected_trade_date()
        with connect_capital_db(self.capital_db_path) as conn:
            current = self._latest_mapping_version(conn, target, int(sw_level))
            if not current:
                return {"sw_level": int(sw_level), "as_of": target, "status": "NOT_READY"}
            return self._mapping_summary(conn, current["mapping_version"], reused=True)

    def estimate_requests(
        self,
        sw_codes: list[str],
        stock_codes: list[str],
        start_date: str,
        end_date: str,
    ) -> dict:
        start, end = normalize_date_arg(start_date), normalize_date_arg(end_date)
        sector_requests, missing_mappings = 0, []
        for sw_code in dict.fromkeys(sw_codes):
            level = sw_level_from_code(sw_code)
            status = self.mapping_status(end, level)
            if status["status"] != "READY":
                missing_mappings.append(sw_code)
                continue
            with connect_capital_db(self.capital_db_path) as conn:
                mappings = conn.execute(
                    """SELECT bk_code FROM sector_mappings
                       WHERE mapping_version=? AND sw_code=? AND mapping_quality IN ('high','usable')""",
                    (status["mapping_version"], sw_code),
                ).fetchall()
            for mapping in mappings:
                if not self._capital_range_cached(
                    "sector_capital", "bk_code", mapping["bk_code"], start, end
                ):
                    sector_requests += 1
        pending_stocks = [
            code for code in dict.fromkeys(normalize_stock_code(code) for code in stock_codes)
            if not self._capital_range_cached("stock_capital", "code", code, start, end)
        ]
        batch_size = int(self.config["miaoxiang"]["maximum_entities_per_query"])
        stock_requests = (len(pending_stocks) + batch_size - 1) // batch_size
        return {
            "start_date": start,
            "end_date": end,
            "sector_requests": sector_requests,
            "stock_requests": stock_requests,
            "estimated_total": sector_requests + stock_requests,
            "pending_stocks": len(pending_stocks),
            "missing_mappings": missing_mappings,
            "note": "不含接口静默遗漏后的单股补查",
        }

    def fetch_sector_capital(
        self, sw_code: str, start_date: str, end_date: str, refresh: bool = False
    ) -> dict:
        start, end = normalize_date_arg(start_date), normalize_date_arg(end_date)
        if start > end:
            raise ValueError("start_date must not be after end_date")
        sw_level = sw_level_from_code(sw_code)
        mapping_summary = self.ensure_sector_mapping(as_of=end, sw_level=sw_level)
        version = mapping_summary["mapping_version"]
        with connect_capital_db(self.capital_db_path) as conn:
            mappings = [dict(row) for row in conn.execute(
                "SELECT * FROM sector_mappings WHERE mapping_version=? AND sw_code=? ORDER BY mapping_rank",
                (version, sw_code),
            )]
        if not mappings:
            return {"sw_code": sw_code, "mapping_version": version, "status": "mapping_missing", "rows": []}
        quality = mappings[0]["mapping_quality"]
        if quality not in {"high", "usable"} or not mappings[0].get("bk_code"):
            return {
                "sw_code": sw_code, "mapping_version": version, "mapping_quality": quality,
                "mapped_blocks": mappings, "status": "mapping_unreliable", "rows": [],
            }

        errors = []
        budget_exhausted = False
        for mapping in mappings:
            bk_code = mapping["bk_code"]
            if refresh or not self._capital_range_cached("sector_capital", "bk_code", bk_code, start, end):
                try:
                    rows, contracts, query, error = self.capital_source.fetch_sector(
                        bk_code, mapping["bk_name"], start, end, mapping["fortune_level"]
                    )
                except RequestBudgetExceeded as exc:
                    errors.append({"bk_code": bk_code, "error": str(exc)})
                    budget_exhausted = True
                    break
                if error:
                    errors.append({"bk_code": bk_code, "error": error})
                    continue
                if not rows or not any(row.get("main_net_inflow") is not None for row in rows):
                    errors.append({"bk_code": bk_code, "error": "缺少主力净流入字段"})
                    continue
                contract_error = self._contract_error("sector", bk_code, contracts)
                if contract_error:
                    errors.append({"bk_code": bk_code, "error": contract_error})
                    continue
                self._save_capital_rows("sector_capital", "bk_code", bk_code, rows, query)
                self._save_contracts("sector", bk_code, contracts)

        components_by_date = defaultdict(list)
        with connect_capital_db(self.capital_db_path) as conn:
            for mapping in mappings:
                cached = conn.execute(
                    "SELECT trade_date,metrics_json FROM sector_capital WHERE bk_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (mapping["bk_code"], start, end),
                ).fetchall()
                for row in cached:
                    metrics = json.loads(row["metrics_json"])
                    components_by_date[row["trade_date"]].append({
                        "bk_code": mapping["bk_code"], "bk_name": mapping["bk_name"],
                        "weight": mapping["mapping_weight"], "purity": mapping["purity"], "metrics": metrics,
                    })
        rows = []
        for trade_date, components in sorted(components_by_date.items()):
            numerator, denominator = 0.0, 0.0
            for component in components:
                amount = component["metrics"].get("amount")
                net = component["metrics"].get("main_net_inflow")
                if amount and net is not None:
                    effective_weight = component["weight"] * component["purity"]
                    numerator += effective_weight * (net / amount)
                    denominator += effective_weight
            rows.append({
                "trade_date": trade_date,
                "main_net_inflow_ratio": numerator / denominator if denominator else None,
                "components": components,
            })
        status = (
            "budget_exhausted" if budget_exhausted
            else "complete" if rows and not errors
            else "partial" if rows else "error"
        )
        return {
            "sw_code": sw_code, "mapping_version": version, "mapping_quality": quality,
            "mapped_blocks": mappings, "rows": rows, "errors": errors, "status": status,
        }

    def fetch_stock_capital(
        self, codes: list[str], start_date: str, end_date: str, refresh: bool = False
    ) -> dict:
        start, end = normalize_date_arg(start_date), normalize_date_arg(end_date)
        if start > end:
            raise ValueError("start_date must not be after end_date")
        requested = list(dict.fromkeys(normalize_stock_code(code) for code in codes))
        names = self._stock_names(requested)
        maximum = int(self.config["miaoxiang"]["maximum_entities_per_query"])
        pending = [
            code for code in requested
            if refresh or not self._capital_range_cached("stock_capital", "code", code, start, end)
        ]
        errors = []
        budget_exhausted = False
        for offset in range(0, len(pending), maximum):
            batch = pending[offset:offset + maximum]
            entities = [{"code": code, "name": names.get(code, "")} for code in batch]
            try:
                rows_by_code, missing, contracts, query, error = self.capital_source.fetch_stocks(
                    entities, start, end
                )
            except RequestBudgetExceeded as exc:
                errors.append({"codes": batch, "error": str(exc)})
                budget_exhausted = True
                break
            if error:
                errors.append({"codes": batch, "error": error})
                continue
            for code, rows in rows_by_code.items():
                if not rows or not any(row.get("main_net_inflow") is not None for row in rows):
                    errors.append({"codes": [code], "error": "缺少主力净流入字段"})
                    continue
                contract_error = self._contract_error("stock", code, contracts)
                if contract_error:
                    errors.append({"codes": [code], "error": contract_error})
                    continue
                self._save_capital_rows("stock_capital", "code", code, rows, query)
                self._save_contracts("stock", code, contracts)
            for code in sorted(missing):
                single = [{"code": code, "name": names.get(code, "")}]
                try:
                    retry_rows, retry_missing, retry_contracts, retry_query, retry_error = self.capital_source.fetch_stocks(
                        single, start, end
                    )
                except RequestBudgetExceeded as exc:
                    errors.append({"codes": [code], "error": str(exc)})
                    budget_exhausted = True
                    break
                if retry_error or retry_missing:
                    errors.append({"codes": [code], "error": retry_error or "妙想遗漏证券"})
                    continue
                contract_error = self._contract_error("stock", code, retry_contracts)
                if contract_error:
                    errors.append({"codes": [code], "error": contract_error})
                    continue
                self._save_capital_rows("stock_capital", "code", code, retry_rows.get(code, []), retry_query)
                self._save_contracts("stock", code, retry_contracts)
            if budget_exhausted:
                break

        result = {}
        with connect_capital_db(self.capital_db_path) as conn:
            for code in requested:
                cached = conn.execute(
                    "SELECT trade_date,metrics_json FROM stock_capital WHERE code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (code, start, end),
                ).fetchall()
                rows = [dict(trade_date=row["trade_date"], **json.loads(row["metrics_json"])) for row in cached]
                result[code] = {"rows": rows, "status": "complete" if rows else "unavailable"}
        status = (
            "budget_exhausted" if budget_exhausted
            else "complete" if result and all(item["rows"] for item in result.values()) and not errors
            else "partial"
        )
        return {"start_date": start, "end_date": end, "stocks": result, "errors": errors, "status": status}

    def _ensure_board_snapshot(self, target: str, fortune_level: int, force: bool) -> str:
        with connect_capital_db(self.capital_db_path) as conn:
            if not force:
                row = conn.execute(
                    """SELECT snapshot_date FROM board_snapshot_runs
                       WHERE status='READY' AND fortune_level=? AND snapshot_date<=?
                       ORDER BY snapshot_date DESC LIMIT 1""",
                    (fortune_level, target),
                ).fetchone()
                if row:
                    return row["snapshot_date"]
        current_target = expected_trade_date()
        if target < current_target:
            raise RuntimeError(
                f"缺少 {target} 的财富通行业快照；不得用 {current_target} 当前成分伪装历史映射"
            )
        catalog = self.board_source.fetch_catalog(fortune_level)
        with connect_capital_db(self.capital_db_path) as conn:
            if force:
                conn.execute(
                    "DELETE FROM fortune_industry_members WHERE snapshot_date=? AND fortune_level=?",
                    (target, fortune_level),
                )
                conn.execute(
                    "DELETE FROM fortune_industry_boards WHERE snapshot_date=? AND fortune_level=?",
                    (target, fortune_level),
                )
            conn.execute(
                """INSERT OR REPLACE INTO board_snapshot_runs
                   VALUES(?,?,?,?,?,?,?,?)""",
                (target, fortune_level, "PARTIAL", len(catalog), 0, None, json.dumps({}, ensure_ascii=False), utc_timestamp()),
            )
            conn.commit()

        for board in catalog:
            with connect_capital_db(self.capital_db_path) as conn:
                cached = conn.execute(
                    """SELECT member_count FROM fortune_industry_boards
                       WHERE snapshot_date=? AND fortune_level=? AND bk_code=?""",
                    (target, fortune_level, board["bk_code"]),
                ).fetchone()
                if cached and cached["member_count"] > 0:
                    continue
            board_members = self.board_source.fetch_members(board["bk_code"])
            member_hash = source_hash(board_members)
            members = [
                (target, fortune_level, board["bk_code"], item["stock_code"], item.get("stock_name", ""))
                for item in board_members
            ]
            with connect_capital_db(self.capital_db_path) as conn:
                conn.execute(
                    "DELETE FROM fortune_industry_members WHERE snapshot_date=? AND fortune_level=? AND bk_code=?",
                    (target, fortune_level, board["bk_code"]),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO fortune_industry_boards VALUES(?,?,?,?,?,?)",
                    (target, fortune_level, board["bk_code"], board["bk_name"], len(board_members), member_hash),
                )
                conn.executemany("INSERT INTO fortune_industry_members VALUES(?,?,?,?,?)", members)
                conn.commit()

        with connect_capital_db(self.capital_db_path) as conn:
            boards = [dict(row) for row in conn.execute(
                """SELECT bk_code,bk_name,member_count,source_hash FROM fortune_industry_boards
                   WHERE snapshot_date=? AND fortune_level=? ORDER BY bk_code""",
                (target, fortune_level),
            )]
            if len(boards) != len(catalog):
                raise RuntimeError(
                    f"财富通{fortune_level}级行业快照不完整: {len(boards)}/{len(catalog)}"
                )
            digest = source_hash(boards)
            member_count = conn.execute(
                """SELECT COUNT(*) FROM fortune_industry_members
                   WHERE snapshot_date=? AND fortune_level=?""",
                (target, fortune_level),
            ).fetchone()[0]
            conn.execute(
                """INSERT OR REPLACE INTO board_snapshot_runs VALUES(?,?,?,?,?,?,?,?)""",
                (
                    target, fortune_level, "READY", len(boards), member_count,
                    digest, json.dumps({}, ensure_ascii=False), utc_timestamp(),
                ),
            )
            conn.commit()
        return target

    def _load_sw_inputs(self, target: str, sw_level: int = 1):
        if not self.market_db_path.exists():
            raise FileNotFoundError(f"共享市场数据库不存在: {self.market_db_path}")
        conn = sqlite3.connect(self.market_db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT MAX(snapshot_date) AS value FROM stock_industries WHERE snapshot_date<=?", (target,)
            ).fetchone()
            snapshot = row["value"] if row else None
            if not snapshot:
                raise RuntimeError(f"{target} 之前没有申万行业快照")
            definitions = {
                row["industry_code"]: row["industry_name"] for row in conn.execute(
                    """SELECT industry_code,industry_name FROM industry_definitions
                       WHERE snapshot_date=? AND industry_system='sw' AND LENGTH(industry_code)=?""",
                    (snapshot, sw_level * 2 + 1),
                )
            }
            industries = {code: {"name": name, "members": set()} for code, name in definitions.items()}
            for item in conn.execute(
                "SELECT code,sw_industry_code FROM stock_industries WHERE snapshot_date=? AND sw_industry_code IS NOT NULL",
                (snapshot,),
            ):
                industry_code = str(item["sw_industry_code"])[:sw_level * 2 + 1]
                if industry_code in industries:
                    industries[industry_code]["members"].add(item["code"])
            window = int(self.config["mapping"]["amount_window"])
            dates = [row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                (target, window),
            )]
            if not dates:
                raise RuntimeError(f"{target} 之前没有日线成交额")
            placeholders = ",".join("?" for _ in dates)
            amounts = {row["code"]: float(row["avg_amount"] or 0.0) for row in conn.execute(
                f"SELECT code,AVG(COALESCE(amount,0)) AS avg_amount FROM daily_bars WHERE trade_date IN ({placeholders}) GROUP BY code",
                dates,
            )}
            return snapshot, industries, amounts
        finally:
            conn.close()

    def _load_fortune_inputs(self, snapshot: str, fortune_level: int = 1) -> dict:
        with connect_capital_db(self.capital_db_path) as conn:
            boards = {row["bk_code"]: {"name": row["bk_name"], "members": set()} for row in conn.execute(
                """SELECT bk_code,bk_name FROM fortune_industry_boards
                   WHERE snapshot_date=? AND fortune_level=?""",
                (snapshot, fortune_level),
            )}
            for row in conn.execute(
                """SELECT bk_code,stock_code FROM fortune_industry_members
                   WHERE snapshot_date=? AND fortune_level=?""",
                (snapshot, fortune_level),
            ):
                if row["bk_code"] in boards:
                    boards[row["bk_code"]]["members"].add(row["stock_code"])
            return boards

    @staticmethod
    def _latest_mapping_version(conn, target: str, sw_level: int = 1):
        return conn.execute(
            """SELECT * FROM mapping_versions WHERE status='READY' AND sw_level=? AND valid_from<=?
               AND (valid_to IS NULL OR valid_to>=?) ORDER BY created_at DESC LIMIT 1""",
            (sw_level, target, target),
        ).fetchone()

    @staticmethod
    def _mapping_summary(conn, version: str, reused: bool) -> dict:
        header = conn.execute("SELECT * FROM mapping_versions WHERE mapping_version=?", (version,)).fetchone()
        counts = {row["mapping_quality"]: row["total"] for row in conn.execute(
            "SELECT mapping_quality,COUNT(DISTINCT sw_code) AS total FROM sector_mappings WHERE mapping_version=? GROUP BY mapping_quality",
            (version,),
        )}
        return {
            "mapping_version": version,
            "sw_level": header["sw_level"],
            "fortune_level": header["fortune_level"],
            "as_of": header["as_of"],
            "sw_snapshot_date": header["sw_snapshot_date"],
            "fortune_snapshot_date": header["fortune_snapshot_date"],
            "quality_counts": counts,
            "reused": reused,
            "status": header["status"],
        }

    def _capital_range_cached(self, table: str, key_name: str, key_value: str, start: str, end: str) -> bool:
        expected = self._expected_trade_dates(start, end)
        if not expected:
            return False
        with connect_capital_db(self.capital_db_path) as conn:
            actual = {row[0] for row in conn.execute(
                f"SELECT trade_date FROM {table} WHERE {key_name}=? AND trade_date BETWEEN ? AND ?",
                (key_value, start, end),
            )}
        return expected.issubset(actual)

    def _expected_trade_dates(self, start: str, end: str) -> set[str]:
        if not self.market_db_path.exists():
            return set()
        conn = sqlite3.connect(self.market_db_path)
        try:
            return {row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date BETWEEN ? AND ?", (start, end)
            )}
        finally:
            conn.close()

    def _stock_names(self, codes: list[str]) -> dict:
        if not codes or not self.market_db_path.exists():
            return {}
        conn = sqlite3.connect(self.market_db_path)
        try:
            placeholders = ",".join("?" for _ in codes)
            return {row[0]: row[1] for row in conn.execute(
                f"SELECT code,name FROM securities WHERE code IN ({placeholders})", codes
            )}
        finally:
            conn.close()

    def _save_capital_rows(self, table: str, key_name: str, key_value: str, rows: list[dict], query: str) -> None:
        now = utc_timestamp()
        with connect_capital_db(self.capital_db_path) as conn:
            for row in rows:
                trade_date = row.get("trade_date")
                if not trade_date:
                    continue
                incoming = {
                    key: value for key, value in row.items()
                    if key not in {"trade_date", key_name, "bk_code", "code"}
                }
                existing = conn.execute(
                    f"SELECT metrics_json FROM {table} WHERE {key_name}=? AND trade_date=?",
                    (key_value, trade_date),
                ).fetchone()
                metrics = json.loads(existing["metrics_json"]) if existing else {}
                metrics.update({key: value for key, value in incoming.items() if value is not None})
                for key, value in incoming.items():
                    metrics.setdefault(key, value)
                conn.execute(
                    f"""INSERT INTO {table}({key_name},trade_date,metrics_json,source_query,fetched_at)
                        VALUES(?,?,?,?,?) ON CONFLICT({key_name},trade_date) DO UPDATE SET
                        metrics_json=excluded.metrics_json,source_query=excluded.source_query,fetched_at=excluded.fetched_at""",
                    (key_value, trade_date, json.dumps(metrics, ensure_ascii=False, sort_keys=True), query, now),
                )
            conn.commit()

    def _contract_error(self, entity_type: str, entity_code: str, contracts: dict) -> str | None:
        with connect_capital_db(self.capital_db_path) as conn:
            existing = {row["metric_name"]: dict(row) for row in conn.execute(
                "SELECT metric_name,source_field_code,source_field_name FROM source_contracts WHERE entity_type=? AND entity_code=?",
                (entity_type, entity_code),
            )}
        for (code, metric), contract in contracts.items():
            if code != entity_code or metric not in existing:
                continue
            previous = existing[metric]
            previous_name = previous["source_field_name"]
            incoming_name = contract["source_field_name"]
            previous_matches = contract_field_matches_metric(entity_type, metric, previous_name)
            incoming_matches = contract_field_matches_metric(entity_type, metric, incoming_name)
            if not previous_matches or not incoming_matches:
                return (
                    f"妙想字段语义契约变化: {metric} "
                    f"{previous['source_field_code']}[{previous_name}] -> "
                    f"{contract['source_field_code']}[{incoming_name}]"
                )
        return None

    def _save_contracts(self, entity_type: str, entity_code: str, contracts: dict) -> None:
        now = utc_timestamp()
        rows = []
        for (code, metric), contract in contracts.items():
            if code != entity_code:
                continue
            rows.append((entity_type, entity_code, metric, contract["source_field_code"], contract["source_field_name"], now))
        if not rows:
            return
        with connect_capital_db(self.capital_db_path) as conn:
            conn.executemany(
                """INSERT INTO source_contracts VALUES(?,?,?,?,?,?) ON CONFLICT(entity_type,entity_code,metric_name)
                   DO UPDATE SET source_field_code=excluded.source_field_code,
                   source_field_name=excluded.source_field_name,last_seen_at=excluded.last_seen_at""",
                rows,
            )
            conn.commit()
