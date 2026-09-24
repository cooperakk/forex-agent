"""Authentication and authorisation.

Threat model, stated plainly: a dashboard that can place trades is worth
exactly as much as the account it controls. If it is reachable from the public
internet and an attacker gets in, the account is gone. Every decision here
follows from that.

* **Read-only by default.** A session is read-only unless the user completes a
  second factor for a specific write. There is no "admin mode" that stays on.
* **Two factors for every write.** Password gets you a session; a TOTP code
  authorises one write. A stolen session token cannot place an order.
* **Argon2id for passwords.** Memory-hard, so an offline attack on a leaked
  hash is expensive.
* **Short sessions, bound to a fingerprint.** A token carries a hash of the
  user agent and client IP; replaying it elsewhere fails.
* **Lockout with exponential backoff**, and every attempt in the audit chain.
* **Loopback bind by default.** Exposing the port is an explicit act that
  produces a permanent warning banner in the UI.
* **Replay protection on TOTP.** A code that has been used cannot be used
  again inside its window.
* **Accounts persist.** Users, roles and TOTP secrets live in a 0600 SQLite
  file in the state directory. Holding them only in memory would mean every
  restart silently rotated the second factor and re-enrolled the operator from
  a log file -- which is precisely the habit an attacker wants them to have.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Set, Tuple

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from jose import JWTError, jwt

from ..core.audit import AuditLog, EventType
from ..core.clock import wall_ns

ALGORITHM = "HS256"
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

# Precomputed once at import. The unknown-user branch verifies against THIS
# rather than hashing a throwaway password on every attempt: hashing plus
# verifying costs about twice a verify, and the resulting ~80 ms gap is a
# remotely observable username-enumeration oracle.
_DUMMY_HASH = _hasher.hash("sentinel-fx-nonexistent-user-placeholder")

# Bounds on the tables keyed by caller-supplied strings. Without them an
# attacker rotating forged addresses grows them without limit.
_MAX_TRACKED_KEYS = 20_000
# A TOTP code is valid for 30s with a +/-1 step tolerance, so 120s of replay
# memory covers every window in which a code could still be accepted.
_TOTP_REPLAY_TTL_SEC = 120.0


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, account: str, issuer: str = "Sentinel-FX") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


#: The three levels, internal name -> what the dashboard calls them. The
#: internal names never change: they are written into the audit chain and into
#: the SQLite store, and renaming them would orphan every historical record.
ROLES: Dict[str, str] = {
    "owner": "مدیر",
    "operator": "کاربر",
    "viewer": "نظاره‌گر",
}

#: One sentence per level, shown next to the choice so that whoever is creating
#: an account does not have to guess what they are granting.
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "owner": ("همه‌کاره. می‌تواند سقف‌های ایمنی را عوض کند، حالت ربات را روی "
              "«کاملاً خودکار» بگذارد، کاربر بسازد و توقف اضطراری را آزاد کند. "
              "این سطح را فقط به خودتان بدهید."),
    "operator": ("می‌تواند معامله‌ای را ببندد، همه را ببندد، ربات را متوقف کند و "
                 "پیشنهادها را بپذیرد یا رد کند. نمی‌تواند سقف‌های ایمنی یا "
                 "تنظیمات را عوض کند و نمی‌تواند کاربر بسازد."),
    "viewer": ("فقط می‌بیند. هیچ دکمه‌ای برایش کار نمی‌کند. برای کسی که باید "
               "گزارش‌ها را ببیند ولی نباید چیزی را تکان بدهد."),
}

#: Minimum password length. Twelve, not eight: this password guards a session
#: that can move an account, the hash is Argon2id so length is the only lever
#: the user controls, and eight characters of anything a human invents is
#: inside reach of an offline attack on a leaked store.
MIN_PASSWORD_LENGTH = 12

#: Passwords that appear at the top of every breach corpus. Not a substitute
#: for a real check -- it is a floor, and it catches the specific case of an
#: operator setting up a demo account "just for now" and never changing it.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "123456", "12345678", "123456789",
    "1234567890", "qwerty", "qwerty123", "abc123", "letmein", "welcome",
    "admin", "admin123", "root", "toor", "changeme", "trustno1", "iloveyou",
    "monkey", "dragon", "sunshine", "princess", "football", "baseball",
    "master", "shadow", "superman", "batman", "passw0rd", "p@ssword",
    "p@ssw0rd", "sentinel", "sentinelfx", "trading", "forex", "money",
    # 12+ characters, because the length check runs first and everything above
    # is shorter than the minimum -- so for a long time NOT ONE entry in this
    # set could ever reject anything. These are the ones that actually top the
    # breach corpora at this length.
    "password1234", "passwordpassword", "qwerty123456", "123456789012",
    "1234567890123", "qwertyuiop123", "iloveyou1234", "letmein12345",
    "administrator", "sentinel-fx1", "welcome123456", "adminadmin12",
})


def _password_shapes(password: str) -> Set[str]:
    """Forms of a password that should all be judged the same.

    "Password123!" and "password123" are the same guess. Comparing only the
    literal made the blocklist trivially defeated by a capital letter.
    """
    lowered = password.strip().lower()
    stripped = "".join(ch for ch in lowered if ch.isalnum())
    letters_only = "".join(ch for ch in lowered if ch.isalpha())
    # Collapse a trailing run of digits: "password2026" -> "password".
    trimmed = lowered.rstrip("0123456789!@#$%^&*._-")
    return {lowered, stripped, letters_only, trimmed}


def check_password_policy(password: str, *, username: str = "") -> Optional[str]:
    """None if acceptable, otherwise the reason, in Persian, for the operator.

    Deliberately short. A policy that demands a symbol, a digit and a capital
    produces "Password1!" on every account in the building; length and a
    blocklist catch more and annoy less.
    """
    if len(password) > 256:
        return "گذرواژه بیش از اندازه بلند است (بیشینه ۲۵۶ نویسه)."
    # The blocklist runs BEFORE the length check. Run after it, every entry
    # shorter than the minimum was unreachable -- which was all of them.
    if _password_shapes(password) & _COMMON_PASSWORDS:
        return ("این گذرواژه (یا شکل سادهٔ آن) در فهرست رایج‌ترین گذرواژه‌های "
                "لو رفته است. اولین چیزی است که امتحان می‌شود — اضافه کردن یک "
                "عدد یا حرف بزرگ به آخرش کمکی نمی‌کند.")
    if len(password) < MIN_PASSWORD_LENGTH:
        return (f"گذرواژه باید دست‌کم {MIN_PASSWORD_LENGTH} نویسه باشد. "
                "یک عبارت چندکلمه‌ای که فقط خودتان می‌دانید از یک کلمهٔ "
                "پیچیدهٔ کوتاه امن‌تر است.")
    lowered = password.strip().lower()
    if username and username.lower() in lowered:
        return "گذرواژه نباید نام کاربری را در خود داشته باشد."
    if len(set(password)) < 5:
        return "گذرواژه تقریباً از یک نویسه تشکیل شده است."
    return None


def _check_username(username: str) -> Optional[str]:
    """None if usable, otherwise why not.

    A username ends up in the audit chain, in a JWT subject claim and in a
    TOTP provisioning URI. Restricting it to an unsurprising character set
    means none of those three has to think about escaping.
    """
    if not username:
        return "نام کاربری نمی‌تواند خالی باشد."
    if len(username) > 64:
        return "نام کاربری نباید بیشتر از ۶۴ نویسه باشد."
    if len(username) < 3:
        return "نام کاربری باید دست‌کم ۳ نویسه باشد."
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789._-")
    if not set(username) <= allowed:
        return ("نام کاربری فقط می‌تواند حرف انگلیسی، رقم، نقطه، خط تیره و "
                "زیرخط داشته باشد.")
    return None


@dataclass
class User:
    username: str
    password_hash: str
    totp_secret: str
    role: str = "operator"          # viewer | operator | owner
    created_ns: int = field(default_factory=wall_ns)
    disabled: bool = False

    @property
    def can_write(self) -> bool:
        return self.role in ("operator", "owner") and not self.disabled

    @property
    def can_change_risk(self) -> bool:
        """Only the owner may move a risk limit. Deliberately the narrowest gate."""
        return self.role == "owner" and not self.disabled



class UserStore:
    """Durable account storage.

    Holds the password hash (Argon2id), the role, and the TOTP secret. The TOTP
    secret is a shared secret by nature: it cannot be hashed, because the server
    must recompute the code. So the file is created 0600 and belongs in the same
    backup-and-protect category as a private key.

    Keeping accounts only in memory looked simpler and was wrong. On every
    restart the owner's TOTP secret was regenerated and a fresh ``otpauth://``
    URI printed to the log, so the second factor protecting every write changed
    without anyone deciding it should, and the recovery ritual -- re-enrol from
    whatever the log says -- is indistinguishable from an attack.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Create the file OURSELVES, 0600, before sqlite touches it. Letting
        # sqlite create it and chmod'ing afterwards leaves a window at 0644,
        # and a file descriptor opened during that window keeps read access
        # forever -- POSIX checks permissions at open(), not at read. The
        # window is winnable: a tight open() loop retrieved the Argon2 hash and
        # the TOTP secret through a descriptor acquired before the chmod.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        except OSError:
            pass
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                         timeout=2.0)
        except sqlite3.Error as exc:
            raise RuntimeError(f"user store {self.path} is not a readable database: {exc}") from exc
        # Unconditionally, not only on creation: a pre-existing 0644 file was
        # never repaired, unlike AuditLog which chmods every time.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    totp_secret   TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    created_ns    INTEGER NOT NULL,
                    disabled      INTEGER NOT NULL DEFAULT 0
                )""")
            self._conn.commit()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"user store {self.path} is not a readable database: {exc}") from exc

    def load(self) -> Dict[str, "User"]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users").fetchall()
        return {r["username"]: User(username=r["username"],
                                    password_hash=r["password_hash"],
                                    totp_secret=r["totp_secret"],
                                    role=r["role"],
                                    created_ns=int(r["created_ns"]),
                                    disabled=bool(r["disabled"]))
                for r in rows}

    def get(self, username: str) -> Optional["User"]:
        """One account, read fresh. Cheap: a primary-key lookup."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?",
                                     (username,)).fetchone()
        if row is None:
            return None
        return User(username=row["username"], password_hash=row["password_hash"],
                    totp_secret=row["totp_secret"], role=row["role"],
                    created_ns=int(row["created_ns"]), disabled=bool(row["disabled"]))

    def upsert(self, user: "User") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, totp_secret, role, "
                "created_ns, disabled) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, "
                "totp_secret=excluded.totp_secret, role=excluded.role, disabled=excluded.disabled",
                (user.username, user.password_hash, user.totp_secret, user.role,
                 user.created_ns, int(user.disabled)))
            self._conn.commit()

    def delete(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass
class Session:
    token_id: str
    username: str
    role: str
    issued_ns: int
    expires_ns: int
    fingerprint: str
    writes_used: int = 0


class SecurityManager:
    def __init__(self, audit: AuditLog, *, secret: Optional[str] = None,
                 session_ttl_minutes: int = 30, max_login_attempts: int = 5,
                 lockout_minutes: int = 15, write_rate_per_minute: int = 10,
                 read_rate_per_minute: int = 120,
                 store: Optional[UserStore] = None) -> None:
        self.audit = audit
        self.store = store
        # Never hard-code or persist a default secret: a predictable signing key
        # makes every token forgeable.
        self.secret = secret or os.environ.get("SENTINEL_JWT_SECRET") or secrets.token_urlsafe(48)
        if len(self.secret) < 32:
            raise ValueError("SENTINEL_JWT_SECRET must be at least 32 characters")
        self.session_ttl = session_ttl_minutes * 60
        self.max_login_attempts = max_login_attempts
        self.lockout_sec = lockout_minutes * 60
        self.write_rate_per_minute = write_rate_per_minute
        self.read_rate_per_minute = read_rate_per_minute

        self._users: Dict[str, User] = {}
        self._sessions: Dict[str, Session] = {}
        self._failures: Dict[str, List[float]] = {}
        self._lockouts: Dict[str, float] = {}
        # (username, code) -> monotonic time used. A dict rather than a set so
        # entries can age out individually.
        self._used_totp: Dict[Tuple[str, str], float] = {}
        self._strikes: Dict[str, int] = {}
        self._rate: Dict[str, List[float]] = {}
        self._lock = threading.RLock()
        # A SEPARATE lock for account administration, held across the whole
        # check-then-act sequence. `_lock` cannot do this job: it guards every
        # authenticated request, and holding it across a SQLite write stalls
        # them all. Without a lock across the sequence, two concurrent
        # demotions each saw the OTHER owner as still enabled and both
        # succeeded -- leaving zero owners, which is exactly the state the
        # last-owner guard exists to prevent. One owner can produce it alone:
        # TOTP's +/-1 window accepts three distinct codes at any instant.
        self._admin_lock = threading.RLock()
        if store is not None:
            self._users.update(store.load())

    # -- users --------------------------------------------------------------- #

    def add_user(self, username: str, password: str, role: str = "operator",
                 totp_secret: Optional[str] = None, *,
                 actor: str = "cli") -> Tuple[User, str]:
        if role not in ROLES:
            raise ValueError("role must be viewer, operator or owner")
        username = (username or "").strip()
        problem = _check_username(username)
        if problem:
            raise ValueError(problem)
        policy = check_password_policy(password, username=username)
        if policy:
            raise ValueError(policy)
        secret_b32 = totp_secret or generate_totp_secret()
        user = User(username=username, password_hash=hash_password(password),
                    totp_secret=secret_b32, role=role)
        # Check the STORE, under the admin lock, and hold it across the write.
        # The cache-only check let an account created by manage_users.py after
        # boot be silently overwritten: UserStore.upsert is an INSERT ... ON
        # CONFLICT DO UPDATE, so an existing owner's role, Argon2 hash and TOTP
        # secret were all replaced -- and the audit chain recorded it as
        # "user_created", not as the credential replacement it actually was.
        with self._admin_lock:
            if username in self._all_accounts():
                raise ValueError(f"user {username!r} already exists")
            if not self._persist(user):
                raise RuntimeError(f"could not persist account {username!r}")
            with self._lock:
                self._users[username] = user
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "user_created", "username": username,
                           "role": role}, actor=actor)
        return user, totp_uri(secret_b32, username)

    def set_password(self, username: str, password: str, *, actor: str = "cli") -> bool:
        """Rotate a password. The TOTP secret is deliberately NOT rotated: a
        password reset is a routine event, re-enrolling an authenticator is not,
        and conflating them teaches the operator to accept a new second factor
        whenever they are told to."""
        # Hash OUTSIDE the lock: Argon2id is ~90ms and every authenticated
        # request needs the same lock.
        with self._lock:
            existing = self._users.get(username)
        if existing is None:
            return False
        policy = check_password_policy(password, username=username)
        if policy:
            # A ValueError, not a False: "the password was rejected because it
            # is weak" and "there is no such user" must not look the same to
            # the caller, or the dashboard tells an operator their reset
            # silently failed.
            raise ValueError(policy)
        updated = replace(existing, password_hash=hash_password(password))
        # Persist FIRST. Mutating memory before the write meant a failed write
        # left a live privilege change that was absent from disk and from the
        # audit chain, and silently reverted at the next restart.
        if not self._persist(updated):
            return False
        with self._lock:
            self._users[username] = updated
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "password_changed", "username": username}, actor=actor)
        return True

    def _all_accounts(self) -> Dict[str, "User"]:
        """Every account as it exists ON DISK.

        `self._users` is loaded once at construction and refreshed per-username
        only on authentication, while `scripts/manage_users.py` writes the same
        database from another process. Counting owners from the cache meant a
        second owner deleted by the CLI still counted, and the last-owner guard
        cheerfully demoted the only real one.
        """
        if self.store is None:
            with self._lock:
                return dict(self._users)
        try:
            fresh = self.store.load()
        except Exception:  # noqa: BLE001 - a read failure must not unblock the guard
            with self._lock:
                return dict(self._users)
        with self._lock:
            # Keep the caches in step, so the next authentication does not
            # resurrect a name the store no longer has.
            self._users = dict(fresh)
        return fresh

    def enabled_owners(self, *, excluding: str = "") -> List[str]:
        """Usernames that can still administer the system, read from the store."""
        return sorted(u.username for u in self._all_accounts().values()
                      if u.role == "owner" and not u.disabled
                      and u.username != excluding)

    def _last_owner_problem_locked(self, username: str, *, action: str) -> Optional[str]:
        """Refuse a change that would leave nobody able to administer.

        Without this the system can be bricked in one click: demote or disable
        the only owner and NOBODY can change a risk limit, release the kill
        switch, switch the mode back off autonomous, or create another owner.
        The only recovery is the command-line tool on the server -- which the
        person who just locked themselves out of the dashboard may not have.
        """
        accounts = self._all_accounts()
        existing = accounts.get(username)
        if existing is None or existing.role != "owner" or existing.disabled:
            return None
        others = [u.username for u in accounts.values()
                  if u.role == "owner" and not u.disabled and u.username != username]
        if others:
            return None
        return (f"{username} تنها مدیر فعال سامانه است. اگر این حساب {action}، "
                "دیگر هیچ‌کس نمی‌تواند سقف‌های ایمنی را عوض کند، توقف اضطراری "
                "را آزاد کند یا کاربر تازه بسازد. اول یک مدیر دیگر بسازید.")

    def set_role(self, username: str, role: str, *, actor: str = "cli") -> bool:
        if role not in ROLES:
            raise ValueError("role must be viewer, operator or owner")
        with self._admin_lock:
            existing = self._all_accounts().get(username)
            if existing is None:
                return False
            if role != "owner":
                problem = self._last_owner_problem_locked(username, action="مدیر نباشد")
                if problem:
                    raise ValueError(problem)
            previous = existing.role
            updated = replace(existing, role=role)
            if not self._persist(updated):
                return False
            with self._lock:
                self._users[username] = updated
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "role_changed", "username": username,
                           "from": previous, "to": role}, actor=actor)
        return True

    def set_disabled(self, username: str, disabled: bool, *, actor: str = "cli") -> bool:
        """Disable rather than delete. A disabled account keeps its history in
        the audit chain attached to a name that still resolves."""
        with self._lock:
            existing = self._users.get(username)
        if existing is None:
            return False
        with self._admin_lock:
            existing = self._all_accounts().get(username)
            if existing is None:
                return False
            if disabled:
                problem = self._last_owner_problem_locked(username, action="غیرفعال شود")
                if problem:
                    raise ValueError(problem)
            updated = replace(existing, disabled=disabled)
            if not self._persist(updated):
                return False
            with self._lock:
                self._users[username] = updated
                if disabled:
                    for tid in [t for t, sess in self._sessions.items()
                                if sess.username == username]:
                        self._sessions.pop(tid, None)
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "account_disabled" if disabled else "account_enabled",
                           "username": username}, actor=actor)
        return True

    def user_summaries(self) -> List[Dict[str, object]]:
        """Every account, with nothing secret in it.

        Distinct from ``list_users``, which hands back the ``User`` objects for
        internal callers. Two methods rather than one because the HTTP surface
        must never be one refactor away from serialising a password hash.

        No password hash and no TOTP secret, ever, for any caller. An endpoint
        that returns the hash "only to the owner" is one authorisation bug away
        from an offline cracking corpus, and the TOTP secret IS the second
        factor -- handing it back would make the second factor a function of
        the first.
        """
        accounts = self._all_accounts()
        with self._lock:
            users = list(accounts.values())
            sessions = [s.username for s in self._sessions.values()
                        if s.expires_ns > wall_ns()]
        live_owners = {u.username for u in users
                       if u.role == "owner" and not u.disabled}
        out: List[Dict[str, object]] = []
        for user in sorted(users, key=lambda u: (u.role != "owner", u.username)):
            out.append({
                "username": user.username,
                "role": user.role,
                "role_label": ROLES.get(user.role, user.role),
                "disabled": user.disabled,
                "created_ns": user.created_ns,
                "can_write": user.can_write,
                "can_change_risk": user.can_change_risk,
                "active_sessions": sessions.count(user.username),
                # Computed from the SAME snapshot as the rows above, so the
                # badge cannot disagree with the list it is drawn on.
                "is_last_owner": (user.role == "owner" and not user.disabled
                                  and live_owners == {user.username}),
            })
        return out

    def delete_user(self, username: str, *, actor: str = "cli") -> bool:
        """Remove an account entirely.

        Disabling is almost always the better answer and the dashboard says so:
        a deleted name stops resolving, so every audit record that mentions it
        becomes harder to read. Deletion exists for an account created by
        mistake, where there is no history worth keeping.
        """
        with self._admin_lock:
            problem = self._last_owner_problem_locked(username, action="حذف شود")
            if problem:
                raise ValueError(problem)
            if username not in self._all_accounts():
                return False
            if self.store is not None and not self.store.delete(username):
                return False
            with self._lock:
                self._users.pop(username, None)
                for tid in [t for t, sess in self._sessions.items()
                            if sess.username == username]:
                    self._sessions.pop(tid, None)
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "user_deleted", "username": username},
                          actor=actor)
        return True

    def rotate_totp(self, username: str, *, actor: str = "cli") -> Optional[str]:
        """Issue a new second factor. Returns the enrolment URI, once.

        Needed when a phone is lost, and it is the single most abusable action
        in this module: whoever performs it can enrol their own authenticator.
        So it is owner-gated at the API, TOTP-gated like every other write, and
        it drops every live session for that user -- an attacker who rotates a
        factor should not also inherit the sessions it was protecting.
        """
        with self._lock:
            existing = self._users.get(username)
        if existing is None:
            return None
        secret = generate_totp_secret()
        updated = replace(existing, totp_secret=secret)
        if not self._persist(updated):
            return None
        with self._lock:
            self._users[username] = updated
            for tid in [t for t, sess in self._sessions.items()
                        if sess.username == username]:
                self._sessions.pop(tid, None)
            # Any code already "used" under the OLD secret must not block a
            # code that happens to collide under the new one.
            for key in [k for k in self._used_totp if k[0] == username]:
                self._used_totp.pop(key, None)
        self.audit.append(EventType.WRITE_ACTION,
                          {"action": "totp_rotated", "username": username},
                          actor=actor)
        return totp_uri(secret, username)

    def _persist(self, user: "User") -> bool:
        """Write an account to the store, OUTSIDE the auth lock.

        Holding `SecurityManager._lock` across a SQLite write meant a
        concurrent `manage_users.py` holding a transaction froze every
        authenticated request for the full busy timeout and then failed.
        """
        if self.store is None:
            return True
        try:
            self.store.upsert(user)
            return True
        except Exception as exc:  # noqa: BLE001
            self.audit.append(EventType.WRITE_DENIED,
                              {"action": "account_write_failed",
                               "username": user.username, "error": str(exc)})
            return False

    def list_users(self) -> List["User"]:
        with self._lock:
            return sorted(self._users.values(), key=lambda u: u.username)

    def get_user(self, username: str) -> Optional[User]:
        with self._lock:
            return self._users.get(username)

    # -- lockout -------------------------------------------------------------- #

    def _locked_out(self, key: str) -> Optional[float]:
        until = self._lockouts.get(key)
        if until and time.monotonic() < until:
            return until - time.monotonic()
        if until:
            self._lockouts.pop(key, None)
        return None

    def _record_failure(self, key: str) -> None:
        now = time.monotonic()
        window = [t for t in self._failures.get(key, []) if now - t < self.lockout_sec]
        window.append(now)
        self._failures[key] = window
        if len(window) >= self.max_login_attempts:
            # Exponential backoff past the first lockout. The strike count is
            # kept SEPARATELY, because the failure window is cleared when the
            # lockout arms -- so deriving the exponent from the window made it
            # 2**0 == 1 every time and every lockout was the same length.
            self._strikes[key] = self._strikes.get(key, 0) + 1
            factor = 2 ** max(0, self._strikes[key] - 1)
            self._lockouts[key] = now + self.lockout_sec * min(factor, 16)
            self._failures[key] = []

    def _prune_tracking(self) -> None:
        """Bound the attacker-keyed maps.

        `_failures`, `_rate`, `_lockouts` and `_strikes` are keyed on strings
        the caller supplies (a username, a forwarded address). Without pruning,
        20,000 forged addresses cost ~8MB and the growth is unbounded -- which
        the systemd unit's OOMPolicy=stop then turns into a stopped engine.
        """
        now = time.monotonic()
        horizon = max(self.lockout_sec, 60.0) * 4
        for name in ("_failures", "_rate"):
            table = getattr(self, name)
            for key in [k for k, v in table.items()
                        if not v or now - max(v) > horizon]:
                table.pop(key, None)
            if len(table) > _MAX_TRACKED_KEYS:
                # Keep the most recent; an attacker cannot evict a real user's
                # counter faster than it is refreshed by their own activity.
                keep = sorted(table.items(), key=lambda kv: max(kv[1]) if kv[1] else 0,
                              reverse=True)[:_MAX_TRACKED_KEYS]
                setattr(self, name, dict(keep))
        for key in [k for k, until in self._lockouts.items() if now > until + horizon]:
            self._lockouts.pop(key, None)
            self._strikes.pop(key, None)
        # Size cap as well as age. Each forged key costs an attacker ~90ms of
        # Argon2, so growth is slow -- but "slow and unbounded" is still
        # unbounded, and OOMPolicy=stop turns that into a stopped engine.
        # Evict the SOONEST-EXPIRING first, so a real user's long lockout is
        # the last thing to go.
        if len(self._lockouts) > _MAX_TRACKED_KEYS:
            keep = dict(sorted(self._lockouts.items(), key=lambda kv: kv[1],
                               reverse=True)[:_MAX_TRACKED_KEYS])
            self._lockouts = keep
            self._strikes = {k: v for k, v in self._strikes.items() if k in keep}
        # TOTP replay entries age out rather than being dropped wholesale: a
        # blanket clear() removed replay protection for every user at once.
        cutoff = now - _TOTP_REPLAY_TTL_SEC
        for key in [k for k, ts in self._used_totp.items() if ts < cutoff]:
            self._used_totp.pop(key, None)
        # Expired sessions were only ever reclaimed when their own token was
        # presented again, so the table grew for the life of the process.
        wall = wall_ns()
        for tid in [t for t, sess in self._sessions.items() if wall > sess.expires_ns]:
            self._sessions.pop(tid, None)

    def _reload_user(self, username: str) -> Optional["User"]:
        """Read this account from the store, refreshing the in-memory copy.

        Accounts are changed by `scripts/manage_users.py`, which runs in a
        SEPARATE process. Without this, disabling or demoting someone had no
        effect on the running server at all: the CLI reported success, the
        store was updated, and the account kept full write authority until the
        next restart.
        """
        if self.store is None:
            return self._users.get(username)
        try:
            fresh = self.store.get(username)
        except Exception:  # noqa: BLE001 - a store read failure must not lock everyone out
            return self._users.get(username)
        if fresh is None:
            self._users.pop(username, None)
            return None
        self._users[username] = fresh
        return fresh

    # -- login ---------------------------------------------------------------- #

    @staticmethod
    def fingerprint(user_agent: str, client_ip: str) -> str:
        return hashlib.sha256(f"{user_agent}|{client_ip}".encode("utf-8")).hexdigest()[:32]

    def login(self, username: str, password: str, *, user_agent: str = "",
              client_ip: str = "") -> Tuple[Optional[str], str]:
        # Two independent lockout keys. Keying only on (username, ip) means an
        # attacker rotating source addresses never locks the ACCOUNT, and behind
        # a reverse proxy every client shares one ip so the account key is the
        # only one that works. Both are required.
        keys = [f"user:{username}", f"ip:{client_ip}"]
        # Read the stored hash under the lock, then release it. Argon2id is
        # deliberately ~90ms of work; holding the global auth lock across it
        # meant one unauthenticated login attempt stalled every authenticated
        # request behind it (measured: verify_token 0.2ms -> 458ms). An
        # attacker could make the dashboard unusable -- no login, no kill, no
        # flatten -- from outside, with no credentials.
        with self._lock:
            self._prune_tracking()
            remaining = next((r for r in (self._locked_out(k) for k in keys) if r), None)
            if remaining is None:
                user = self._reload_user(username)
                stored_hash = user.password_hash if user else _DUMMY_HASH
        if remaining:
            self.audit.append(EventType.AUTH_FAILURE,
                              {"username": username, "reason": "locked_out",
                               "retry_in_sec": int(remaining)}, actor=client_ip)
            return None, f"locked out; try again in {int(remaining)}s"

        # Equal work whether or not the user exists: one verify either way,
        # against the real hash or the precomputed placeholder. OUTSIDE the lock.
        ok = verify_password(password, stored_hash)

        with self._lock:
            if user is None or not ok or user.disabled:
                for k in keys:
                    self._record_failure(k)
                self.audit.append(EventType.AUTH_FAILURE,
                                  {"username": username, "reason": "bad_credentials"},
                                  actor=client_ip)
                return None, "invalid credentials"

            for k in keys:
                self._failures.pop(k, None)
            token_id = secrets.token_urlsafe(24)
            now = wall_ns()
            session = Session(
                token_id=token_id, username=username, role=user.role, issued_ns=now,
                expires_ns=now + self.session_ttl * 1_000_000_000,
                fingerprint=self.fingerprint(user_agent, client_ip))
            self._sessions[token_id] = session

        payload = {"sub": username, "jti": token_id, "role": user.role,
                   "fp": session.fingerprint,
                   "exp": int(session.expires_ns / 1e9), "iat": int(now / 1e9)}
        token = jwt.encode(payload, self.secret, algorithm=ALGORITHM)
        self.audit.append(EventType.AUTH_SUCCESS,
                          {"username": username, "role": user.role}, actor=client_ip)
        return token, "ok"

    def verify_token(self, token: str, *, user_agent: str = "",
                     client_ip: str = "") -> Optional[Session]:
        try:
            payload = jwt.decode(token, self.secret, algorithms=[ALGORITHM])
        except JWTError:
            return None
        token_id = payload.get("jti", "")
        with self._lock:
            session = self._sessions.get(token_id)
            if session is None or wall_ns() > session.expires_ns:
                self._sessions.pop(token_id, None)
                return None
            # Binding the token to the client makes a stolen token much less useful.
            if session.fingerprint != self.fingerprint(user_agent, client_ip):
                self.audit.append(EventType.AUTH_FAILURE,
                                  {"username": session.username,
                                   "reason": "fingerprint_mismatch"}, actor=client_ip)
                return None

            # Re-resolve the account on every request rather than trusting the
            # role baked into the token at login. Without this, downgrading
            # someone from owner to viewer leaves them with owner authority
            # until their session expires -- which is exactly the window in
            # which you downgraded them for a reason.
            # Re-read from the STORE, not just the in-memory cache. Account
            # changes are made by scripts/manage_users.py in another process,
            # so an in-memory lookup could never see them: a disabled operator
            # kept full write authority until the process restarted.
            user = self._reload_user(session.username)
            if user is None or user.disabled:
                self._sessions.pop(token_id, None)
                self.audit.append(EventType.AUTH_FAILURE,
                                  {"username": session.username,
                                   "reason": "account_disabled_or_removed"}, actor=client_ip)
                return None
            if user.role != session.role:
                self.audit.append(EventType.WRITE_DENIED,
                                  {"username": session.username,
                                   "reason": "role_changed_mid_session",
                                   "from": session.role, "to": user.role}, actor=client_ip)
                session.role = user.role
            return session

    def logout(self, token_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(token_id, None) is not None

    # -- second factor --------------------------------------------------------- #

    def verify_totp(self, username: str, code: str, *,
                    consume: bool = True) -> Tuple[bool, str]:
        """Check a TOTP code.

        ``consume`` marks the code used. The caller passes ``False`` to check a
        code on a write that may still be refused for another reason: burning
        the operator's valid code on a rate-limited attempt locked them out of
        their own emergency controls for 30 seconds at a time, with "this code
        has already been used" as the only explanation.
        """
        with self._lock:
            user = self._users.get(username)
            if user is None or user.disabled:
                return False, "unknown user"
            code = (code or "").strip().replace(" ", "")
            if not code.isdigit() or len(code) != 6:
                return False, "a TOTP code is six digits"
            # Replay protection: a code is single-use inside its window.
            if (username, code) in self._used_totp:
                self.audit.append(EventType.WRITE_DENIED,
                                  {"username": username, "reason": "totp_replay"})
                return False, "this code has already been used"
            totp = pyotp.TOTP(user.totp_secret)
            if not totp.verify(code, valid_window=1):
                self.audit.append(EventType.WRITE_DENIED,
                                  {"username": username, "reason": "totp_invalid"})
                return False, "invalid code"
            if consume:
                self._used_totp[(username, code)] = time.monotonic()
            return True, "ok"

    def consume_totp(self, username: str, code: str) -> None:
        code = (code or "").strip().replace(" ", "")
        with self._lock:
            self._used_totp[(username, code)] = time.monotonic()

    def authorise_write(self, session: Session, totp_code: str, action: str,
                        *, requires_owner: bool = False) -> Tuple[bool, str]:
        with self._lock:
            user = self._reload_user(session.username)
        if user is None or not user.can_write:
            self.audit.append(EventType.WRITE_DENIED,
                              {"username": session.username, "action": action,
                               "reason": "role_not_permitted"})
            return False, "this role cannot perform write actions"
        if requires_owner and not user.can_change_risk:
            self.audit.append(EventType.WRITE_DENIED,
                              {"username": session.username, "action": action,
                               "reason": "owner_required"})
            return False, "only the owner may change a risk limit"
        # RATE LIMIT FIRST. Checking the code before the limiter meant a wrong
        # code consumed no budget and tripped no lockout at all: measured at
        # 474 guesses/second against a six-digit secret, with nothing slowing
        # down, alerting, or locking the account. A second factor that can be
        # brute-forced at that rate is not a second factor.
        if not self.check_rate(f"write:{session.username}", self.write_rate_per_minute):
            self.audit.append(EventType.WRITE_DENIED,
                              {"username": session.username, "action": action,
                               "reason": "rate_limited"})
            return False, "too many write actions; slow down"
        # An armed lockout must reach a session that is ALREADY OPEN. Checking
        # it only at login meant a holder of a live token kept guessing TOTP
        # codes at the write rate limit indefinitely while the account was
        # nominally locked -- the lockout stopped the door and left the window.
        with self._lock:
            remaining = self._locked_out(f"user:{session.username}")
        if remaining:
            self.audit.append(EventType.WRITE_DENIED,
                              {"username": session.username, "action": action,
                               "reason": "locked_out"})
            return False, (f"this account is locked for another {int(remaining)}s "
                           "after repeated failures")

        # Check without consuming, so a code is only burned by a write that
        # actually proceeds.
        ok, msg = self.verify_totp(session.username, totp_code, consume=False)
        if not ok:
            # A failed second factor is a login-grade failure and counts toward
            # the same lockout, on the username key. Once it arms, every
            # session belonging to that user is dropped.
            with self._lock:
                self._record_failure(f"user:{session.username}")
                if self._locked_out(f"user:{session.username}"):
                    for tid in [t for t, sess in self._sessions.items()
                                if sess.username == session.username]:
                        self._sessions.pop(tid, None)
            return False, msg
        self.consume_totp(session.username, totp_code)
        with self._lock:
            session.writes_used += 1
        self.audit.append(EventType.WRITE_ACTION,
                          {"username": session.username, "action": action},
                          actor=session.username)
        return True, "ok"

    # -- rate limiting ---------------------------------------------------------- #

    def check_rate(self, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        with self._lock:
            window = [t for t in self._rate.get(key, []) if now - t < 60]
            if len(window) >= per_minute:
                self._rate[key] = window
                return False
            window.append(now)
            self._rate[key] = window
            return True

    def purge_expired(self) -> int:
        now = wall_ns()
        with self._lock:
            stale = [k for k, s in self._sessions.items() if s.expires_ns < now]
            for k in stale:
                self._sessions.pop(k, None)
        return len(stale)

    @property
    def active_sessions(self) -> int:
        self.purge_expired()
        with self._lock:
            return len(self._sessions)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    # The dashboard bundles everything it needs; no external origin is allowed
    # to inject script into a page that can move money.
    # connect-src is 'self' ONLY. "ws: wss:" are scheme-only sources with no
    # host restriction, so injected script could have exfiltrated to any
    # websocket endpoint on the internet -- from a page that can move money.
    # A same-origin /ws is covered by 'self' in every current browser.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}
