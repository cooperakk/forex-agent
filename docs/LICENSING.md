# Sentinel-FX — Licensing

> **Read the limits first.** A licensing document that opens with its features
> and buries its weaknesses is a sales document. This one opens with what the
> system cannot do, because deploying it under a false belief is worse than not
> deploying it.

## What this can and cannot guarantee

**Guaranteed by cryptography** — true regardless of who reads the source:

| Property | Why it holds |
|---|---|
| A licence cannot be **forged** | Ed25519. The private key exists only on the vendor's machine; the software embeds the public key. |
| A licence cannot be **edited** | One changed byte — expiry, tier, machine — invalidates the signature. |
| A licence cannot be **moved** | It carries hashed identifiers of the machine it was issued for. |
| A licence **expires** | The window is inside the signed payload. |
| Code modification is **detectable** | A signed manifest of SHA-256 hashes over the protected modules. |

**Not guaranteed, and no licensing system can guarantee it:**

> **This cannot stop the machine's owner.** The software runs as Python source
> on a server the customer controls. Someone with root there can delete the
> check. That is equally true of obfuscated Python, of a compiled extension, and
> of a packed binary — those raise the cost, they do not change the outcome.

The integrity manifest is checked by code that can itself be edited. It turns
"comment out one line" into "patch several modules consistently and regenerate
a signature you do not have the key for", which in practice means deleting the
check — leaving evidence any later comparison with a release will find.

**So the honest model is:** this stops casual copying, accidental
over-deployment, silent expiry, and tampering going unnoticed. It does not stop
a determined reverse engineer with root.

**If you need a guarantee rather than a deterrent**, there is exactly one
structural answer: keep the valuable computation on a server you control and
have the customer's agent call it. `activation_url` is the hook for that.
Everything else is a lock on a door the customer already owns.

---

## Tiers

Tiers gate **capability**, never safety. Every tier gets the same risk engine,
the same audit chain and the same acceptance protocol — shipping a "cheaper"
version with weaker safety would be indefensible.

| Tier | Live | Instruments | Accounts | Equity ceiling | Research lab |
|---|---|---|---|---|---|
| `evaluation` | no | 3 | 1 | 25,000 | no |
| `research` | no | unlimited | 1 | unlimited | yes |
| `live_single` | **yes** | 8 | 1 | unlimited | yes |
| `live_multi` | **yes** | unlimited | 5 | unlimited | yes |
| `unlimited` | **yes** | unlimited | unlimited | unlimited | yes |

Per-licence overrides exist, so a customer can be granted an exception without
inventing a tier:

```bash
--capability max_instruments=12 --capability llm_news=true
```

---

## Vendor workflow

### 1. Generate the keypair — once, offline

```bash
./scripts/licensegen.py keygen --out ./vendor-keys
```

Produces `private.pem` (0600) and `public.txt`.

> The private key must never reach a customer machine, a repository, a CI
> secret store customers can read, or a backup outside your control. Lose it
> and you can never issue or renew a licence again. Leak it and every licence
> ever issued becomes forgeable; the only remedy is a new key and a re-issue of
> the entire field.

Embed the public key in the build you ship:

```bash
SENTINEL_LICENSE_PUBKEY=<contents of public.txt>
```

### 2. The customer sends their machine fingerprint

On **their** server:

```bash
python scripts/licensegen.py fingerprint --out fingerprint.json
```

The file is safe to email: every identifier is hashed, so it discloses neither
their hostname, nor their MAC address, nor their disk ids.

### 3. Issue

```bash
./scripts/licensegen.py issue \
    --key ./vendor-keys/private.pem \
    --to "Acme Capital" \
    --tier live_single \
    --days 365 \
    --machine-file fingerprint.json \
    --out acme.key
```

Send `acme.key`. The customer puts it at `/var/lib/sentinel/licence.key`.

### 4. Sign the release manifest

```bash
./scripts/licensegen.py manifest --key ./vendor-keys/private.pem --version 1.0.0
```

Ship `MANIFEST.sig` with the release. Installations verify themselves against
it, and **live trading is refused** when a protected module does not match.

---

## The fingerprint, and why it is deliberately forgiving

Five signals are read: the OS machine id, the first non-virtual MAC, a CPU
signature, the root filesystem UUID, and the hostname. A licence matches when
**three of them** agree.

That threshold is a deliberate trade. A fingerprint that breaks when a network
card is replaced or a container is rebuilt turns a paying customer into a
support ticket at 3am — and the natural fix, "just ignore mismatches", removes
the control entirely. Three of five tolerates hardware maintenance and still
refuses a copy to a different machine.

Docker, veth, bridge and VPN interfaces are skipped: they are regenerated on
every container start, and including one would make the fingerprint unstable
for exactly the deployment this software recommends.

```bash
python scripts/licensegen.py fingerprint --verbose
```

shows the raw values, so an operator whose licence stopped matching can see
*which* component changed rather than only that it did.

---

## What happens when a licence is missing, expired, or wrong

**The governing rule: a licence problem must never make the system dangerous.**

| Situation | Effect |
|---|---|
| No licence file | New entries stop. Dashboard works. **Open positions keep being managed.** |
| Expired, inside grace (14 days) | Everything works. A warning appears everywhere. |
| Expired, past grace | New entries stop. **Open positions keep being managed.** |
| Wrong machine | New entries stop, with an explanation of which identifiers differ. |
| Tier excludes live | Live refused. Paper and research unaffected. |
| Integrity check fails | **Live refused.** Paper unaffected. |
| Over an instrument limit | Warning. Nothing is silently truncated. |

Note what is absent from that table: nothing closes a position, widens a stop,
or stops managing an open book. Refusing to protect an open trade because an
invoice is unpaid is indefensible, and a vendor who does it will one day be
explaining a loss they caused.

The 14-day grace period is not generosity. It exists so a renewal that arrives
late does not stop a live book at 3am.

---

## Self-hosted builds

With no `SENTINEL_LICENSE_PUBKEY` configured, licensing is **inert**: the gate
reports `unlicensed_mode` and permits everything. That is correct when the
customer is the vendor, and it says so on the dashboard rather than pretending
to enforce something.

---

## Operator reference

```bash
# Check my licence
python scripts/licensegen.py inspect --licence /var/lib/sentinel/licence.key \
    --pubkey public.txt --check-machine

# Is my install unmodified?
python scripts/licensegen.py check-manifest --pubkey public.txt

# What does the running system think?
curl -s -H "Authorization: Bearer $TOKEN" localhost:8088/api/licence | jq
```

## Renewal

Re-issue with a new `--days` against the same fingerprint and replace the file.
No reinstall, no restart needed at the next licence check. The dashboard warns
from 21 days out, so a renewal is never a surprise.


---

## Terms, renewal, and the anti-rollback guard

### Calendar months, not day counts

A term is a number of **calendar months**, defaulting to three. This is not
pedantry:

* four 90-day quarters are 360 days, so a customer on quarterly renewals gains
  five free days a year and their renewal date walks backwards through the
  calendar until it no longer matches the invoice;
* 31 January plus three months is 30 April, and a naive day-of-month copy
  raises `ValueError` inside a signing routine.

Month arithmetic clamps down, never up: 31 January + 1 month is 28 (or 29)
February, never 1 March. A licence must never silently gain a day it was not
sold.

### The billing anchor

Clamping is lossy, and chaining quarters ratchets downwards for ever:

```
31 Mar -> 30 Jun -> 30 Sep -> 30 Dec -> 30 Mar     one day lost, every year
```

So each licence carries `anchor_day`, the day of the month the **subscription**
began on, and each term re-reaches for it when the month is long enough:

```
31 Mar -> 30 Jun -> 30 Sep -> 31 Dec -> 31 Mar     stable
```

Four quarterly renewals from 31 March land exactly on 31 March. There is a test
for this (`TestCalendarTerms`), because it is the kind of arithmetic that is
wrong for years before anyone notices.

### Renewal

```bash
./scripts/licensegen.py renew \
    --key ./vendor-keys/private.pem \
    --pubkey ./vendor-keys/public.txt \
    --licence acme-q1.key --out acme-q2.key
```

Two rules, both of which exist because the naive version costs somebody money:

* **Renewing early does not throw away the remaining days.** The new term starts
  where the old one ended. Without this, a customer who renews a week ahead of
  expiry — exactly the behaviour a vendor wants — pays for that week twice.
* **Renewing long after expiry does not back-date the term.** Past
  `--late-grace` days (default 14), the new term starts today instead.
  Otherwise a customer returning after six months receives a licence that
  expired three months ago.

The old document's signature is verified before it is extended, with the machine
check switched off (renewal happens on the vendor's machine). Skipping the
verification would let a forged document be laundered into a genuine one by
renewing it.

`subscription_id` is stable across renewals and `term_index` counts them, so
"this customer is on their fourth quarter" is answerable from any single file.

### The renewal ladder

| Days remaining | Stage | What the operator is told |
|---|---|---|
| > 30 | `healthy` | the licence is active |
| 30 – 15 | `approaching` | "a good time to renew — no rush" |
| 14 – 4 | `due` | "if it is not renewed, no new trades open" |
| ≤ 3 | `critical` | "urgent" |
| past expiry | `expired` | in the grace period; new entries stop when it ends |

The escalation is in the **wording**, not only in the number: "29 days" and
"2 days" look equally unalarming in a table.

### The anti-rollback guard

Every expiry check anywhere reduces to comparing a date against the machine's
clock, and the machine's clock belongs to the customer. So the cheapest bypass
of a three-month licence is `date -s`, and it takes one line.

`sentinel/licensing/clock_guard.py` keeps an authenticated state file recording
the **highest wall-clock time ever seen** on this installation and the set of
**licence ids already watched past expiry**. Two rules follow:

1. If the clock is materially behind the high-water mark (tolerance: 6 hours,
   which is far larger than any NTP step, resumed VM or confused RTC), the
   licence is treated as unverifiable until the clock is corrected.
2. A licence this installation has already watched expire stays expired for
   ever, whatever the clock says afterwards.

The state is authenticated with HMAC-SHA256 under a key derived from **the
machine map inside the licence** and the vendor public key. Deriving it from the
*live* fingerprint was a complete bypass: a licence tolerates 3-of-5 fingerprint
components matching, so one `hostnamectl set-hostname` rotated the key, the
state failed to authenticate, the history was discarded, and a permanently
expired licence came back valid with the clock wound back.

**What it does not do.** The derivation inputs are all present on the customer's
machine, so someone with root and this source can recompute the HMAC and write
whatever state they like, or delete the file. This raises the bypass from
"change the clock" to "read the source, derive the key, forge the state" —
a real increase and not a guarantee. A missing, unreadable or unwritable state
file is reported in the dashboard and written to the audit journal, because a
healthy installation that has run for months and has no anti-rollback state has
had it removed; but "reported" means a warning, not a refusal, since a genuine
restore-from-backup looks identical.

### A licence problem never makes the system dangerous

The gate is re-evaluated every 15 minutes while the process runs — it used to be
computed once at boot, which meant a three-month licence on a server that stayed
up authorised live entries months past its expiry and the dashboard's countdown
froze at whatever it said on the day the service started.

When a licence is missing, expired or bound elsewhere: new entries stop, the
dashboard keeps working read-only and says exactly what is wrong, and **existing
positions keep being managed** — stops, trails, the give-back ratchet, the
weekend flatten. Refusing to protect an open trade because an invoice is unpaid
would be indefensible.

---

## 1.5.0 hardening

Four gaps let a licensee trade live without a valid licence in 1.4.0. Each is
closed, and each has a regression test in `tests/test_licensing_150.py`.

### 1. The vendor key is embedded, and the environment can no longer override it

Before 1.5.0 the only source of the vendor key was `SENTINEL_LICENSE_PUBKEY`.
Unsetting it switched licensing off ("unlicensed mode"); pointing it at a key
the licensee generated made a self-signed "unlimited" licence verify.

`sentinel/licensing/vendor_key.py` now carries the key(s), written at release
time:

```bash
./scripts/licensegen.py embed-key --pubkey ./vendor-keys/public.txt \
    [--lease-pubkey ./lease-keys/public.txt]
./scripts/licensegen.py manifest --key ./vendor-keys/private.pem --version 1.5.0
```

Resolution order: an explicit argument, then the **embedded** key (the
environment is then ignored), then the environment -- the last only for
self-hosted builds with no embedded key.

### 2. A distributed build must carry its manifest

With an embedded key, a missing `MANIFEST.sig` is an integrity **failure** for
live trading, not "unsigned, fine". Deleting the manifest used to switch the
integrity check off. The manifest now also covers `vendor_key.py`,
`activation.py`, `clock_guard.py`, `bootstrap.py` and `agent/orchestrator.py`,
the files that wire the gate in.

### 3. The licence is asked every cycle, not once at boot

A licence that expired while the service stayed up kept authorising live
entries until the next restart. `Agent.entry_permission()` now asks the gate on
every decision cycle and on every human entry path (accepting a proposal, the
manual trade ticket). Only **new** live risk is refused; exits, stops and the
management of open positions are never gated by a licence.

### 4. Online activation is implemented

`activation_url` was carried in every licence and implemented nowhere. It is
now the one control a licensee with root cannot delete, because its answer
comes from the vendor's server (`sentinel/licensing/activation.py`):

* the client POSTs the licence, a digest of the machine fingerprint and a
  random nonce; the server answers with an Ed25519-signed **lease**;
* the client accepts it only if the signature verifies against the lease key,
  the nonce is the one it sent (no replay), and the licence id and device are
  its own; the lease is cached (0600) and bounded by its own expiry;
* **server unreachable** -> the cached lease keeps working until it expires
  (default 72 h), so a vendor outage never stops a live book at 3 am;
* **revoked / seats exhausted** -> no new live entries, reason shown verbatim;
* the server's clock is a time reference the licensee does not control: a
  local clock rolled back behind the last lease is refused.

Issue a licence that requires activation:

```bash
./scripts/licensegen.py issue --key ./vendor-keys/private.pem --to "Acme" \
    --tier live_single --months 3 --machine-file acme-fp.json \
    --activation-url https://licence.example.com/v1/lease --activation-interval 24
```

Run the vendor's activation server with a **separate lease key** (the licence
key stays offline; a compromised activation server can mint leases for
existing licences, never licences):

```bash
./scripts/licensegen.py keygen --out ./lease-keys
python scripts/license_server.py serve --db var/licence-server.db \
    --lease-key ./lease-keys/private.pem --vendor-pubkey ./vendor-keys/public.txt \
    --host 127.0.0.1 --port 8443          # behind a TLS reverse proxy
python scripts/license_server.py register --db ... --licence acme.key --seats 1
python scripts/license_server.py revoke   --db ... --licence-id XXXX-... --reason "..."
python scripts/license_server.py reset-devices --db ... --licence-id XXXX-...
```

### Protected (compiled) builds

```bash
pip install "cython>=3.0" setuptools        # plus a C compiler
python scripts/build_protected.py --version 1.5.0 \
    --key ./vendor-keys/private.pem --pubkey ./vendor-keys/public.txt \
    --lease-pubkey ./lease-keys/public.txt --compile --out ./release
```

This copies the tree without vendor-only tools, embeds the keys, compiles the
licensing, risk, audit, verdict and security modules to native extension
modules **and deletes their Python source**, signs the manifest over the
shipped binaries, and packs a tarball. Build on the customer's platform
(OS, architecture, Python minor version). The full test suite passes against a
compiled build, except the two tests that assert *self-hosted* behaviour
(no key -> inert; no manifest -> allowed), which a distributed build correctly
refuses.

### The honest limit, restated

Ed25519 makes licences unforgeable and uneditable. Compilation and the signed
manifest raise tampering from "edit one line" to "reverse-engineer and patch
machine code, then defeat the manifest". Neither is absolute against someone
with root on their own machine. The online lease is the control that is not in
their hands; for the strongest guarantee, keep the most valuable computation on
a server you operate.
