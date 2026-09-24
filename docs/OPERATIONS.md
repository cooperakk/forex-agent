# Sentinel-FX — Operations runbook

The short document you want open when something is wrong.

## Stop everything, right now

```bash
touch var/KILL                                        # local
docker compose exec engine touch /data/var/KILL       # docker
sudo -u sentinel touch /var/lib/sentinel/var/KILL     # systemd
```

No API call, no authentication, no dependency on the process being healthy.
New risk stops on the next cycle. **Open positions keep their venue-side
stops** — the kill switch stops new risk, it does not liquidate.

To liquidate as well, use `POST /api/control/flatten` (operator + TOTP), or
close positions at the broker directly.

Release requires owner + TOTP from the dashboard, or deleting the file.

## Daily, five minutes

```bash
make verify-audit                      # chain intact?
curl -s localhost:8088/api/health      # loop alive, clock sane, feed fresh?
```

On the dashboard: the risk page (which vetoes fired and how often), the
positions page (**every position has a stop** — if not, the
`unprotected_book` veto should already be blocking new entries), and the
decision journal.

A veto firing constantly is information, not noise. `cost_barrier` every cycle
means the geometry is wrong. `stale_data` every cycle means the feed is
broken. `missing_conversion` means the account currency and the instruments
disagree.

## Weekly

- Read the postmortems. The question is not "did it win" but "was the losing
  trade a correct decision given what was knowable at entry".
- Check the proposal queue. Approving one invalidates that strategy's verdict
  fingerprint — by design. Re-run acceptance before it trades again.
- Verify a backup **restores**, not just that it exists.

---

## Symptoms

### The loop has stopped but the process is up

The API answers, the dashboard loads, nothing happens. The heartbeat file is
what distinguishes this from a healthy system:

```bash
cat var/heartbeat.json    # ts_ns should be within a few seconds of now
```

The watchdog should already have engaged the kill switch after 45 s. If it has
not, the watchdog is not running — check it first.

### Orders in `unknown` state

Expected after a network interruption. `unresolved_orders` blocks new entries
until they resolve, which is correct. The reconciler queries the venue every
30 s and on startup.

If one is stuck: check the venue's own order history for the client order id
(printed in the audit log next to `order.sent`). **Do not** delete the record
to clear the block — the whole point is that an unresolved order might be a
live position.

### Reconciliation mismatches

`ops.reconcile_mismatch` in the audit log means local belief disagreed with the
venue. The venue wins and the system adopts its view. One mismatch after a
restart is normal. Repeated mismatches on the same instrument mean something
is placing orders outside the system — check for a second instance running
against the same account, which is the failure mode this specifically catches.

### The system halted itself

Look for `risk.halt` in the audit log; the reason is in the record. Common
causes: a loss budget hit, the drawdown ladder reaching its halt step, a
position/side mismatch on restart hydration, or a failed close.

Resume requires owner + TOTP and is deliberately not automatic. Read the
reason first.

### Sizing looks wrong

Check, in this order: the ladder step (`risk.ladder_step`), the regime
multiplier, whether a conversion rate is missing, and `L0.2` — below the
granularity threshold the 0.01-lot floor distorts the risk budget by more than
20 % and no amount of configuration fixes it.

### A strategy stopped trading after a config change

Correct. Changing a parameter changes the configuration fingerprint, which
revokes the verdict. Check the audit log for
`startup_authority_violation`. Re-run the acceptance protocol.

---

## Restart safely

```bash
# 1. stop new risk first
touch var/KILL
# 2. wait one full decision cycle (default 60s)
# 3. restart
docker compose restart engine     # or: systemctl restart sentinel-engine
# 4. watch the startup replay resolve unterminated orders
docker compose logs -f engine
# 5. release only after positions and stops reconcile
```

On startup the agent replays unterminated orders from the journal and queries
each one *before* doing anything else, re-hydrates position metadata matched on
**side** (halting on mismatch), and restores the equity peak and period
baselines. Skipping step 1 is usually survivable; it is simply not worth it.

### Somebody needs access, or has lost it

```bash
python scripts/manage_users.py list
python scripts/manage_users.py add     --username alice --role viewer
python scripts/manage_users.py passwd  --username alice     # TOTP unchanged
python scripts/manage_users.py disable --username bob       # drops live sessions
```

A lost authenticator is the one case that needs a rebuild: there is no way to
recover a TOTP secret from the store as a human-readable code path, and there
should not be. Disable the account and create a new one.

---

## Recovering state

Losing `var/` loses the drawdown ladder's memory, the acceptance history and
every account.
Restore the whole directory from a backup rather than individual files — the
agent state, the verdict registry and the audit chain are consistent with each
other at snapshot time.

If `verdicts.db` is unreadable the process refuses to start. That is
deliberate: the automatic "repair" would demote every accepted strategy and
persist it. Restore from a backup; re-run acceptance only if no backup exists.

---

## Escalation, honestly

If you cannot tell what the system did, the audit log can:

```bash
grep '"order' var/audit.jsonl | tail -50
grep '"decision.risk_veto"' var/audit.jsonl | tail -50
grep '"risk.halt"\|"ops.kill_switch"' var/audit.jsonl
```

Every decision carries its reasoning. That is what the journal is for, and it
is why it is an append-only text file rather than a database.
