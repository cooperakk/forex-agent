# Sentinel-FX — Deployment

Three ways to run it. Pick one.

| | Use when | Effort |
|---|---|---|
| **Local** | evaluating, researching, paper trading | 5 min |
| **Docker** | a VPS you control, single host | 15 min |
| **systemd** | a VPS, no container runtime, maximum hardening | 30 min |

Read `SECURITY.md` before exposing anything to a network.

---

## 0. Before anything

**The clock.** Every decision timestamp, every idempotency key and every
session-window check depends on it. Install an NTP client and verify drift:

```bash
timedatectl status          # System clock synchronized: yes
```

**The venue.** Start on a practice account. `OANDA_API_HOST` differs between
practice (`api-fxpractice`) and live (`api-fxtrade`); mixing them up is a
common and expensive mistake.

**Which currency the account is in.** If it is not USD, the FX-conversion path
matters enormously — a JPY account with an unknown conversion rate would be a
166× sizing error, which is why the system vetoes rather than guesses.

---

## 1. Local (development / research)

```bash
tar -xzf sentinel-fx-1.0.0.tar.gz && cd sentinel-fx

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# dashboard (needs Node ≥ 20)
cd dashboard && npm ci && npm run build && cd ..

.venv/bin/python -m pytest -q          # expect: 222 passed
```

Run it:

```bash
cp .env.example .env && chmod 600 .env
# set SENTINEL_JWT_SECRET, SENTINEL_ADMIN_USER, SENTINEL_ADMIN_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"

set -a && . ./.env && set +a
.venv/bin/python scripts/serve.py
```

On first start it prints an `otpauth://` URI. Scan it with your authenticator
**now** — it is printed once, when the account is created, and the account then
persists in `var/users.db`. Then open <http://127.0.0.1:8088>.

You can skip the environment variables entirely and create the owner directly:

```bash
.venv/bin/python scripts/manage_users.py add --username owner --role owner
```

Accounts survive restarts, so `SENTINEL_ADMIN_PASSWORD` is read only when no
such account exists — an existing one is never overwritten from the
environment. Delete the variable once the owner is created.

Other entry points:

```bash
make paper          # end-to-end paper simulation
make acceptance STRATEGY=donchian_trend
make verify-audit   # check the hash chain
```

---

## 2. Docker (recommended for a server)

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env                 # JWT secret, admin user/password, broker creds

docker compose build
docker compose up -d
docker compose logs -f engine    # grab the otpauth:// URI from here
```

Two lines in `docker-compose.yml` are load-bearing:

```yaml
ports:
  - "127.0.0.1:8088:8088"     # NOT "8088:8088". See below.
volumes:
  - sentinel-data:/data       # this is the system's memory
```

**The port binding.** Published as `8088:8088`, the dashboard is on the public
internet with trading authority. Reach it through an SSH tunnel instead:

```bash
ssh -N -L 8088:127.0.0.1:8088 you@your-server
```

then open `http://127.0.0.1:8088` on your own machine. If several people need
access, use `deploy/nginx/sentinel.conf` — with the client-certificate block
left in.

**The volume.** It holds the audit chain, the verdict registry, the agent's
equity peak and period baselines, and the closed-trade memory. Delete it and
the drawdown ladder forgets it was ever in drawdown — a system that was
correctly trading at half size resumes at full size.

The compose file also runs a **watchdog** container. It watches the heartbeat
*file*, not an HTTP endpoint, because the API can answer perfectly while the
decision loop is wedged. If the loop goes quiet for 45 s it engages the kill
switch and **leaves it engaged** — a human decides whether it is safe to
resume.

Housekeeping:

```bash
docker compose logs -f engine
docker compose exec engine python -c \
  "from sentinel.core.audit import AuditLog; print(AuditLog('/data/var/audit.jsonl').verify())"
docker compose down            # state survives; the volume is not removed
```

---

## 3. systemd (bare metal / VPS, no containers)

```bash
sudo useradd --system --home /opt/sentinel-fx --shell /usr/sbin/nologin sentinel
sudo mkdir -p /opt/sentinel-fx /var/lib/sentinel /etc/sentinel /var/backups/sentinel

sudo tar -xzf sentinel-fx-1.0.0.tar.gz -C /opt --strip-components=0
cd /opt/sentinel-fx
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
(cd dashboard && sudo npm ci && sudo npm run build)

sudo cp .env.example /etc/sentinel/sentinel.env
sudo chmod 600 /etc/sentinel/sentinel.env
sudo $EDITOR /etc/sentinel/sentinel.env

sudo chown -R sentinel:sentinel /opt/sentinel-fx /var/lib/sentinel /var/backups/sentinel
sudo chmod 700 /var/lib/sentinel

sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-engine sentinel-watchdog sentinel-backup.timer

journalctl -u sentinel-engine -f     # otpauth:// URI appears here
```

### The unit setting that matters most

```ini
[Unit]
StartLimitIntervalSec=600
StartLimitBurst=5
```

Without it, a process that crashes on every startup — a bad credential, a
corrupt verdict registry, a broker rejecting everything — is restarted
forever, and **each restart is another chance to re-submit an order whose fate
is unknown**. Five failures in ten minutes stops the unit and leaves it
stopped. That is the behaviour you want at 3 a.m.

(These keys belong in `[Unit]`. systemd ignores them in `[Service]`, which is
a silent way to lose the protection.)

The watchdog deliberately uses `Restart=always` with **no** burst limit and
`Wants=` rather than `Requires=` on the engine: a restarting watchdog cannot
place an order, and a watchdog bound to the engine's state would stop at
exactly the moment it becomes useful.

---

## 4. First run — the order to do things in

1. Open the dashboard. It starts in **advisory mode**, **paper venue**,
   **read-only**.
2. Configure your instruments and risk on the Settings page. Note the defaults:
   0.50 % per trade, 2.00 % total open risk, 4 positions, 60 % break-even
   ceiling.
3. **Run the acceptance protocol.** It will fail on the demo strategy. That is
   correct — see `ACCEPTANCE-PROTOCOL.md`.
4. Let it paper-trade for weeks. Read the audit log.
5. Only then consider advisory-on-live → semi-auto → autonomous.

### Test the kill switch before you need it

```bash
# docker
docker compose exec engine touch /data/var/KILL
# systemd
sudo -u sentinel touch /var/lib/sentinel/var/KILL
```

New risk stops immediately — no API call, no authentication, no dependency on
the process being healthy. Release it from the dashboard (owner + TOTP) or by
deleting the file.

---

## 5. Backups

`sentinel-backup.timer` runs every six hours. It snapshots SQLite databases
through `.backup` (a plain `cp` of a live database yields a backup that
restores into corruption), copies the audit journal, agent state and config,
and **verifies the hash chain inside the snapshot** rather than trusting the
copy.

```bash
sudo /opt/sentinel-fx/deploy/backup.sh /var/lib/sentinel /var/backups/sentinel
```

Ship these off-host. The audit journal is tamper-*evident*, not
tamper-*proof*; continuous off-host shipping is what makes it resistant.

---

## 6. Going live — the checklist

- [ ] `L0.5` connectivity measured over **weeks**: ≥ 99 % uptime to the venue
- [ ] `L0.6` a real withdrawal has cleared
- [ ] `L0.4` the venue supports server-side stops, confirmed
- [ ] A passing acceptance verdict whose fingerprint matches the live config
- [ ] `SENTINEL_JWT_SECRET` set and stable
- [ ] `SENTINEL_ADMIN_PASSWORD` **removed** from the environment
- [ ] `var/users.db` included in the backup set and protected like a key
- [ ] TOTP enrolled and tested
- [ ] Kill switch tested under load
- [ ] Watchdog confirmed to trip (stop the engine; watch it engage)
- [ ] Backups running and restore tested
- [ ] Bound to loopback or behind mTLS
- [ ] Starting size is the minimum your account allows

If any box is unticked, stay in paper mode. Nothing is lost by waiting.

---

## 7. Troubleshooting

**`refusing to start: verdict registry is not a readable database`** — correct
behaviour, not a bug. Restore `verdicts.db` from a backup. Deleting it would
demote every accepted strategy irreversibly, so the system will not do that
for you.

**`live mode requires these environment variables: ...`** — credentials are
read from the environment only, never from a config file or the repository.

**`REFUSED: <strategy> claims accepted but no verdict matches`** — the config
was edited after the acceptance run. Re-run the protocol.

**The dashboard shows a login screen forever** — the page requires a JSON
response with a `mode` key from `/api/status`. A static host's SPA fallback
returning HTML with status 200 produces exactly this. Serve the dashboard from
the engine (`--dashboard dashboard/dist`) or fix the proxy.

**Sessions die on every restart** — `SENTINEL_JWT_SECRET` is unset, so a random
one is generated at startup. (Accounts themselves persist in `var/users.db`;
this only affects *sessions*.)

**`owner 'owner' already exists; environment credentials ignored`** — working
as intended. The account is in `var/users.db` and the environment cannot
overwrite it. Change the password with
`python scripts/manage_users.py passwd --username owner`.

**Nobody can log in / `WARNING: no accounts exist`** — create the first owner:
`python scripts/manage_users.py add --username owner --role owner`.

**Every session looks hijacked behind a proxy** — sessions are bound to a
client fingerprint that includes the source address. Forward the real one
(`proxy_set_header X-Forwarded-For $remote_addr`).

**`vetoed: missing_conversion`** — the account currency differs from the
instrument's quote currency and the rate is unknown. The engine refuses to
guess. Supply the conversion rate, or trade instruments quoted in the account
currency.

**`vetoed: cost_barrier`** — the break-even win rate for the proposed geometry
exceeds 60 %. Widen the target, tighten the cost, or abandon the family. Do
not raise the threshold.


---

## MetaTrader brokers from a Linux server: the bridge

The `MetaTrader5` Python package exists only for Windows, so an engine on
Ubuntu cannot import it. The supported arrangement is:

```
[ Ubuntu: engine ]  <-- SSH reverse tunnel --  [ Windows: MT5 terminal + scripts/mt5_bridge.py ]
      127.0.0.1:5555                                     127.0.0.1:5555
```

* On Windows, `deploy/mt5-bridge/start-bridge.ps1` installs the package into a
  private venv and runs `scripts/mt5_bridge.py`, which attaches to the
  signed-in terminal and serves it on loopback. It prints a token once.
* `deploy/mt5-bridge/tunnel.ps1 -Server <ip>` keeps `ssh -N -R
  127.0.0.1:5555:127.0.0.1:5555` alive, so the server sees the bridge on its
  own loopback. Nothing is exposed on the internet; SSH carries the traffic.
* On Ubuntu, `sudo deploy/scripts/connect-mt5.sh` writes
  `SENTINEL_MT5_BRIDGE` and `SENTINEL_MT5_BRIDGE_TOKEN` into
  `/etc/sentinel/sentinel.env`, verifies the account with a read-only probe,
  and restarts the engine. `build_broker`, discovery and the dashboard's
  connection test all use the bridge transparently from then on.

The wire protocol (`sentinel/brokers/mt5_bridge.py`) is JSON lines over TCP
with a shared token; only the methods the adapter uses are bridged. The
bridge refuses to bind a non-loopback address unless told `--allow-remote`.
A Persian walkthrough for a first-time operator is in `docs/UBUNTU-FA.md`.
