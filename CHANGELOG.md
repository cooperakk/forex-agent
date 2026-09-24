# Changelog

## 1.5.0 -- 2026-09-24

An end-to-end audit of 1.4.0 followed by the features the owner asked for.
Every defect below has a regression test that fails against 1.4.0.

### Fixed -- capital preservation

- **Accepting a proposal discarded the agent's shrinkage.** The risk engine
  sizes from the full per-trade budget and that size was sent as-is, so a human
  click could place up to 1/caution times the agent's own proposal. Lessons,
  news and the meta-label scale are now re-applied at acceptance, never looser
  than when the proposal was made. The strategy's horizon (time stop) travels
  with the proposal.
- A strategy **suspended by the performance guard** could still trade through a
  queued proposal.
- **The licence was checked once, at boot**: an expiry during a long uptime
  kept authorising live entries. It is now asked every cycle and on every human
  entry path; exits and protection are never gated.
- The **rolling 24 h loss window** covered ~7 h at short decision intervals and
  was forgotten by restarts.
- The **break-even stop** used default costs instead of the configured
  commission and slippage.
- A zero period baseline read the whole account as today's profit.

### Fixed -- correctness and availability

- The Linux installer shipped with **CRLF line endings** and could not run at
  all ("bash\r: No such file or directory"). Line endings are now pinned by
  `.gitattributes` and tested.
- The Linux installer used Python 3.10 on Ubuntu 22.04 (3.11+ is required).
- 48 API handlers were `async` while doing blocking I/O, so one slow broker
  froze the whole API **including the kill switch**.
- `/api/audit` returned the oldest records and re-hashed the whole journal on
  every poll.
- Meta-label diagnostics were erased before reaching the dashboard; trade-id
  trimming kept old ids; manual closes produced spurious `phantom` mismatches;
  malformed `Content-Length` escaped the error handler.
- Paths the process loads or executes (the pickled meta model, the plugin
  directory, data/backup/ledger locations) could be changed from the dashboard.
- The performance-guard suspension could not be seen or released from the
  console.

### Security -- licensing

- The vendor key is **embedded** at release time (`licensegen.py embed-key`);
  the environment can no longer switch licensing off or substitute a key.
- A distributed build **requires** its signed manifest; the manifest covers the
  files that wire the gate in.
- **Online activation** (`activation_url`) is implemented: Ed25519-signed,
  nonce-bound leases; offline tolerance until the lease expires; revocation
  and seat limits; server time as a rollback reference. Reference server:
  `scripts/license_server.py`.
- **Protected builds** (`scripts/build_protected.py`): vendor tools removed,
  keys embedded, protected modules compiled to native code with Cython and
  their sources deleted, manifest signed over the binaries.
- Malformed licences raise `LicenseInvalid` (never a bare exception); licence
  ids are unique.

### Added

- **AI assistants** (`sentinel/ai`): Claude, ChatGPT, Gemini, DeepSeek, Kimi,
  **Jev (TypeSafe AI System One, used for probabilistic news classification)**
  and any OpenAI-compatible endpoint; sealed write-only keys; fallback chain;
  budgets; journalled calls. Used for official-news extraction (shrink/block
  only), the **trade coach** (plain-Persian review of each closed trade), and a
  **daily brief**. See `docs/AI-PROVIDERS.md`.
- **Live news**: confirmed economic calendar and official central-bank feeds;
  blackout windows are now real.
- **Manual trading**: a ticket with a mandatory stop, sized by the risk budget
  and judged by every veto; preview mode; real money off by default
  (`agent.manual_trading_live`).
- **Dashboard**: "معامله دستی" and "هوش مصنوعی و اخبار" pages, guard release,
  meta probability and caution on each decision, licence and guard banners.
- **Windows Server** deployment (`deploy/windows`): installer, supervisor,
  environment check, diagnostics with support bundle, kill switch, backup,
  update with rollback, uninstall. See `docs/WINDOWS-SERVER.md`.
- **Linux** `check-environment.sh`; broader backup coverage;
  `scripts/backup_state.py` (portable backup).

Tests: 961 -> 1086 passing (+2 platform-specific skips).

## 1.4.0 and earlier

See `docs/HANDOFF-1.4.0-FA.md`.
