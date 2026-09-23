"""Pure state transitions for Pool RS candidate tracking."""

from __future__ import annotations

from collections.abc import Iterable


ACTIVE = "RS_ACTIVE"
STRUCTURE = "STRUCTURE_TRACKED"
GRACE = "GRACE_TRACKING"
EXITED = "EXITED"


def prepare_tracking_rows(
    trade_date: str,
    candidates: Iterable[dict],
    previous: dict[str, dict],
    trade_dates: list[str],
    grace_limit: int,
    bootstrap_codes: set[str] | None = None,
    strategy_version: str = "",
) -> list[dict]:
    """Build the idempotent pre-Quant snapshot for one trade date."""
    bootstrap_codes = bootstrap_codes or set()
    calendar = sorted(set(trade_dates))
    rows = []
    for candidate in candidates:
        code = str(candidate.get("code") or "").zfill(6)
        prior = previous.get(code) or {}
        current_rs = bool(candidate.get("rs_current_eligible"))
        hard_reason = str(candidate.get("hard_filter_reason") or "")
        was_exited = prior.get("tracking_status") == EXITED
        first_seen = (
            trade_date if current_rs and was_exited
            else prior.get("first_seen_date") or trade_date
        )
        last_rs = trade_date if current_rs else prior.get("last_rs_eligible_date")
        grace_anchor = None
        grace_days = 0
        exit_reason = None

        if hard_reason:
            status = EXITED
            exit_reason = f"HARD_FILTER:{hard_reason}"
        elif current_rs:
            status = ACTIVE
        elif code in bootstrap_codes and not prior:
            status = STRUCTURE
        elif prior.get("tracking_status") == STRUCTURE:
            status = STRUCTURE
        else:
            grace_anchor = prior.get("grace_start_date") or last_rs or prior.get("trade_date")
            grace_days = sum(grace_anchor < day <= trade_date for day in calendar) if grace_anchor else 1
            if grace_days <= grace_limit:
                status = GRACE
            else:
                status = EXITED
                exit_reason = "GRACE_EXPIRED"

        source = "RS" if current_rs else (
            "BLOOM_BOOTSTRAP" if code in bootstrap_codes and not prior else "RS_HISTORY"
        )
        row = {
            "trade_date": trade_date,
            "code": code,
            "name": candidate.get("name") or prior.get("name") or code,
            "tracking_status": status,
            "resolution_status": "PENDING",
            "first_seen_date": first_seen,
            "last_rs_eligible_date": last_rs,
            "rs_current_eligible": current_rs,
            "grace_start_date": grace_anchor,
            "grace_trade_days": grace_days,
            "grace_remaining_days": max(0, grace_limit - grace_days),
            "tracking_source": source,
            "quant_stage": prior.get("quant_stage"),
            "bloom_status": prior.get("bloom_status"),
            "post_breakout_state": prior.get("post_breakout_state"),
            "exit_reason": exit_reason,
            "strategy_version": strategy_version,
            "_prior_tracking_status": prior.get("tracking_status"),
            "_prior_grace_start_date": prior.get("grace_start_date"),
            "_prior_grace_trade_days": prior.get("grace_trade_days"),
            "_prior_grace_remaining_days": prior.get("grace_remaining_days"),
        }
        rows.append(row)
    return rows


def finalize_tracking_rows(
    trade_date: str,
    rows: Iterable[dict],
    quant_rows: dict[str, dict],
    bloom_rows: dict[str, dict],
    grace_limit: int,
    retain_data_issue: bool = True,
) -> list[dict]:
    """Resolve current Pool rows after Quant and Bloom have completed."""
    finalized = []
    for source in rows:
        row = dict(source)
        code = row["code"]
        quant = quant_rows.get(code) or {}
        bloom = bloom_rows.get(code) or {}
        bloom_status = str(bloom.get("bloom_status") or "").upper()
        post_state = str(
            bloom.get("post_breakout_state") or quant.get("post_breakout_state") or ""
        ).upper()
        row["quant_stage"] = quant.get("structure_stage") or bloom.get("model2_stage")
        row["bloom_status"] = bloom_status or None
        row["post_breakout_state"] = post_state or None

        if row["tracking_status"] == EXITED:
            pass
        elif bloom_status == "DATA_ISSUE":
            if retain_data_issue and row.get("_prior_tracking_status"):
                row["tracking_status"] = row["_prior_tracking_status"]
                row["grace_start_date"] = row.get("_prior_grace_start_date")
                row["grace_trade_days"] = int(row.get("_prior_grace_trade_days") or 0)
                prior_remaining = row.get("_prior_grace_remaining_days")
                row["grace_remaining_days"] = int(
                    grace_limit if prior_remaining is None else prior_remaining
                )
        elif bloom_status == "EXIT" or post_state in {
            "POST_BREAKOUT_FAILED", "POST_BREAKOUT_EXPIRED"
        }:
            if row["rs_current_eligible"]:
                row["tracking_status"] = ACTIVE
                row["grace_start_date"] = None
                row["grace_trade_days"] = 0
                row["grace_remaining_days"] = grace_limit
            else:
                row["tracking_status"] = GRACE
                row["grace_start_date"] = trade_date
                row["grace_trade_days"] = 0
                row["grace_remaining_days"] = grace_limit
                row["tracking_source"] = "RS_HISTORY"
            row["exit_reason"] = None
        elif bloom_status:
            row["tracking_status"] = STRUCTURE
            row["grace_start_date"] = None
            row["grace_trade_days"] = 0
            row["grace_remaining_days"] = grace_limit
            row["exit_reason"] = None

        row["resolution_status"] = "FINAL"
        for key in [key for key in row if key.startswith("_prior_")]:
            row.pop(key, None)
        finalized.append(row)
    return finalized
