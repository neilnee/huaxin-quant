"""Point-in-time A-share price adjustment backed by persisted corporate actions."""

from __future__ import annotations

import hashlib
import io
import json
import math
import socket
import sqlite3
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta

import pandas as pd
import requests


PRIMARY_SOURCE = "tdx_xdxr"
VERIFICATION_SOURCE = "baostock_qfq"
FALLBACK_SOURCE = "eastmoney_qfq"
PRICE_COLUMNS = ("open", "high", "low", "close")


def safe_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def action_hash(action: dict) -> str:
    values = [
        action["date"],
        *[f"{safe_float(action[field]):.10f}" for field in (
            "cash_dividend_per_10", "bonus_shares_per_10",
            "rights_shares_per_10", "rights_price",
        )],
    ]
    return hashlib.sha256("|".join(values).encode("ascii")).hexdigest()


def normalize_tdx_actions(frame: pd.DataFrame | None, as_of: str) -> list[dict]:
    """Normalize category-1 XDXR rows and exclude events after the run date."""
    if frame is None or frame.empty:
        return []
    actions = []
    for row in frame.to_dict("records"):
        if int(safe_float(row.get("category"))) != 1:
            continue
        try:
            ex_date = f"{int(row['year']):04d}-{int(row['month']):02d}-{int(row['day']):02d}"
        except (KeyError, TypeError, ValueError):
            continue
        if ex_date > as_of:
            continue
        action = {
            "date": ex_date,
            "cash_dividend_per_10": safe_float(row.get("fenhong")),
            "bonus_shares_per_10": safe_float(row.get("songzhuangu")),
            "rights_shares_per_10": safe_float(row.get("peigu")),
            "rights_price": safe_float(row.get("peigujia")),
        }
        if any(action[field] for field in (
            "cash_dividend_per_10", "bonus_shares_per_10", "rights_shares_per_10"
        )):
            action["source_hash"] = action_hash(action)
            actions.append(action)
    return sorted(actions, key=lambda item: item["date"])


def load_actions(conn: sqlite3.Connection, code: str, as_of: str) -> list[dict]:
    rows = conn.execute(
        """SELECT ex_date,cash_dividend_per_10,bonus_shares_per_10,
                  rights_shares_per_10,rights_price,source_hash
             FROM corporate_actions
            WHERE code=? AND source=? AND ex_date<=? ORDER BY ex_date""",
        (code, PRIMARY_SOURCE, as_of),
    ).fetchall()
    return [
        {
            "date": row[0], "cash_dividend_per_10": float(row[1]),
            "bonus_shares_per_10": float(row[2]), "rights_shares_per_10": float(row[3]),
            "rights_price": float(row[4]), "source_hash": row[5],
        }
        for row in rows
    ]


def sync_tdx_actions(
    conn: sqlite3.Connection,
    source,
    code: str,
    as_of: str,
    force: bool = False,
) -> dict:
    """Refresh one code at most once per as-of date and return changed events."""
    cached = conn.execute(
        "SELECT status,action_count,error_message FROM corporate_action_syncs WHERE code=? AND source=? AND as_of_date=?",
        (code, PRIMARY_SOURCE, as_of),
    ).fetchone()
    if cached and cached[0] == "SUCCESS" and not force:
        return {"status": "CACHED", "changed": [], "action_count": int(cached[1])}

    now = datetime.now().isoformat(timespec="seconds")
    try:
        actions = normalize_tdx_actions(source.fetch_corporate_actions(code), as_of)
        changed = []
        for action in actions:
            prior = conn.execute(
                "SELECT source_hash FROM corporate_actions WHERE code=? AND ex_date=? AND source=?",
                (code, action["date"], PRIMARY_SOURCE),
            ).fetchone()
            if prior is None or prior[0] != action["source_hash"]:
                changed.append(action)
            conn.execute(
                """INSERT INTO corporate_actions(
                       code,ex_date,cash_dividend_per_10,bonus_shares_per_10,
                       rights_shares_per_10,rights_price,source,source_hash,first_seen_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code,ex_date,source) DO UPDATE SET
                       cash_dividend_per_10=excluded.cash_dividend_per_10,
                       bonus_shares_per_10=excluded.bonus_shares_per_10,
                       rights_shares_per_10=excluded.rights_shares_per_10,
                       rights_price=excluded.rights_price,source_hash=excluded.source_hash,
                       last_seen_at=excluded.last_seen_at""",
                (
                    code, action["date"], action["cash_dividend_per_10"],
                    action["bonus_shares_per_10"], action["rights_shares_per_10"],
                    action["rights_price"], PRIMARY_SOURCE, action["source_hash"], now, now,
                ),
            )
        combined_hash = hashlib.sha256(
            "|".join(item["source_hash"] for item in actions).encode("ascii")
        ).hexdigest()
        conn.execute(
            """INSERT OR REPLACE INTO corporate_action_syncs
               (code,source,as_of_date,status,action_count,source_hash,error_message,fetched_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (code, PRIMARY_SOURCE, as_of, "SUCCESS", len(actions), combined_hash, None, now),
        )
        conn.commit()
        return {"status": "SUCCESS", "changed": changed, "action_count": len(actions)}
    except Exception as exc:
        conn.execute(
            """INSERT OR REPLACE INTO corporate_action_syncs
               (code,source,as_of_date,status,action_count,source_hash,error_message,fetched_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (code, PRIMARY_SOURCE, as_of, "FAILED", 0, None, str(exc)[:500], now),
        )
        conn.commit()
        return {"status": "FAILED", "changed": [], "action_count": 0, "error": str(exc)}


def event_factor(previous_close: float, action: dict) -> float:
    """Return the multiplicative factor mapping pre-event prices to post-event basis."""
    dividend = safe_float(action.get("cash_dividend_per_10")) / 10.0
    bonus = safe_float(action.get("bonus_shares_per_10")) / 10.0
    rights = safe_float(action.get("rights_shares_per_10")) / 10.0
    rights_price = safe_float(action.get("rights_price"))
    denominator = previous_close * (1.0 + bonus + rights)
    numerator = previous_close - dividend + rights * rights_price
    if previous_close <= 0 or denominator <= 0 or numerator <= 0:
        raise ValueError(f"invalid corporate-action factor inputs: close={previous_close}, action={action}")
    return numerator / denominator


def apply_point_in_time_qfq(frame: pd.DataFrame, actions: list[dict], as_of: str) -> tuple[pd.DataFrame, list[dict]]:
    """Adjust OHLC in memory; the caller's raw frame is never modified."""
    adjusted = frame.copy()
    adjusted["date"] = pd.to_datetime(adjusted["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw_close = dict(zip(adjusted["date"], adjusted["close"].astype(float)))
    applied = []
    for action in sorted(actions, key=lambda item: item["date"]):
        ex_date = str(action["date"])
        if ex_date > as_of:
            continue
        prior_dates = [value for value in raw_close if value < ex_date]
        affected = adjusted["date"] < ex_date
        if not prior_dates or not affected.any():
            continue
        previous_date = max(prior_dates)
        factor = event_factor(raw_close[previous_date], action)
        adjusted.loc[affected, list(PRICE_COLUMNS)] = (
            adjusted.loc[affected, list(PRICE_COLUMNS)].astype(float) * factor
        )
        applied.append({**action, "previous_trade_date": previous_date, "factor": factor})
    return adjusted, applied


class BaoStockVerifier:
    """Compare local event factors with BaoStock using only 2-3 nearby sessions."""

    provider_name = "BaoStock"

    def __init__(self):
        self.client = None
        self.login_error = None

    def _login(self):
        if self.client is not None:
            return self.client
        if self.login_error is not None:
            raise RuntimeError(f"BaoStock 登录本轮已失败: {self.login_error}")
        previous_timeout = socket.getdefaulttimeout()
        try:
            import baostock as bs

            # BaoStock creates its socket inside login; bound both connect and recv.
            socket.setdefaulttimeout(10)
            with redirect_stdout(io.StringIO()):
                result = bs.login()
            if result.error_code != "0":
                raise RuntimeError(result.error_msg)
        except Exception as exc:
            self.login_error = exc
            raise
        finally:
            socket.setdefaulttimeout(previous_timeout)
        self.client = bs
        return bs

    def close(self) -> None:
        if self.client is not None:
            with redirect_stdout(io.StringIO()):
                self.client.logout()
            self.client = None

    @staticmethod
    def _query(client, code: str, start: str, end: str, adjustflag: str) -> dict[str, float]:
        prefix = (
            "sh" if code.startswith(("5", "6")) else
            "bj" if code.startswith(("4", "8", "9")) else
            "sz"
        )
        result = client.query_history_k_data_plus(
            f"{prefix}.{code}", "date,close", start_date=start, end_date=end,
            frequency="d", adjustflag=adjustflag,
        )
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        rows = {}
        while result.next():
            values = result.get_row_data()
            rows[values[0]] = float(values[1])
        return rows

    def verify(
        self,
        code: str,
        action: dict,
        raw_frame: pd.DataFrame,
        as_of: str,
        max_factor_diff_pct: float,
        max_raw_close_diff_pct: float,
        sample_days: int = 3,
    ) -> dict:
        client = self._login()
        ex_date = str(action["date"])
        center = date.fromisoformat(ex_date)
        start = (center - timedelta(days=10)).isoformat()
        end = min(date.fromisoformat(as_of), center + timedelta(days=10)).isoformat()
        raw = self._query(client, code, start, end, "3")
        qfq = self._query(client, code, start, end, "2")
        local = {
            str(row.date): float(row.close)
            for row in raw_frame[["date", "close"]].itertuples(index=False)
        }
        common = sorted(set(raw) & set(qfq) & set(local))
        before = [value for value in common if value < ex_date]
        on_or_after = [value for value in common if value >= ex_date]
        if not before or not on_or_after:
            raise RuntimeError(f"{self.provider_name} 核验窗口缺少除权日前后交易日")
        reference = on_or_after[0]
        samples = [before[-1], reference]
        if len(on_or_after) > 1 and sample_days >= 3:
            samples.append(on_or_after[1])
        expected = event_factor(local[before[-1]], action)
        anchor_ratio = qfq[reference] / raw[reference]
        factor_diffs = []
        raw_diffs = []
        details = []
        for trade_date in samples:
            normalized = (qfq[trade_date] / raw[trade_date]) / anchor_ratio
            target = expected if trade_date < ex_date else 1.0
            factor_diff = abs(normalized / target - 1.0) * 100
            raw_diff = abs(raw[trade_date] / local[trade_date] - 1.0) * 100
            factor_diffs.append(factor_diff)
            raw_diffs.append(raw_diff)
            details.append({
                "date": trade_date, "normalized_factor": normalized,
                "expected_factor": target, "factor_diff_pct": factor_diff,
                "raw_close_diff_pct": raw_diff,
            })
        max_factor = max(factor_diffs)
        max_raw = max(raw_diffs)
        status = "VERIFIED" if max_factor <= max_factor_diff_pct and max_raw <= max_raw_close_diff_pct else "CONFLICT"
        return {
            "status": status, "sample_count": len(samples),
            "max_factor_diff_pct": max_factor, "max_raw_close_diff_pct": max_raw,
            "detail": {"samples": details, "reference_date": reference},
        }


class EastmoneyVerifier(BaoStockVerifier):
    """On-demand third source; reuse the same normalized event comparison."""

    URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    provider_name = "东方财富"

    def __init__(self):
        self.session = requests.Session()

    def _login(self):
        return self

    def close(self):
        self.session.close()

    def _query(self, client, code, start, end, adjustflag):
        market = "1" if code.startswith("6") else "0"
        response = self.session.get(self.URL, params={
            "secid": f"{market}.{code}", "klt": "101",
            "fqt": "0" if adjustflag == "3" else "1",
            "beg": start.replace("-", ""), "end": end.replace("-", ""),
            "lmt": "100", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56",
        }, timeout=10)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        if payload.get("rc") != 0 or data.get("code") != code or not data.get("klines"):
            raise RuntimeError("东方财富核验返回空数据或证券不符")
        rows = {}
        for line in data["klines"]:
            fields = line.split(",")
            trade_date = date.fromisoformat(fields[0]).isoformat()
            close = float(fields[2])
            if not math.isfinite(close) or close <= 0 or trade_date in rows:
                raise RuntimeError("东方财富核验价格无效或日期重复")
            if start <= trade_date <= end:
                rows[trade_date] = close
        return rows

    def verify(self, code, action, raw_frame, as_of, max_factor_diff_pct,
               max_raw_close_diff_pct, sample_days=3):
        result = super().verify(code, action, raw_frame, as_of, max_factor_diff_pct,
                                max_raw_close_diff_pct, sample_days)
        dates = sorted(str(value) for value in raw_frame["date"] if str(value) <= as_of)
        before = [value for value in dates if value < action["date"]]
        after = [value for value in dates if value >= action["date"]]
        expected = ([before[-1]] if before else []) + after[:2 if sample_days >= 3 else 1]
        actual = [item["date"] for item in result["detail"]["samples"]]
        if actual != expected or len(expected) < 2:
            raise RuntimeError("东方财富核验缺少本地应有的事件样本")
        result["detail"]["endpoint"] = self.URL
        return result


def save_verification(
    conn: sqlite3.Connection, code: str, action: dict, result: dict,
    run_date: str | None = None,
    verification_source: str = VERIFICATION_SOURCE,
) -> None:
    detail = dict(result.get("detail", {}))
    detail["action_hash"] = action_hash(action)
    if run_date:
        detail["run_date"] = run_date
    conn.execute(
        """INSERT OR REPLACE INTO adjustment_verifications(
               code,ex_date,primary_source,verification_source,status,sample_count,
               max_factor_diff_pct,max_raw_close_diff_pct,detail_json,verified_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            code, action["date"], PRIMARY_SOURCE, verification_source,
            result["status"], int(result.get("sample_count", 0)),
            result.get("max_factor_diff_pct"), result.get("max_raw_close_diff_pct"),
            json.dumps(detail, ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def fallback_verification_passed(conn, code, action, as_of):
    """Reuse a passed third-source opinion only for the same event and run-date boundary."""
    row = conn.execute(
        """SELECT status,detail_json FROM adjustment_verifications WHERE code=? AND ex_date=?
           AND primary_source=? AND verification_source=?""",
        (code, action["date"], PRIMARY_SOURCE, FALLBACK_SOURCE),
    ).fetchone()
    if not row or row[0] != "VERIFIED":
        return False
    detail = json.loads(row[1])
    run_date = detail.get("run_date")
    return bool(run_date and run_date <= as_of and detail.get("action_hash") == action_hash(action))


def retry_conflicting_verifications(
    conn: sqlite3.Connection, code: str, applied: list[dict],
    raw_frame: pd.DataFrame, as_of: str, verifier: BaoStockVerifier,
    config: dict,
) -> None:
    """Recheck stale conflicts once per trading date, retaining the gate on errors."""
    actions = {item["date"]: item for item in applied}
    rows = conn.execute(
        """SELECT ex_date,detail_json,verified_at FROM adjustment_verifications
           WHERE code=? AND primary_source=? AND verification_source=? AND status='CONFLICT'""",
        (code, PRIMARY_SOURCE, VERIFICATION_SOURCE),
    ).fetchall()
    for ex_date, detail_json, verified_at in rows:
        if ex_date not in actions:
            continue
        if fallback_verification_passed(conn, code, actions[ex_date], as_of):
            continue
        detail = json.loads(detail_json)
        last_run = max(
            detail.get("run_date") or verified_at[:10],
            detail.get("conflict_retry_run_date") or "",
        )
        if as_of <= last_run:
            continue
        try:
            result = verifier.verify(
                code, actions[ex_date], raw_frame, as_of,
                float(config.get("max_factor_diff_pct", 1.0)),
                float(config.get("max_raw_close_diff_pct", 0.15)),
                int(config.get("verification_sample_days", 3)),
            )
            if result["status"] not in {"VERIFIED", "CONFLICT"}:
                raise RuntimeError("冲突重验未返回有效核验结果")
        except Exception as exc:
            # A provider outage must not erase an established conflict.
            detail.update({"conflict_retry_run_date": as_of, "conflict_retry_error": str(exc)})
            conn.execute(
                """UPDATE adjustment_verifications SET detail_json=?
                   WHERE code=? AND ex_date=? AND primary_source=? AND verification_source=?""",
                (json.dumps(detail, ensure_ascii=False), code, ex_date,
                 PRIMARY_SOURCE, VERIFICATION_SOURCE),
            )
            conn.commit()
            continue
        save_verification(conn, code, actions[ex_date], result, run_date=as_of)


def reclassify_verifications(
    conn: sqlite3.Connection,
    code: str,
    max_factor_diff_pct: float,
    max_raw_close_diff_pct: float,
) -> None:
    """Reapply configured tolerances without making another provider request."""
    conn.execute(
        """UPDATE adjustment_verifications
              SET status=CASE
                  WHEN max_factor_diff_pct<=? AND max_raw_close_diff_pct<=? THEN 'VERIFIED'
                  ELSE 'CONFLICT' END
            WHERE code=? AND sample_count>0
              AND max_factor_diff_pct IS NOT NULL AND max_raw_close_diff_pct IS NOT NULL""",
        (max_factor_diff_pct, max_raw_close_diff_pct, code),
    )
    conn.commit()


def supplement_verifications(conn, code, applied, raw_frame, as_of, verifier, config):
    """Fetch a third-source opinion only for recorded primary verification failures."""
    if config.get("fallback_verification_source", FALLBACK_SOURCE) != FALLBACK_SOURCE:
        return
    for action in applied:
        primary = conn.execute(
            """SELECT status,detail_json,verified_at FROM adjustment_verifications WHERE code=? AND ex_date=?
               AND primary_source=? AND verification_source=?""",
            (code, action["date"], PRIMARY_SOURCE, VERIFICATION_SOURCE),
        ).fetchone()
        if not primary or primary[0] not in {"CONFLICT", "PENDING"}:
            continue
        if (json.loads(primary[1]).get("run_date") or primary[2][:10]) > as_of:
            continue
        cached = conn.execute(
            """SELECT status,detail_json FROM adjustment_verifications WHERE code=? AND ex_date=?
               AND primary_source=? AND verification_source=?""",
            (code, action["date"], PRIMARY_SOURCE, FALLBACK_SOURCE),
        ).fetchone()
        if cached:
            detail = json.loads(cached[1])
            if detail.get("action_hash") == action_hash(action):
                if cached[0] == "VERIFIED" or detail.get("run_date", "") >= as_of:
                    continue
        try:
            result = verifier.verify(
                code, action, raw_frame, as_of,
                float(config.get("max_factor_diff_pct", 1.0)),
                float(config.get("max_raw_close_diff_pct", 0.15)),
                int(config.get("verification_sample_days", 3)),
            )
        except Exception as exc:
            result = {"status": "PENDING", "sample_count": 0, "detail": {"error": str(exc)}}
            # Retain an established third-source conflict if the next request fails.
            if cached and cached[0] == "CONFLICT" and json.loads(cached[1]).get("action_hash") == action_hash(action):
                detail = json.loads(cached[1])
                detail.update({"run_date": as_of, "retry_error": str(exc)})
                conn.execute(
                    """UPDATE adjustment_verifications SET detail_json=? WHERE code=? AND ex_date=?
                       AND primary_source=? AND verification_source=?""",
                    (json.dumps(detail, ensure_ascii=False), code, action["date"], PRIMARY_SOURCE, FALLBACK_SOURCE),
                )
                conn.commit()
                continue
        save_verification(conn, code, action, result, run_date=as_of, verification_source=FALLBACK_SOURCE)


def effective_verifications(conn, code, applied, as_of=None):
    effective = []
    for action in applied:
        rows = conn.execute(
            """SELECT verification_source,status,detail_json FROM adjustment_verifications
               WHERE code=? AND ex_date=? AND primary_source=?""",
            (code, action["date"], PRIMARY_SOURCE),
        ).fetchall()
        primary = next((row for row in rows if row[0] == VERIFICATION_SOURCE), None)
        fallback = next((row for row in rows if row[0] == FALLBACK_SOURCE
                         and json.loads(row[2]).get("action_hash") == action_hash(action)
                         and (not as_of or json.loads(row[2]).get("run_date", "") <= as_of)), None)
        selected = primary
        if (not primary or primary[1] != "VERIFIED") and fallback and fallback[1] in {"VERIFIED", "CONFLICT"}:
            if fallback[1] == "VERIFIED" or not primary or primary[1] != "CONFLICT":
                selected = fallback
        effective.append((selected[1], selected[0]) if selected else ("PENDING", "unverified"))
    return effective


def verification_status(conn: sqlite3.Connection, code: str, applied: list[dict], as_of=None) -> str:
    if not applied:
        return "NO_ACTION"
    statuses = [status for status, _ in effective_verifications(conn, code, applied, as_of)]
    if "CONFLICT" in statuses:
        return "CONFLICT"
    if all(value == "VERIFIED" for value in statuses):
        return "VERIFIED"
    if "VERIFIED" in statuses:
        return "PARTIAL"
    return "PENDING"
