"""Data preparation facade shared by market analysis and later strategy modules."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

from scripts.data.market_data_store import BLOCK_DIR, DB_PATH, connect_db, create_schema
from scripts.data.tdx_block_data import TDXBlockSource
from scripts.data.market_data import MiaoxiangSource
from scripts.shared import expected_trade_date, normalize_date_arg


BLOCK_KIND = {"block_zs.dat": "zs", "block_fg.dat": "fg", "block_gn.dat": "gn"}


def resolve_as_of(value: str | None = None) -> str:
    return normalize_date_arg(value) if value else expected_trade_date()


def today_stamp(date_value: str) -> str:
    return date_value.replace("-", "")[2:]


def is_a_share(code: str, market: int) -> bool:
    if market == 1:
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    return code.startswith(("000", "001", "002", "003", "300", "301", "430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920"))


class MarketDataService:
    """Owns TDX acquisition, snapshots, incremental repair and readiness checks."""

    def __init__(self, config: dict):
        self.config = config
        self.data_config = config["data"]

    def refresh_security_master(self, conn: sqlite3.Connection, source: TDXBlockSource, as_of: str) -> list[str]:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for market, frame in source.fetch_security_lists().items():
            for item in frame[["code", "name"]].itertuples(index=False):
                code, name = str(item.code).zfill(6), str(item.name).replace("\x00", "").strip()
                if is_a_share(code, market):
                    rows.append((code, name, market, int("ST" in name.upper()), now, now))
        conn.executemany(
            """INSERT INTO securities(code,name,market,is_st,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market,
               is_st=excluded.is_st, last_seen_at=excluded.last_seen_at""",
            rows,
        )
        conn.execute("DELETE FROM universe_members WHERE trade_date=?", (as_of,))
        conn.execute(
            """INSERT INTO universe_members(trade_date,code,eligible,exclusion_reason)
               SELECT ?, code, CASE WHEN is_st=1 THEN 0 ELSE 1 END,
               CASE WHEN is_st=1 THEN 'ST' ELSE NULL END FROM securities""",
            (as_of,),
        )
        conn.commit()
        return [row[0] for row in conn.execute("SELECT code FROM universe_members WHERE trade_date=? AND eligible=1", (as_of,))]

    @staticmethod
    def normalized_bars(frame: pd.DataFrame, code: str, as_of: str) -> list[tuple]:
        if frame is None or frame.empty:
            return []
        data = frame.copy()
        date_column = "datetime" if "datetime" in data else "date"
        volume_column = "vol" if "vol" in data else "volume"
        amount_column = "amount" if "amount" in data else None
        data["trade_date"] = pd.to_datetime(data[date_column], errors="coerce").dt.strftime("%Y-%m-%d")
        data = data[data["trade_date"].notna() & (data["trade_date"] <= as_of)]
        data = data[data[volume_column].fillna(0).astype(float) > 0]
        now = datetime.now().isoformat(timespec="seconds")
        return [
            (code, str(row.trade_date), float(row.open), float(row.high), float(row.low), float(row.close),
             float(getattr(row, volume_column)), float(getattr(row, amount_column)) if amount_column and pd.notna(getattr(row, amount_column)) else None, "tdx", now)
            for row in data.itertuples(index=False)
        ]

    def fetch_stock_bars(self, source: TDXBlockSource, fallback: MiaoxiangSource, code: str, name: str, lookback: int, as_of: str) -> list[tuple]:
        primary_error = ""
        try:
            bars = self.normalized_bars(source.fetch_stock_bars(code, lookback), code, as_of)
            if bars and bars[-1][1] == as_of:
                return bars
            primary_error = "通达信未覆盖目标交易日"
        except Exception as exc:
            primary_error = str(exc)
        frame, error = fallback.fetch_bars(code, name)
        bars = self.normalized_bars(frame, code, as_of) if frame is not None else []
        if not bars or bars[-1][1] != as_of:
            raise RuntimeError(f"TDX: {primary_error}; 妙想: {error or '未覆盖目标交易日'}")
        return [(*bar[:8], "miaoxiang", bar[9]) for bar in bars]

    @staticmethod
    def save_bars(conn: sqlite3.Connection, bars: list[tuple]) -> None:
        conn.executemany(
            """INSERT INTO daily_bars(code,trade_date,open,high,low,close,volume,amount,source,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,
               close=excluded.close,volume=excluded.volume,amount=excluded.amount,source=excluded.source,fetched_at=excluded.fetched_at""",
            bars,
        )

    def snapshot_blocks(self, conn: sqlite3.Connection, source: TDXBlockSource, as_of: str) -> dict:
        target = BLOCK_DIR / today_stamp(as_of)
        target.mkdir(parents=True, exist_ok=True)
        summary = {}
        for block_file in source.fetch_blocks(self.data_config["block_files"]):
            kind = BLOCK_KIND[block_file.filename]
            (target / block_file.filename).write_bytes(block_file.payload)
            grouped: dict[str, set[str]] = {}
            for row in block_file.rows:
                grouped.setdefault(str(row["blockname"]).strip(), set()).add(str(row["code"]).zfill(6))
            conn.execute("DELETE FROM block_members WHERE snapshot_date=? AND block_kind=?", (as_of, kind))
            conn.execute("DELETE FROM block_snapshots WHERE snapshot_date=? AND block_kind=?", (as_of, kind))
            snapshots, members = [], []
            for name, codes in grouped.items():
                snapshots.append((as_of, kind, name, block_file.filename, block_file.sha256, len(codes)))
                members.extend((as_of, kind, name, code) for code in sorted(codes))
            conn.executemany("INSERT INTO block_snapshots VALUES(?,?,?,?,?,?)", snapshots)
            conn.executemany("INSERT INTO block_members VALUES(?,?,?,?)", members)
            summary[kind] = {"blocks": len(grouped), "relations": len(members), "sha256": block_file.sha256}
        industry = source.fetch_industry_data()
        (target / "tdxhy.cfg").write_bytes(industry.tdxhy_payload)
        (target / "incon.dat").write_bytes(industry.incon_payload)
        conn.execute("DELETE FROM stock_industries WHERE snapshot_date=?", (as_of,))
        conn.execute("DELETE FROM industry_definitions WHERE snapshot_date=?", (as_of,))
        conn.execute("DELETE FROM block_members WHERE snapshot_date=? AND block_kind LIKE 'industry%'", (as_of,))
        conn.execute("DELETE FROM block_snapshots WHERE snapshot_date=? AND block_kind LIKE 'industry%'", (as_of,))
        definitions = []
        for code, (section, name) in industry.definitions.items():
            system = "tdx" if code.startswith("T") else "sw" if code.startswith("X") else section.lower()
            definitions.append((as_of, system, code, name, section))
        conn.executemany("INSERT INTO industry_definitions VALUES(?,?,?,?,?)", definitions)
        conn.executemany("INSERT INTO stock_industries VALUES(?,?,?,?)", [(as_of, row["code"], row["tdx_code"] or None, row["sw_code"] or None) for row in industry.mappings])
        grouped_industry: dict[tuple[str, str], set[str]] = {}
        for row in industry.mappings:
            for prefix_length, level in ((3, "l1"), (5, "l2"), (7, "l3")):
                ancestor_code = row["sw_code"][:prefix_length]
                info = industry.definitions.get(ancestor_code)
                if info and info[0] == "TDXRSHY":
                    grouped_industry.setdefault((f"industry_sw_{level}", info[1]), set()).add(row["code"])
        snapshots, members = [], []
        for (kind, name), codes in grouped_industry.items():
            snapshots.append((as_of, kind, name, "tdxhy.cfg+incon.dat", industry.tdxhy_sha256, len(codes)))
            members.extend((as_of, kind, name, code) for code in sorted(codes))
        conn.executemany("INSERT INTO block_snapshots VALUES(?,?,?,?,?,?)", snapshots)
        conn.executemany("INSERT INTO block_members VALUES(?,?,?,?)", members)
        summary["industry_sw"] = {"blocks": len(grouped_industry), "relations": len(members), "tdxhy_sha256": industry.tdxhy_sha256, "incon_sha256": industry.incon_sha256}
        (target / "manifest.json").write_text(json.dumps({"as_of": as_of, "blocks": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
        conn.commit()
        return summary

    def ingest_bars(self, conn: sqlite3.Connection, source: TDXBlockSource, codes: list[str], as_of: str, lookback: int, run_id: str, job_type: str, names: dict[str, str] | None = None) -> dict:
        completed = {row[0] for row in conn.execute("SELECT code FROM ingestion_jobs WHERE run_id=? AND job_type=? AND target_date=? AND status='success'", (run_id, job_type, as_of))}
        stats = {"requested": len(codes), "success": 0, "failed": 0, "fallback": 0, "failed_codes": {}, "repaired_codes": []}
        names = names or {row[0]: row[1] for row in conn.execute("SELECT code,name FROM securities WHERE code IN ({})".format(",".join("?" for _ in codes)), codes)} if codes else {}
        fallback = MiaoxiangSource()
        for index, code in enumerate(codes, 1):
            if code in completed:
                stats["success"] += 1
                continue
            try:
                known_rows = conn.execute("SELECT count(*) FROM daily_bars WHERE code=?", (code,)).fetchone()[0]
                request_lookback = max(lookback, self.data_config["initial_lookback_days"]) if known_rows < self.data_config["minimum_history_days"] else lookback
                bars = self.fetch_stock_bars(source, fallback, code, names.get(code, code), request_lookback, as_of)
                if known_rows < self.data_config["minimum_history_days"] and len(bars) < self.data_config["minimum_history_days"]:
                    raise RuntimeError(f"有效日线不足 {len(bars)}")
                self.save_bars(conn, bars)
                stats["fallback"] += int(any(bar[8] == "miaoxiang" for bar in bars))
                stats["repaired_codes"].append(code)
                status, error = "success", None
                stats["success"] += 1
            except Exception as exc:
                status, error = "failed", str(exc)[:500]
                stats["failed"] += 1
                stats["failed_codes"][code] = error
            conn.execute(
                """INSERT INTO ingestion_jobs VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,job_type,code,target_date) DO UPDATE SET status=excluded.status,
                   attempts=ingestion_jobs.attempts+1,error_message=excluded.error_message,updated_at=excluded.updated_at""",
                (run_id, job_type, code, as_of, status, 1, error, datetime.now().isoformat(timespec="seconds")),
            )
            if index % 50 == 0:
                conn.commit()
                print(f"  日线进度 {index}/{len(codes)}，成功 {stats['success']}，失败 {stats['failed']}")
        conn.commit()
        return stats

    def get_daily_bars(self, codes: list[tuple[str, str]], as_of: str, minimum_days: int, force_refresh: bool = False) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
        """Read canonical bars first; repair only codes missing history or target-date coverage."""
        conn = connect_db(); create_schema(conn)
        try:
            requested = [code for code, _ in codes]
            if not requested:
                return {}, {}
            conn.executemany("INSERT INTO securities(code,name,market,is_st,first_seen_at,last_seen_at) VALUES(?,?,0,0,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,last_seen_at=excluded.last_seen_at", [(code, name, datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds")) for code, name in codes])
            checks = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT code,count(*),max(trade_date) FROM daily_bars WHERE code IN ({}) AND trade_date<=? GROUP BY code".format(",".join("?" for _ in requested)), [*requested, as_of])}
            missing = requested if force_refresh else [code for code in requested if code not in checks or checks[code][0] < minimum_days or checks[code][1] != as_of]
            repair = self.ingest_bars(conn, TDXBlockSource(), missing, as_of, max(15, minimum_days), f"quant_{today_stamp(as_of)}", "quant_refresh" if force_refresh else "quant_repair", names=dict(codes)) if missing else {"failed_codes": {}, "repaired_codes": []}
            frame = pd.read_sql_query("SELECT code,trade_date AS date,open,high,low,close,volume,COALESCE(amount,0) AS turnover,source FROM daily_bars WHERE code IN ({}) AND trade_date<=? ORDER BY code,trade_date".format(",".join("?" for _ in requested)), conn, params=[*requested, as_of])
            frames = {code: rows.drop(columns="code").reset_index(drop=True) for code, rows in frame.groupby("code")}
            status = {code: {"source": "database", "error": None, "retryable": False} for code in frames}
            for code, error in repair.get("failed_codes", {}).items():
                status[code] = {"source": "repair_failed", "error": error, "retryable": "112" in error or "限流" in error}
            for code in repair.get("repaired_codes", []):
                if code in status:
                    status[code]["source"] = str(frames[code].iloc[-1]["source"])
            return frames, status
        finally:
            conn.close()

    def ingest_benchmarks(self, conn: sqlite3.Connection, source: TDXBlockSource, as_of: str, lookback: int) -> None:
        for code in self.config["benchmarks"].values():
            request_lookback = max(lookback, self.data_config["minimum_history_days"] + 10)
            bars = self.normalized_bars(source.fetch_index_bars(code, request_lookback), f"IDX:{code}", as_of)
            if len(bars) < self.data_config["minimum_history_days"]:
                raise RuntimeError(f"宽基指数 {code} 有效日线不足 {len(bars)}")
            self.save_bars(conn, bars)
        conn.commit()

    @staticmethod
    def coverage(conn: sqlite3.Connection, as_of: str) -> tuple[int, int, float]:
        total = conn.execute("SELECT count(*) FROM universe_members WHERE trade_date=? AND eligible=1", (as_of,)).fetchone()[0]
        valid = conn.execute("""SELECT count(*) FROM universe_members u JOIN daily_bars b ON u.code=b.code
            WHERE u.trade_date=? AND u.eligible=1 AND b.trade_date=?""", (as_of, as_of)).fetchone()[0]
        return total, valid, valid / total if total else 0.0

    def ensure_ready(self, as_of: str | None = None, profile: str = "market_regime", lookback: int | None = None, run_type: str = "update", max_codes: int | None = None) -> dict:
        if profile != "market_regime":
            raise ValueError(f"暂不支持的数据 profile: {profile}")
        target = resolve_as_of(as_of)
        history = lookback or self.data_config["refresh_lookback_days"]
        conn = connect_db()
        create_schema(conn)
        run_id = f"{run_type}_{target}"
        conn.execute("""INSERT INTO run_log VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET status='RUNNING',detail_json='{}',started_at=excluded.started_at,finished_at=NULL""", (run_id, run_type, target, "RUNNING", None, "{}", datetime.now().isoformat(timespec="seconds"), None))
        conn.commit()
        source = TDXBlockSource(chunk_size=self.data_config["block_chunk_size"])
        try:
            blocks = self.snapshot_blocks(conn, source, target)
            codes = self.refresh_security_master(conn, source, target)
            if max_codes:
                codes = codes[:max_codes]
            self.ingest_benchmarks(conn, source, target, history)
            bars = self.ingest_bars(conn, source, codes, target, history, run_id, run_type)
            total, valid, ratio = self.coverage(conn, target)
            status = "READY" if not max_codes and ratio >= self.data_config["minimum_coverage_ratio"] else "PARTIAL"
            detail = {"profile": profile, "blocks": blocks, "bars": bars, "universe": {"total": total, "valid": valid, "coverage": ratio}, "sample_mode": bool(max_codes), "database": str(DB_PATH)}
            conn.execute("UPDATE run_log SET status=?,coverage_ratio=?,detail_json=?,finished_at=? WHERE run_id=?", (status, ratio, json.dumps(detail, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), run_id))
            conn.commit()
            return {"run_id": run_id, "as_of_trade_date": target, "status": status, **detail}
        except Exception as exc:
            conn.execute("UPDATE run_log SET status='FAILED',detail_json=?,finished_at=? WHERE run_id=?", (json.dumps({"error": str(exc)}, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), run_id))
            conn.commit()
            raise
        finally:
            conn.close()

    @staticmethod
    def latest_status() -> dict:
        conn = connect_db()
        create_schema(conn)
        try:
            row = conn.execute("SELECT * FROM run_log ORDER BY started_at DESC LIMIT 1").fetchone()
            return dict(row) if row else {"status": "EMPTY", "database": str(DB_PATH)}
        finally:
            conn.close()
