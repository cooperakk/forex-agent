# Sentinel-FX — Security model

> The threat here is not abstract. This software holds credentials that can
> move money, and it is reachable over a network. The realistic adversaries,
> in order of likelihood, are: **you, at 2 a.m., with a fat finger**; a stolen
> laptop or session cookie; a malicious dependency; and only then a targeted
> attacker.

## 1. Trust boundaries

```
┌─── untrusted ────────────────────────────────────────────────┐
│  the internet, the browser, any reverse proxy                │
└───────────────────────┬──────────────────────────────────────┘
                        │  TLS + (recommended) mTLS
┌───────────────────────▼──────────────────────────────────────┐
│  API layer — authenticates, authorises, rate-limits, audits  │
└───────────────────────┬──────────────────────────────────────┘
                        │  in-process, single lock
┌───────────────────────▼──────────────────────────────────────┐
│  Runtime / Agent — holds the only writable handle to state   │
└───────────────────────┬──────────────────────────────────────┘
                        │  credentials from ENVIRONMENT only
┌───────────────────────▼──────────────────────────────────────┐
│  Broker — the authority on positions and money               │
└──────────────────────────────────────────────────────────────┘
```

Two rules follow and are enforced in code:

1. **Credentials never touch the repository, the config JSON, or the API
   surface.** `bootstrap.py` reads them from the environment and refuses to
   start in live mode if they are missing. There is no endpoint that returns
   a credential, and no config field that holds one.
2. **The broker is the authority.** Local state is a cache that is reconciled
   against the venue every 30 seconds and on startup.

## 2. Authentication

**Passwords** — Argon2id via `argon2-cffi`, with per-user salts. Nothing in
the system stores or logs a plaintext password.

**The timing oracle, and how it is closed.** A naive login handler skips the
hash computation when the username does not exist, and the response-time
difference tells an attacker which usernames are real. Measured at **1.97×**
before the fix. `security.py` holds a module-level `_DUMMY_HASH` and verifies
against it for unknown users, bringing the ratio to **1.05×**.

**Lockout** — after `max_login_attempts` (default 5) the account is locked for
`lockout_minutes` (default 15). The counter is keyed **independently** on
`user:<name>` and `ip:<addr>`, not on the pair. Keying on the pair lets an
attacker with a botnet get 5 attempts *per source address* against one
account, which is not a lockout.

**Sessions** — JWT, HS256, signed with `SENTINEL_JWT_SECRET`, TTL 30 minutes
by default. Each token is bound to a client fingerprint (user agent + source
address); a token replayed from elsewhere is rejected. If you deploy behind a
proxy, forward the real client address or every session will look hijacked.

**TOTP** — RFC 6238, 30-second window, ±1 step tolerance, with replay
protection (a code that has been used cannot be reused inside its window).
Enrolled via an `otpauth://` URI printed **once**, when the account is created.

**Where accounts live.** `<state_dir>/users.db`, SQLite, created `0600`. It
holds the Argon2id password hash, the role, and the TOTP secret. The TOTP
secret cannot be hashed — the server must recompute the code — so that file
belongs in the same protect-and-back-up category as a private key.

> An earlier build held accounts in memory only. Every restart regenerated the
> owner's TOTP secret and printed a fresh `otpauth://` URI to the log, so the
> second factor protecting every write changed without anyone deciding it
> should — and the recovery ritual, *re-enrol from whatever the log says*, is
> indistinguishable from an attack. Persistence is a security property here,
> not a convenience.

`SENTINEL_ADMIN_USER` / `SENTINEL_ADMIN_PASSWORD` are read **only when no such
account exists**. An existing account is never overwritten from the
environment; otherwise anyone who could set an environment variable would
replace the owner's password and second factor on the next restart.

**Account management is a command on the server, not an API endpoint** —
`scripts/manage_users.py` — because account management is the highest-value
target in the whole surface and nothing about it needs to be reachable from a
browser. Passwords are read from a prompt, never from an argument, so they do
not land in shell history or a process listing.

```bash
python scripts/manage_users.py add     --username owner --role owner
python scripts/manage_users.py list
python scripts/manage_users.py passwd  --username owner
python scripts/manage_users.py role    --username alice --role viewer
python scripts/manage_users.py disable --username bob
```

A password change deliberately does **not** rotate the TOTP secret: a password
reset is routine, re-enrolling an authenticator is not, and conflating them
trains the operator to accept a new second factor whenever they are told to.

**Sessions are re-checked against the account on every request.** The role in
the token is not trusted: disabling an account drops its live sessions
immediately, and a demotion from owner to viewer takes effect on the next
request rather than at session expiry — which is exactly the window in which
you demoted them.

## 3. Authorisation

Three roles, and the separation is real rather than cosmetic:

| Role | Can |
|---|---|
| `viewer` | Read everything. Change nothing. **This is what a dashboard is for.** |
| `operator` | Everything a viewer can, plus: halt, resume, flatten, close a position, accept/reject advice. Operators can make the system *safer*, and can act on advice — they cannot change what it is allowed to do. |
| `owner` | Everything, plus the five authority endpoints below. |

Owner-only:

```
POST /api/control/mode          switch advisory ↔ semi-auto ↔ autonomous
POST /api/control/kill          engage the kill switch
POST /api/control/kill/release  release it
POST /api/config                change configuration
POST /api/proposals/review      approve a learned parameter change
```

**Every** mutating endpoint additionally requires a fresh TOTP in the `X-TOTP`
header — not once per session, but per write. A stolen session token alone
cannot move money.

Write rate limit: 10/min (default). Read: 120/min.

## 4. Privileged fields

Three configuration fields cannot be changed through `POST /api/config` at
all, regardless of role, because a config patch is the wrong mechanism for
them:

- `agent.mode` — has its own endpoint, its own audit event, its own checks
- `execution.venue_mode` — paper → live is a promotion, not an edit
- `execution.broker` — changing the venue under a running book is not an edit

Promotion to live additionally requires a **verdict whose fingerprint matches
the current configuration**. `_guard_privileged_fields()` enforces this in
`api/state.py`.

### Why the verdict registry is a security control

The most dangerous privilege escalation in a trading system is not root. It is
**a strategy acquiring the right to trade real money without having earned
it**. Before the registry existed, one authenticated config write could set
`lifecycle: accepted` and the system would trade it.

Now: acceptance is issued only by `scripts/run_acceptance.py`, stored in
`var/verdicts.db`, and bound to `config_fingerprint(instruments, params,
timeframe)`. At startup, `enforce_config_authority()` re-checks every badge
and **repairs unbacked ones downward**. Change a parameter on an accepted
strategy and its fingerprint no longer matches — acceptance is revoked
automatically.

An unreadable registry raises `RegistryUnreadable` and the process refuses to
start, because treating "unreadable" as "empty" would irreversibly erase the
acceptance history.

## 5. The audit journal

Append-only JSONL, SHA-256 hash-chained, `0o600`. Every record carries the
previous record's hash, so any edit, deletion or reordering breaks the chain
at a detectable point.

```bash
make verify-audit        # → OK, or "BROKEN at seq N: <reason>"
```

What is recorded: authentication (success and failure), every write action and
every denied write, every configuration change, every order intent / send /
ack / fill / reject / unknown / duplicate-blocked, every risk veto, every
ladder step and halt, every kill-switch event, every reconciliation mismatch,
every research verdict.

The chain is tamper-**evident**, not tamper-proof. An attacker with write
access to the file can rewrite it from any point and recompute the chain. For
tamper-resistance, ship the journal off-host continuously; `deploy/backup.sh`
snapshots it every six hours and verifies the chain inside each snapshot.

## 6. Network exposure

The default bind is `127.0.0.1`. The process **refuses to bind beyond loopback**
unless `SENTINEL_ALLOW_PUBLIC_BIND=1` is set explicitly.

The recommended access path is an SSH tunnel:

```bash
ssh -N -L 8088:127.0.0.1:8088 you@your-server
```

If you must expose it, `deploy/nginx/sentinel.conf` shows the shape: TLS 1.3,
**client-certificate authentication**, a separate rate limit on the login
endpoint, HSTS. The application's password + TOTP is the second layer there,
not the first.

Response headers set by the app: a strict CSP (`default-src 'self'`, no inline
script, no remote origins), `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. The dashboard ships
with all assets — including the Doran webfont — served from origin, so the CSP
needs no exceptions.

CORS defaults to `http://127.0.0.1:5173` (the dev server) and nothing else.

## 7. Container and host hardening

The compose file and the systemd units are part of the security model, not
packaging convenience:

- port published to `127.0.0.1` only — **do not "fix" this to `8088:8088`**
- `read_only: true` root filesystem; the only writable path is the state volume
- `cap_drop: ALL`, `no-new-privileges`, non-root uid 10001
- systemd: `ProtectSystem=strict`, `ProtectHome`, `PrivateDevices`,
  `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`, empty
  `CapabilityBoundingSet`, `UMask=0077`
- `StartLimitBurst=5` inside `StartLimitIntervalSec=600` — a process that
  crashes on every start must **stop**, because each restart is another chance
  to re-submit an order whose fate is unknown

## 8. Supply chain

`requirements.txt` pins exact versions. Rebuild the lock deliberately, read
the diff, and re-run the suite. The dashboard has **no runtime charting
dependency** — the SVG chart kit is written by hand — which removes the single
largest category of transitive front-end packages.

## 9. Operational security

- Delete `SENTINEL_ADMIN_PASSWORD` from the environment once the owner exists.
- `SENTINEL_JWT_SECRET` must be set and stable, or every session dies on restart.
- Back up `var/` (audit chain, verdicts, agent state, memory). Everything else
  can be re-fetched; these cannot.
- Test the kill switch before you need it: `touch var/KILL` stops new risk
  immediately, with no API call and no authentication.
- Start in `paper` mode. Run for weeks. Read the audit log. Then decide.

## 10. What this model does not protect against

Stated plainly, because a security document that only lists strengths is a
marketing document:

- **A compromised host.** Root on the box reads the environment, the state
  directory and the process memory. The broker credentials are then gone.
  Mitigate at the venue: IP allowlists, withdrawal-disabled API keys,
  per-key permission scoping.
- **A compromised browser.** A malicious extension can read the dashboard and
  a live session. The TOTP-per-write requirement limits it to the window in
  which the user types a code.
- **A malicious dependency.** Pinning limits drift; it does not detect a
  backdoor introduced in a pinned version.
- **The broker itself.** Counterparty risk is real. Regulation, segregation of
  client funds and a tested withdrawal path are the controls, and `L0.6` of
  the acceptance protocol requires the withdrawal path to have been exercised
  with real money before scale-up.
- **You, deliberately.** Every guard has an owner-authenticated release. The
  system is built to stop an accident, not to stop its owner.


---

## Accounts: three levels

| Internal name | Dashboard | May |
|---|---|---|
| `owner` | مدیر | everything: risk limits, the mode dial, account management, venue switching, releasing the kill switch |
| `operator` | کاربر | close a position, flatten, halt, accept or reject advice — but not change a setting |
| `viewer` | نظاره‌گر | read |

The internal names never change: they are written into the audit chain and into
the SQLite store, and renaming them would orphan every historical record.

**Every write still needs a fresh TOTP code, at every level.** A session grants
reading only. There is no "admin mode" that stays on.

### Management endpoints

All owner-only, **including the listing**. Enumerating who can log in is
reconnaissance: it tells an attacker which names to spray and which of them can
move money.

`GET /api/users`, `POST /api/users/{create,role,disable,password,totp,delete}`.

No response from any of them contains a password hash or a TOTP secret. An
endpoint that returns the hash "only to the owner" is one authorisation bug away
from an offline cracking corpus, and the TOTP secret *is* the second factor —
returning it would make the second factor a function of the first.

The enrolment URI from `create` and `totp` is shown **once** and is not
retrievable afterwards. If it is lost the answer is to rotate the factor, not to
look it up.

### The last-administrator guard

The last enabled owner cannot be demoted, disabled or deleted. Without this the
system can be bricked in one click: after that nobody can change a risk limit,
release the kill switch, take the agent off autonomous, or create another owner,
and the only recovery is a shell on the server.

Two implementation details that were defects first:

* The guard holds a **dedicated admin lock across check-then-act**. Checking and
  mutating separately let two concurrent demotions each see the other owner as
  still enabled — and one owner can produce that alone, because TOTP's ±1 window
  accepts three distinct codes at any instant.
* It counts owners **from the store, not from the in-process cache**. The cache
  is loaded at construction and refreshed per-username on authentication, while
  `scripts/manage_users.py` writes the same database from another process, so a
  second owner deleted by the CLI still counted.

`add_user` likewise checks the store: the cache-only check let an account
created out of band be silently overwritten — role, Argon2 hash and TOTP secret
all replaced — with the audit chain recording it as `user_created`.

### Password policy

Minimum 12 characters, plus a blocklist of the passwords that top every breach
corpus, matched against **normalised forms** (lower-cased, punctuation stripped,
a trailing digit run removed) so that "Password123!" is judged as "password".

The blocklist is checked **before** the length test. Running it after meant that
every entry shorter than the minimum was unreachable — which was all of them,
and the "floor" rejected nothing for the life of the feature.

Rotating a password deliberately does **not** rotate the TOTP secret. A password
reset is routine; re-enrolling an authenticator is not, and conflating them
teaches an operator to accept a new second factor whenever they are told to —
which is precisely the habit an attacker wants.

Rotating a TOTP secret drops every live session for that user: whoever performs
the rotation can enrol their own authenticator, so they must not also inherit
the sessions it was protecting.
