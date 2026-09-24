#!/usr/bin/env python3
"""Read-only venue evidence for the acceptance protocol. Never places an order.

    python scripts/probe_account.py --config var/config.json --output evidence/venue-probe.json

Records what the account IS -- broker, id, server, type, currency, equity,
leverage, contract specs, live spread and the round-trip cost it implies --
and whether a server-side stop was actually observed on an open position.
``run_acceptance.py --venue-probe`` feeds the environment gates from it
instead of from a profile's declaration, and refuses it after seven days.

No position means no stop was observed: that is reported as such, not as
success. Open a small DEMO position with a stop during integration testing
if you need that line to read true; never a live one for this purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import SentinelConfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="var/config.json")
    ap.add_argument("--output", required=True)
    ap.add_argument("--symbol", default="EUR_USD")
    args = ap.parse_args()

    cfg = SentinelConfig.load(args.config)
    from sentinel.bootstrap import _connection_kwargs
    from sentinel.brokers import build_broker
    from sentinel.brokers.connection import ReadOnlyBroker
    from sentinel.core.audit import NullAudit
    kwargs = _connection_kwargs(cfg, Path(cfg.ops.state_dir), NullAudit())
    if cfg.execution.broker != "paper":
        broker = ReadOnlyBroker(build_broker(cfg.execution.broker, **kwargs))
    else:
        from sentinel.bootstrap import DEFAULT_INSTRUMENTS
        from sentinel.brokers.paper import PaperBroker
        broker = ReadOnlyBroker(PaperBroker(instruments=DEFAULT_INSTRUMENTS))
    try:
        account = broker.account()
        instruments = broker.instruments()
        inst = instruments.get(args.symbol)
        if inst is None:
            raise SystemExit(f"{args.symbol} is not offered by this venue")
        quote = broker.quote(args.symbol)
        conv = broker.conversion_rate(inst.quote, account.currency)
        if conv is None or conv <= 0:
            raise SystemExit(f"no {inst.quote}->{account.currency} conversion")
        pip_value = inst.contract_size * inst.pip * conv
        spread_pips = (quote.ask - quote.bid) / inst.pip
        cost = (spread_pips + cfg.execution.commission_per_lot_round_turn / pip_value
                + cfg.execution.expected_slippage_pips * 2)
        positions = broker.positions()
        protected = [p for p in positions if p.stop_loss is not None and p.broker_stop_confirmed]
        try:
            min_stop = broker.min_stop_distance(args.symbol)
        except Exception:  # noqa: BLE001
            min_stop = None
        report = {
            "schema": 1, "observed_at_ns": time.time_ns(),
            "broker": cfg.execution.broker,
            "account_id": str(account.account_id),
            "account_type": account.account_type or "",
            "server": cfg.execution.expected_account_server,
            "venue_name": account.venue_name,
            "currency": account.currency, "equity": str(account.equity),
            "balance": str(account.balance), "leverage": account.leverage,
            "symbol": args.symbol,
            "pip_value_per_lot": str(pip_value), "spread_pips": str(spread_pips),
            "round_trip_cost_pips": str(cost),
            "min_stop_distance": str(min_stop) if min_stop is not None else None,
            "instrument_specs": {k: {"pip": str(v.pip), "tick": str(v.tick),
                                     "contract_size": str(v.contract_size),
                                     "min_lot": str(v.min_lot), "lot_step": str(v.lot_step)}
                                 for k, v in instruments.items() if k in cfg.agent.semi_auto_envelope.get("instruments", []) or k == args.symbol},
            "open_positions": len(positions),
            "server_stops_observed": bool(protected),
            "protected_tickets": [p.venue_position_id for p in protected if p.venue_position_id],
            "observation": "existing venue stops only; no test order was sent",
            "capabilities": asdict(broker.capabilities),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"read-only probe saved: {out}")
        print(f"  account {report['account_id']} ({report['account_type'] or 'type unknown'}) "
              f"{report['currency']} equity {report['equity']}")
        print(f"  {args.symbol} spread {spread_pips:.2f}p, round trip ~{cost:.2f}p")
        if not protected:
            print("  no open position with a venue-side stop was observed; gate L0.4 "
                  "reads 'not supported' from this probe until one is.")
    finally:
        try:
            broker.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
