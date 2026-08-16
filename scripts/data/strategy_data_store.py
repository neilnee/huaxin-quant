"""SQLite persistence for deterministic strategy decisions and events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from scripts.shared import PROJECT_ROOT


DB_PATH = Path(PROJECT_ROOT) / "cache" / "strategy" / "strategy_data.sqlite"
SCHEMA_VERSION = "1"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _code(row: dict) -> str:
    return str(row.get("code") or "").zfill(6)


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    memory = str(path) == ":memory:"
    target = None if memory else Path(path)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(":memory:" if memory else target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not memory:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS strategy_documents (
            document_id TEXT PRIMARY KEY,
            module TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            strategy_version TEXT,
            schema_name TEXT,
            source_path TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(module, trade_date, payload_hash)
        );
        CREATE TABLE IF NOT EXISTS current_documents (
            module TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            document_id TEXT NOT NULL REFERENCES strategy_documents(document_id),
            PRIMARY KEY(module, trade_date)
        );
        CREATE TABLE IF NOT EXISTS vcp_structure_snapshots (
            document_id TEXT NOT NULL REFERENCES strategy_documents(document_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            structure_type TEXT,
            structure_stage TEXT,
            structure_anchor TEXT,
            structure_valid INTEGER,
            structure_score REAL,
            structure_risk_score REAL,
            setup_signal TEXT,
            setup_quality TEXT,
            action_hint TEXT,
            strategy_version TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(document_id, code)
        );
        CREATE INDEX IF NOT EXISTS idx_vcp_date_stage
            ON vcp_structure_snapshots(trade_date, structure_stage);
        CREATE INDEX IF NOT EXISTS idx_vcp_code_date
            ON vcp_structure_snapshots(code, trade_date);
        CREATE TABLE IF NOT EXISTS signal_plans (
            document_id TEXT NOT NULL REFERENCES strategy_documents(document_id) ON DELETE CASCADE,
            plan_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            plan_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            structure_anchor TEXT,
            setup_family TEXT,
            plan_action TEXT,
            target_quality TEXT,
            invalid_price REAL,
            strategy_version TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(document_id, plan_id)
        );
        CREATE INDEX IF NOT EXISTS idx_plan_date_code ON signal_plans(plan_date, code);
        CREATE TABLE IF NOT EXISTS bloom_daily_snapshots (
            document_id TEXT NOT NULL REFERENCES strategy_documents(document_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            bloom_status TEXT,
            bloom_signal TEXT,
            model2_stage TEXT,
            model2_setup_signal TEXT,
            structure_score REAL,
            strategy_version TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(document_id, code)
        );
        CREATE INDEX IF NOT EXISTS idx_bloom_date_status
            ON bloom_daily_snapshots(trade_date, bloom_status);
        CREATE TABLE IF NOT EXISTS bloom_events (
            trade_date TEXT NOT NULL,
            event_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            code TEXT NOT NULL,
            event_type TEXT,
            strategy_version TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(trade_date, event_id)
        );
        CREATE TABLE IF NOT EXISTS buy_point_events (
            event_id TEXT PRIMARY KEY,
            plan_id TEXT,
            plan_date TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            structure_anchor TEXT,
            setup_family TEXT,
            entry_action TEXT,
            entry_grade TEXT,
            reference_entry_price REAL,
            initial_invalid_price REAL,
            quant_strategy_version TEXT,
            plan_strategy_version TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_buy_entry_code ON buy_point_events(entry_date, code);
        CREATE TABLE IF NOT EXISTS buy_point_lifecycle_daily (
            event_id TEXT NOT NULL REFERENCES buy_point_events(event_id) ON DELETE CASCADE,
            trade_date TEXT NOT NULL,
            age_trade_days INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            current_return_pct REAL,
            current_r REAL,
            mfe_pct REAL,
            mfe_r REAL,
            mae_pct REAL,
            mae_r REAL,
            hit_1r INTEGER NOT NULL DEFAULT 0,
            hit_2r INTEGER NOT NULL DEFAULT 0,
            hit_3r INTEGER NOT NULL DEFAULT 0,
            invalid_touched INTEGER NOT NULL DEFAULT 0,
            invalid_confirmed INTEGER NOT NULL DEFAULT 0,
            lifecycle_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(event_id, trade_date)
        );
        """
    )
    conn.execute(
        "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def _trade_date(module: str, payload: dict) -> str:
    meta = payload.get("meta") or {}
    value = meta.get("run_date") or meta.get("date") or payload.get("summary", {}).get("date")
    if not value:
        raise ValueError(f"{module} payload has no trade date")
    return str(value)


def save_document(conn: sqlite3.Connection, module: str, payload: dict, source_path: str = "") -> str:
    meta = payload.get("meta") or {}
    trade_date = _trade_date(module, payload)
    payload_hash = _hash(payload)
    document_id = f"{module}:{trade_date}:{payload_hash[:16]}"
    conn.execute(
        """INSERT OR IGNORE INTO strategy_documents
           (document_id,module,trade_date,strategy_version,schema_name,source_path,
            payload_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            document_id, module, trade_date,
            meta.get("strategy_version") or payload.get("summary", {}).get("strategy_version"),
            meta.get("schema"), source_path, payload_hash, _json(payload),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.execute(
        """INSERT INTO current_documents(module,trade_date,document_id) VALUES(?,?,?)
           ON CONFLICT(module,trade_date) DO UPDATE SET document_id=excluded.document_id""",
        (module, trade_date, document_id),
    )
    return document_id


def load_document(conn: sqlite3.Connection, module: str, trade_date: str) -> dict | None:
    row = conn.execute(
        """SELECT d.payload_json FROM current_documents c
           JOIN strategy_documents d ON d.document_id=c.document_id
           WHERE c.module=? AND c.trade_date=?""",
        (module, trade_date),
    ).fetchone()
    return json.loads(row[0]) if row else None


def document_dates(conn: sqlite3.Connection, module: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT trade_date FROM current_documents WHERE module=? ORDER BY trade_date", (module,)
    )]


def structure_anchor(row: dict) -> str:
    value = row.get("setup_structure_anchor_date")
    if value:
        return str(value)
    group = row.get("contraction_group") or []
    if group and isinstance(group[0], dict) and group[0].get("start_date"):
        return str(group[0]["start_date"])
    contractions = row.get("contractions") or []
    if contractions and isinstance(contractions[-1], dict) and contractions[-1].get("start_date"):
        return str(contractions[-1]["start_date"])
    return str(row.get("structure_breakout_date") or "unanchored")


def save_quant(conn: sqlite3.Connection, payload: dict, source_path: str = "") -> str:
    document_id = save_document(conn, "quant", payload, source_path)
    trade_date = _trade_date("quant", payload)
    strategy_version = (payload.get("meta") or {}).get("strategy_version")
    conn.execute("DELETE FROM vcp_structure_snapshots WHERE document_id=?", (document_id,))
    for ordinal, row in enumerate(payload.get("results", [])):
        conn.execute(
            """INSERT INTO vcp_structure_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id, ordinal, trade_date, _code(row), row.get("name"),
                row.get("structure_type"), row.get("structure_stage"), structure_anchor(row),
                int(bool(row.get("structure_valid"))) if row.get("structure_valid") is not None else None,
                row.get("structure_score"), row.get("structure_risk_score"), row.get("setup_signal"),
                row.get("setup_quality"), row.get("action_hint"),
                row.get("strategy_version") or strategy_version,
                _json(row),
            ),
        )
    conn.commit()
    return document_id


def _plan_id(plan_date: str, row: dict) -> str:
    raw = "|".join((
        plan_date, _code(row), str(row.get("structure_anchor") or "unanchored"),
        str(row.get("setup_family") or ""), str(row.get("plan_action") or ""),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def save_signal_plan(conn: sqlite3.Connection, payload: dict, source_path: str = "") -> str:
    document_id = save_document(conn, "signal_plan", payload, source_path)
    plan_date = _trade_date("signal_plan", payload)
    strategy_version = (payload.get("meta") or {}).get("strategy_version")
    conn.execute("DELETE FROM signal_plans WHERE document_id=?", (document_id,))
    for ordinal, row in enumerate(payload.get("plans", [])):
        conn.execute(
            """INSERT INTO signal_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id, _plan_id(plan_date, row), ordinal, plan_date, _code(row), row.get("name"),
                row.get("structure_anchor"), row.get("setup_family"), row.get("plan_action"),
                row.get("target_quality"), row.get("invalid_price"),
                row.get("strategy_version") or strategy_version,
                _json(row),
            ),
        )
    conn.commit()
    return document_id


def _bloom_rows(payload: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    for value in (payload.get("sections") or {}).values():
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and _code(row).strip("0"):
                    rows[_code(row)] = row
    for row in payload.get("top_candidates") or []:
        if isinstance(row, dict) and _code(row).strip("0"):
            rows[_code(row)] = row
    return list(rows.values())


def save_bloom(
    conn: sqlite3.Connection,
    payload: dict,
    state_rows: dict[str, dict] | list[dict] | None = None,
    events: list[dict] | None = None,
    source_path: str = "",
) -> str:
    document_id = save_document(conn, "bloom", payload, source_path)
    trade_date = _trade_date("bloom", payload)
    strategy_version = (payload.get("summary") or {}).get("strategy_version")
    rows = list(state_rows.values()) if isinstance(state_rows, dict) else (state_rows or _bloom_rows(payload))
    conn.execute("DELETE FROM bloom_daily_snapshots WHERE document_id=?", (document_id,))
    for ordinal, row in enumerate(rows):
        conn.execute(
            """INSERT INTO bloom_daily_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id, ordinal, trade_date, _code(row), row.get("bloom_status"),
                row.get("bloom_signal"), row.get("model2_stage"), row.get("model2_setup_signal"),
                row.get("structure_score"), row.get("strategy_version") or strategy_version, _json(row),
            ),
        )
    if events is not None:
        replace_bloom_events(conn, trade_date, events)
    conn.commit()
    return document_id


def replace_bloom_events(conn: sqlite3.Connection, trade_date: str, events: list[dict]) -> None:
    conn.execute("DELETE FROM bloom_events WHERE trade_date=?", (trade_date,))
    for ordinal, event in enumerate(events):
        event_hash = _hash(event)[:24]
        conn.execute(
            "INSERT INTO bloom_events VALUES(?,?,?,?,?,?,?)",
            (
                trade_date, event_hash, ordinal, _code(event), event.get("event_type"),
                event.get("strategy_version"), _json(event),
            ),
        )


def load_bloom_state(conn: sqlite3.Connection, trade_date: str) -> dict[str, dict]:
    row = conn.execute(
        "SELECT document_id FROM current_documents WHERE module='bloom' AND trade_date=?",
        (trade_date,),
    ).fetchone()
    if not row:
        return {}
    return {
        item["code"]: json.loads(item["payload_json"])
        for item in conn.execute(
            "SELECT code,payload_json FROM bloom_daily_snapshots WHERE document_id=? ORDER BY ordinal",
            (row[0],),
        )
    }


def load_all_bloom_events(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT payload_json FROM bloom_events ORDER BY trade_date,ordinal"
    )]


VCP_LIST_STATUSES = ("EARLY", "FORMING", "MATURE", "TRIGGERED", "RISK_BLOCKED")


def load_vcp_selection_events(
    conn: sqlite3.Connection,
    through_date: str | None = None,
    valid_trade_dates: set[str] | None = None,
    active_statuses: tuple[str, ...] = VCP_LIST_STATUSES,
) -> list[dict]:
    """Return the first VCP-list appearance for each stock and structure round."""
    placeholders = ",".join("?" for _ in active_statuses)
    date_condition = "AND b.trade_date<=?" if through_date else ""
    args = [*active_statuses]
    if through_date:
        args.append(through_date)
    rows = conn.execute(
        f"""SELECT b.trade_date,b.code,b.payload_json,
                   q.structure_anchor,q.structure_stage,q.structure_score,
                   q.structure_risk_score,q.strategy_version,q.payload_json
              FROM bloom_daily_snapshots b
              JOIN current_documents bc ON bc.document_id=b.document_id AND bc.module='bloom'
              LEFT JOIN current_documents qc
                ON qc.module='quant' AND qc.trade_date=b.trade_date
              LEFT JOIN vcp_structure_snapshots q
                ON q.document_id=qc.document_id AND q.code=b.code
             WHERE b.bloom_status IN ({placeholders}) {date_condition}
             ORDER BY b.trade_date,b.ordinal,b.code""",
        args,
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    events = []
    for row in rows:
        if valid_trade_dates is not None and row[0] not in valid_trade_dates:
            continue
        bloom = json.loads(row[2])
        quant = json.loads(row[8]) if row[8] else {}
        first_seen = str(bloom.get("first_seen") or row[0])
        anchor = str(row[3] or quant.get("setup_structure_anchor_date") or f"unanchored:{first_seen}")
        key = (row[1], anchor)
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "event_type": "VCP_SELECTION",
            "selection_date": row[0],
            "signal_date": row[0],
            "code": row[1],
            "name": bloom.get("name") or quant.get("name") or row[1],
            "structure_anchor": anchor,
            "initial_stage": bloom.get("model2_stage") or row[4] or "NONE",
            "initial_bloom_status": bloom.get("bloom_status") or "",
            "structure_score": bloom.get("structure_score") if bloom.get("structure_score") not in (None, "") else row[5],
            "structure_risk_score": bloom.get("structure_risk_score") if bloom.get("structure_risk_score") not in (None, "") else row[6],
            "selection_close_snapshot": bloom.get("close") or quant.get("close"),
            "quant_strategy_version": row[7] or quant.get("strategy_version") or "",
            "bloom_strategy_version": bloom.get("strategy_version") or "",
        })
    return events


def buy_event_id(event: dict) -> str:
    raw = "|".join((
        str(event.get("plan_date") or ""), str(event.get("entry_date") or ""), _code(event),
        str(event.get("structure_anchor") or "unanchored"), str(event.get("setup_family") or ""),
        str(event.get("entry_action") or ""),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def save_buy_point_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    count = 0
    for event in events:
        event_id = buy_event_id(event)
        plan_id = _plan_id(str(event.get("plan_date") or ""), event)
        conn.execute(
            """INSERT INTO buy_point_events
               (event_id,plan_id,plan_date,entry_date,code,name,structure_anchor,setup_family,
                entry_action,entry_grade,reference_entry_price,initial_invalid_price,
                quant_strategy_version,plan_strategy_version,payload_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 plan_id=excluded.plan_id,
                 plan_date=excluded.plan_date,
                 entry_date=excluded.entry_date,
                 code=excluded.code,
                 name=excluded.name,
                 structure_anchor=excluded.structure_anchor,
                 setup_family=excluded.setup_family,
                 entry_action=excluded.entry_action,
                 entry_grade=excluded.entry_grade,
                 reference_entry_price=excluded.reference_entry_price,
                 initial_invalid_price=excluded.initial_invalid_price,
                 quant_strategy_version=excluded.quant_strategy_version,
                 plan_strategy_version=excluded.plan_strategy_version,
                 payload_json=excluded.payload_json""",
            (
                event_id, plan_id, event.get("plan_date"), event.get("entry_date"), _code(event),
                event.get("name"), event.get("structure_anchor"), event.get("setup_family"),
                event.get("entry_action"), event.get("entry_grade"), event.get("signal_close_snapshot"),
                event.get("invalid_price"), event.get("quant_strategy_version"),
                event.get("plan_strategy_version"), _json(event),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        count += 1
    conn.commit()
    return count


def load_buy_point_events(conn: sqlite3.Connection, through_date: str | None = None) -> list[dict]:
    sql = "SELECT payload_json FROM buy_point_events"
    args: tuple = ()
    if through_date:
        sql += " WHERE entry_date<=?"
        args = (through_date,)
    sql += " ORDER BY entry_date,code,event_id"
    return [json.loads(row[0]) for row in conn.execute(sql, args)]


def replace_lifecycle_rows(conn: sqlite3.Connection, event_id: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM buy_point_lifecycle_daily WHERE event_id=?", (event_id,))
    for row in rows:
        conn.execute(
            """INSERT INTO buy_point_lifecycle_daily
               (event_id,trade_date,age_trade_days,open,high,low,close,current_return_pct,
                current_r,mfe_pct,mfe_r,mae_pct,mae_r,hit_1r,hit_2r,hit_3r,
                invalid_touched,invalid_confirmed,lifecycle_status,payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, row["trade_date"], row["age_trade_days"], row.get("open"), row.get("high"),
                row.get("low"), row.get("close"), row.get("current_return_pct"), row.get("current_r"),
                row.get("mfe_pct"), row.get("mfe_r"), row.get("mae_pct"), row.get("mae_r"),
                int(bool(row.get("hit_1r"))), int(bool(row.get("hit_2r"))), int(bool(row.get("hit_3r"))),
                int(bool(row.get("invalid_touched"))), int(bool(row.get("invalid_confirmed"))),
                row.get("lifecycle_status") or "OPEN", _json(row),
            ),
        )
    conn.commit()


def lifecycle_latest(conn: sqlite3.Connection, through_date: str | None = None) -> list[dict]:
    condition = "AND trade_date<=?" if through_date else ""
    args = (through_date,) if through_date else ()
    rows = conn.execute(
        f"""SELECT l.payload_json FROM buy_point_lifecycle_daily l
            JOIN (SELECT event_id,max(trade_date) AS trade_date
                  FROM buy_point_lifecycle_daily WHERE 1=1 {condition}
                  GROUP BY event_id) latest
              ON latest.event_id=l.event_id AND latest.trade_date=l.trade_date
            ORDER BY l.trade_date,l.event_id""",
        args,
    )
    return [json.loads(row[0]) for row in rows]
