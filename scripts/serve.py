#!/usr/bin/env python3
"""Start the Sentinel-FX engine and dashboard API."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from sentinel.api.main import create_app
from sentinel.bootstrap import build_runtime
from sentinel.core.config import SentinelConfig


def _default_config_for(cfg_path: Path) -> SentinelConfig:
    """A fresh configuration whose state paths sit beside the config file.

    The defaults are relative ("var/audit.jsonl") because that is right when
    you run from a checkout. In a container the config lives at /data/config.json
    and the root filesystem is read-only, so a relative default would try to
    write into the image and fail at the worst possible moment -- after the
    engine has already decided to place an order. Rebase them once, at the
    moment the file is created, and the config on disk then says plainly where
    its state lives rather than depending on the working directory.
    """
    cfg = SentinelConfig()
    root = cfg_path.resolve().parent

    # No special case for "the config happens to be in the CWD". Keeping the
    # paths relative there left the engine writing its state wherever it was
    # started from, while the watchdog unit watches an absolute path -- so a
    # service started with a different WorkingDirectory silently wrote its
    # heartbeat somewhere the watchdog could not see it, and the kill switch
    # engaged on boot for no visible reason.
    def rebase(value: str) -> str:
        return value if Path(value).is_absolute() else str(root / value)

    cfg.ops.state_dir = rebase(cfg.ops.state_dir)
    cfg.ops.audit_log = rebase(cfg.ops.audit_log)
    cfg.ops.killswitch_file = rebase(cfg.ops.killswitch_file)
    if cfg.ops.backup_dir:
        cfg.ops.backup_dir = rebase(cfg.ops.backup_dir)
    cfg.data.store_path = rebase(cfg.data.store_path)

    # A first-run configuration with no strategies is a process that thinks
    # and never speaks: `_consider_entries` iterates the allocations and there
    # are none. The dashboard's strategy switches are deliberately read-only
    # (a strategy's lifecycle is not something a browser click may change), so
    # a fresh install had no path at all from "installed" to "produces a
    # decision". These are HYPOTHESIS allocations: they may trade the paper
    # venue and a demo account, and the risk engine refuses them real money
    # until the acceptance protocol says otherwise. `scripts/manage_strategies.py`
    # edits the list afterwards.
    from sentinel.core.config import StrategyAllocation
    cfg.strategies = [StrategyAllocation(**spec) for spec in DEMO_ALLOCATIONS]
    return cfg


#: The starter set: three H4 systems from different families on the majors.
#: H4 because the served feed is H4 (see bootstrap.build_runtime); a strategy
#: declared on another timeframe would be handed the wrong bars.
DEMO_INSTRUMENTS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CHF"]
DEMO_ALLOCATIONS = [
    {"name": "donchian_trend", "enabled": True, "instruments": DEMO_INSTRUMENTS,
     "timeframe": "H4", "lifecycle": "hypothesis"},
    {"name": "ma_cross_atr", "enabled": True, "instruments": DEMO_INSTRUMENTS,
     "timeframe": "H4", "lifecycle": "hypothesis"},
    {"name": "inside_bar_break", "enabled": True, "instruments": DEMO_INSTRUMENTS,
     "timeframe": "H4", "lifecycle": "hypothesis"},
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel-FX server")
    ap.add_argument("--config", default=os.environ.get("SENTINEL_CONFIG", "var/config.json"))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--dashboard", default="dashboard/dist")
    ap.add_argument("--no-loop", action="store_true",
                    help="serve the API without running the decision loop")
    ap.add_argument("--trusted-proxy", default=os.environ.get("SENTINEL_TRUSTED_PROXY"),
                    help="comma-separated IPs/CIDRs of reverse proxies whose "
                         "X-Forwarded-For header may be believed. Unset means "
                         "the header is ignored entirely, which is correct for "
                         "a loopback bind or an SSH tunnel.")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        _default_config_for(cfg_path).save(cfg_path)
        print(f"[serve] wrote a default configuration to {cfg_path}")

    runtime, security = build_runtime(cfg_path)
    cfg = runtime.agent.config
    host = args.host or cfg.security.bind_host
    port = args.port or cfg.security.bind_port

    # create_app used to derive "is this public?" from the CONFIG's bind_host,
    # while uvicorn bound whatever --host said. The shipped Dockerfile passes
    # --host 0.0.0.0, so in the container the SENTINEL_ALLOW_PUBLIC_BIND
    # refusal was never evaluated and the dashboard's permanent exposure banner
    # never appeared -- the two controls that exist for exactly that case.
    # Pass the host actually being bound.
    app = create_app(runtime, security,
                     dashboard_dir=args.dashboard if Path(args.dashboard).is_dir() else None,
                     bind_host=host)
    runtime.start(run_loop=not args.no_loop)
    print(f"[serve] mode={cfg.agent.mode.value} venue={cfg.execution.venue_mode.value} "
          f"broker={cfg.execution.broker}")
    print(f"[serve] listening on http://{host}:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("[serve] WARNING: this port is reachable beyond loopback. A dashboard with "
              "trading authority on an open port is equivalent to publishing the account.")
    try:
        # proxy_headers=False by default. uvicorn otherwise trusts
        # X-Forwarded-For from any loopback peer -- which is precisely the
        # documented access path (ssh -L). A client could then choose its own
        # apparent address, defeating the per-IP lockout, the login rate limit
        # and the session fingerprint, and writing arbitrary bytes into the
        # audit chain's `actor` field. Opt in explicitly with --trusted-proxy.
        uvicorn.run(app, host=host, port=port, log_level=cfg.ops.log_level.lower(),
                    access_log=False,
                    proxy_headers=bool(args.trusted_proxy),
                    forwarded_allow_ips=args.trusted_proxy or None)
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
