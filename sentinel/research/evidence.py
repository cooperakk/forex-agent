"""Evidence the acceptance protocol reads by CONTENT, never by a caller's label.

Three kinds, three verifiers:

* **Dataset** -- a directory of per-instrument CSVs plus a manifest naming the
  broker, the price basis, the cost schedule and the SHA-256 of every file.
  ``verify_dataset`` is what turns "--data-label live-quality" from a claim
  into a checked statement: real bid/ask columns, a complete cost schedule
  (commission, slippage, BOTH swap tables, every instrument), file hashes
  that match. Gate L10 reads its result.

* **Forward record** -- the closed trades and the floating-equity marks of a
  demo or live run under the configuration being promoted, with the runtime
  fingerprint they were produced under. ``verify_forward`` reconciles the
  ledger (starting equity + cumulative net P&L == realised balance, to the
  cent), checks that the equity marks cover the whole period, and measures
  the drawdown on floating equity, not just on closed trades. Gate L12.

* **Venue probe** -- a read-only snapshot of the account the verdict is for:
  broker, account id, server, type, currency, contract specs, an observed
  server-side stop if any position was open. Fresh within seven days. Gate
  L0 reads the capabilities from it instead of from a constant.

What a hash proves, stated plainly: that the file the protocol read is the
file the manifest described. It does not prove the file came from the
broker. That is the operator's word, recorded as such in every result.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ..data.validation import validate_universe

REQUIRED_COST_KEYS = ("commission_per_lot_round_turn", "slippage_pips_mean",
                      "slippage_pips_sigma", "swap_long_pips_per_day", "swap_short_pips_per_day")
VENUE_PROBE_MAX_AGE_SEC = 7 * 86400


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# dataset
# --------------------------------------------------------------------------- #


def verify_dataset(directory: str | Path, frames: Dict[str, pd.DataFrame],
                   manifest_path: str | Path) -> Dict[str, Any]:
    """Check a bar directory against its manifest. Raises with the reason."""
    directory = Path(directory)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    validate_universe(frames, bid_ask=True)
    if not manifest.get("broker"):
        raise ValueError("manifest must name the broker the export came from")
    if manifest.get("price_basis") != "mid-with-bid-ask":
        raise ValueError("manifest price_basis must be 'mid-with-bid-ask': the OHLC must be "
                         "built from mid ticks and the bid/ask OHLC from their own ticks")
    cost = manifest.get("cost_schedule") or {}
    missing = [k for k in REQUIRED_COST_KEYS if k not in cost]
    if missing:
        raise ValueError(f"cost schedule incomplete: missing {missing}")
    for key in ("commission_per_lot_round_turn", "slippage_pips_mean", "slippage_pips_sigma"):
        v = float(cost[key])
        if not np.isfinite(v) or v < 0:
            raise ValueError(f"cost schedule {key} must be a finite non-negative number")
    for key in ("swap_long_pips_per_day", "swap_short_pips_per_day"):
        table = cost[key]
        if not isinstance(table, dict):
            raise ValueError(f"{key} must map instrument -> pips per day")
        absent = sorted(set(frames) - set(table))
        if absent:
            raise ValueError(f"{key} has no entry for {absent}; an explicit 0 is required "
                             "where the venue charges none")
        if not all(np.isfinite(float(v)) for v in table.values()):
            raise ValueError(f"{key} contains a non-finite value")
    hashes: Dict[str, str] = {}
    declared = manifest.get("files") or {}
    for symbol in frames:
        file = directory / f"{symbol}.csv"
        if not file.exists():
            raise ValueError(f"{file.name} named in the universe is not in {directory}")
        digest = sha256_file(file)
        if declared.get(file.name) != digest:
            raise ValueError(f"dataset hash mismatch for {file.name}: the file is not the "
                             "one the manifest describes")
        hashes[file.name] = digest
    return {
        "verified": True, "sha256": hashes, "bid_ask": True,
        "broker": manifest["broker"], "cost_schedule": cost,
        "price_basis": manifest["price_basis"],
        "exported_at": manifest.get("exported_at"),
        "authenticity": "operator-supplied broker export; hashes attest identity, not origin",
    }


def write_dataset_manifest(directory: str | Path, *, broker: str, cost_schedule: Dict[str, Any],
                           output: str | Path, price_basis: str = "mid-with-bid-ask",
                           note: str = "") -> Dict[str, Any]:
    """Hash every CSV in ``directory`` into a manifest the verifier accepts."""
    directory = Path(directory)
    files = {p.name: sha256_file(p) for p in sorted(directory.glob("*.csv"))}
    if not files:
        raise ValueError(f"no CSV files in {directory}")
    missing = [k for k in REQUIRED_COST_KEYS if k not in cost_schedule]
    if missing:
        raise ValueError(f"cost schedule incomplete: missing {missing}")
    body = {
        "schema": 1, "broker": broker, "price_basis": price_basis,
        "cost_schedule": cost_schedule, "files": files,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": note,
    }
    Path(output).write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return body


# --------------------------------------------------------------------------- #
# forward record
# --------------------------------------------------------------------------- #

FORWARD_TRADE_COLUMNS = ("trade_id", "opened_at", "closed_at", "net_pnl",
                         "balance_after", "account_id")


def verify_forward(manifest_path: str | Path, runtime_hash: str) -> Dict[str, Any]:
    """Check a forward-test record against the runtime it claims. Raises with the reason."""
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("runtime_hash") != runtime_hash:
        raise ValueError("forward record belongs to a different runtime fingerprint; the "
                         "configuration or the code changed since it was produced")
    root = path.resolve().parent
    trades_file = (root / str(manifest.get("trades_file", ""))).resolve()
    equity_file = (root / str(manifest.get("equity_file", ""))).resolve()
    for f in (trades_file, equity_file):
        if root not in f.parents:
            raise ValueError("forward evidence files must sit beside the manifest")
    if sha256_file(trades_file) != manifest.get("sha256"):
        raise ValueError("forward trades file hash mismatch")
    if sha256_file(equity_file) != manifest.get("equity_sha256"):
        raise ValueError("forward equity file hash mismatch")

    df = pd.read_csv(trades_file)
    if "equity_after" in df.columns and "balance_after" not in df.columns:
        df = df.rename(columns={"equity_after": "balance_after"})
    missing = [c for c in FORWARD_TRADE_COLUMNS if c not in df.columns]
    if missing or df.empty:
        raise ValueError(f"forward trades need columns {FORWARD_TRADE_COLUMNS}; missing {missing}")
    if df["trade_id"].duplicated().any():
        raise ValueError("duplicate trade ids in the forward record")
    opened = pd.to_datetime(df["opened_at"], utc=True, errors="coerce")
    closed = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    if opened.isna().any() or closed.isna().any():
        raise ValueError("unparseable forward trade timestamps")
    if (closed <= opened).any():
        raise ValueError("a forward trade closes before it opens")
    if not closed.is_monotonic_increasing:
        raise ValueError("forward trades must be ordered by close time")
    if (closed > pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("a forward trade closes in the future")
    accounts = df["account_id"].astype(str).unique()
    if len(accounts) != 1 or accounts[0] != str(manifest.get("account_id")):
        raise ValueError("forward trades must all belong to the manifest's account")

    pnl = df["net_pnl"].to_numpy(dtype=float)
    balance = df["balance_after"].to_numpy(dtype=float)
    start = float(manifest.get("starting_equity", 0))
    if not (np.isfinite(pnl).all() and np.isfinite(balance).all() and np.isfinite(start)):
        raise ValueError("non-finite forward amounts")
    if start <= 0 or (balance <= 0).any():
        raise ValueError("forward balances must be positive")
    if not np.allclose(start + np.cumsum(pnl), balance, atol=0.02, rtol=0):
        raise ValueError("forward ledger fails reconciliation: starting equity plus "
                         "cumulative net P&L must equal the balance after each trade "
                         "(deposits and withdrawals must be excluded)")
    realised_marks = np.r_[start, balance]
    dd_realised = float(np.max(1 - realised_marks / np.maximum.accumulate(realised_marks)) * 100)

    marks = pd.read_csv(equity_file)
    if not {"timestamp", "equity"} <= set(marks.columns) or marks.empty:
        raise ValueError("forward equity file needs timestamp,equity columns")
    times = pd.to_datetime(marks["timestamp"], utc=True, errors="coerce")
    eq = marks["equity"].to_numpy(dtype=float)
    if times.isna().any() or not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError("forward equity marks must be unique, increasing timestamps")
    if not np.isfinite(eq).all() or (eq <= 0).any():
        raise ValueError("forward equity marks must be finite and positive")
    if times.iloc[0] > opened.min() or times.iloc[-1] < closed.max():
        raise ValueError("equity marks do not cover the whole trading record")
    dd_floating = float(np.max(1 - eq / np.maximum.accumulate(eq)) * 100)
    return {
        "verified": True, "runtime_hash": runtime_hash, "n_trades": int(len(df)),
        "net_pnl": float(pnl.sum()), "net_return_pct": float(pnl.sum() / start * 100),
        "max_drawdown_pct": max(dd_realised, dd_floating),
        "max_drawdown_realised_pct": dd_realised, "max_drawdown_floating_pct": dd_floating,
        "first_open": opened.min().isoformat(), "last_close": closed.max().isoformat(),
        "account_id": str(manifest["account_id"]), "sha256": manifest["sha256"],
        "equity_marks": int(len(marks)),
    }


# --------------------------------------------------------------------------- #
# venue probe
# --------------------------------------------------------------------------- #


def verify_venue(probe_path: str | Path, config, *, now_ns: Optional[int] = None) -> Dict[str, Any]:
    """Check a read-only venue probe against the configuration it is for."""
    body = json.loads(Path(probe_path).read_text(encoding="utf-8"))
    execution = config.execution
    expected = {
        "broker": execution.broker,
        "account_id": str(execution.expected_account_id),
        "server": execution.expected_account_server,
        "account_type": execution.venue_mode.value,
        "currency": execution.account_currency,
    }
    mismatches = [k for k, v in expected.items()
                  if str(body.get(k, "")) != str(v) and not (k == "server" and not v)]
    if mismatches:
        raise ValueError(f"venue probe does not match the intended account on {mismatches}")
    observed = int(body.get("observed_at_ns", 0) or 0)
    now = now_ns if now_ns is not None else time.time_ns()
    age = (now - observed) / 1e9
    if not 0 <= age <= VENUE_PROBE_MAX_AGE_SEC:
        raise ValueError("venue probe must be no older than seven days (and not from the future)")
    for key in ("equity", "pip_value_per_lot", "round_trip_cost_pips"):
        v = float(body.get(key, "nan"))
        if not np.isfinite(v) or v <= 0:
            raise ValueError(f"venue probe {key} must be a positive finite number")
    if body.get("server_stops_observed") and not body.get("protected_tickets"):
        raise ValueError("venue probe claims an observed server-side stop but lists no ticket")
    return body
