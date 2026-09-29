"""Optional, local-only remote identity storage; never part of production settings."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

SESSION_SECONDS = 12 * 60 * 60


class RemoteRole(StrEnum):
    VIEWER = "Viewer"
    OPERATOR = "Operator"


class RemoteAuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class RemoteSession:
    user_id: str
    email: str
    role: RemoteRole
    expires_at: float


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"stagepilot-remote-csrf-v1", hashlib.sha256).hexdigest()


def normalize_email(value: str) -> str:
    value = value.strip().casefold()
    if len(value) > 254 or value.count("@") != 1 or any(c.isspace() for c in value):
        raise RemoteAuthError(422, "A valid email address is required.")
    local, domain = value.split("@")
    if not local or not domain or "." not in domain or domain.startswith("."):
        raise RemoteAuthError(422, "A valid email address is required.")
    return value


class RemoteStore:
    """SQLite transactions enforce last-Operator protection across connections.

    Construction does no I/O. Failures affect remote requests only. Call methods
    on the remote-specific worker limiter, never on the production event loop.
    Session tokens are hashed at rest; user changes revoke all their sessions.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the database privately before SQLite opens it (no credential bytes yet).
        self.path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('Viewer', 'Operator')),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL
                );
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"],
            "enabled": bool(row["enabled"]),
        }

    def installation_generation(self, generation: str) -> None:
        """Re-enable invalidates sessions, while restart in the same epoch preserves them."""
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS remote_generation (id INTEGER PRIMARY KEY, value TEXT)"
            )
            row = db.execute("SELECT value FROM remote_generation WHERE id=1").fetchone()
            if row is None or row[0] != generation:
                db.execute("DELETE FROM sessions")
                db.execute("INSERT OR REPLACE INTO remote_generation VALUES (1, ?)", (generation,))

    def users(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [self._user(row) for row in db.execute("SELECT * FROM users ORDER BY email")]

    def claim_action(self, key: str, fingerprint: str, expires: float) -> tuple[int, bytes] | None:
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS remote_actions "
                "(key TEXT PRIMARY KEY, fingerprint TEXT, expires REAL, "
                "status INTEGER, response BLOB)"
            )
            db.execute("DELETE FROM remote_actions WHERE expires<=?", (self.clock(),))
            row = db.execute("SELECT * FROM remote_actions WHERE key=?", (key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise RemoteAuthError(409, "This request identifier belongs to another action.")
                if row["status"] is None:
                    raise RemoteAuthError(
                        409, "Action outcome is pending or uncertain. Check live state."
                    )
                return int(row["status"]), bytes(row["response"])
            if db.execute("SELECT count(*) FROM remote_actions").fetchone()[0] >= 2048:
                raise RemoteAuthError(429, "Remote action capacity reached. Try again later.")
            db.execute(
                "INSERT INTO remote_actions VALUES (?, ?, ?, NULL, NULL)",
                (key, fingerprint, expires),
            )
            return None

    def finish_action(self, key: str, status: int, response: bytes) -> None:
        if len(response) > 262144:
            raise RemoteAuthError(409, "Action completed. Check live state before continuing.")
        with self._db() as db:
            db.execute(
                "UPDATE remote_actions SET status=?, response=? WHERE key=?",
                (status, response, key),
            )

    def bootstrap(self, email: str, password: str) -> dict[str, Any]:
        return self.create_user(email, password, RemoteRole.OPERATOR, first_only=True)

    def create_user(
        self, email: str, password: str, role: RemoteRole, *, first_only: bool = False
    ) -> dict[str, Any]:
        email = normalize_email(email)
        if not 12 <= len(password) <= 1024:
            raise RemoteAuthError(422, "Passwords must contain 12 to 1024 characters.")
        encoded = self.hasher.hash(password)
        with self._db() as db:
            if first_only and db.execute("SELECT 1 FROM users").fetchone():
                raise RemoteAuthError(409, "The first Operator already exists.")
            if not db.execute("SELECT 1 FROM users").fetchone() and role != RemoteRole.OPERATOR:
                raise RemoteAuthError(409, "The first remote user must be an Operator.")
            user_id = secrets.token_urlsafe(18)
            try:
                db.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, 1)",
                    (user_id, email, encoded, role.value),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteAuthError(409, "That email is already registered.") from exc
            return {"id": user_id, "email": email, "role": role.value, "enabled": True}

    def update_user(
        self,
        user_id: str,
        *,
        role: RemoteRole | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        delete: bool = False,
    ) -> None:
        encoded = None
        if password is not None:
            if not 12 <= len(password) <= 1024:
                raise RemoteAuthError(422, "Passwords must contain 12 to 1024 characters.")
            encoded = self.hasher.hash(password)
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise RemoteAuthError(404, "Remote user not found.")
            if (
                row["enabled"]
                and row["role"] == RemoteRole.OPERATOR
                and (delete or enabled is False or role == RemoteRole.VIEWER)
            ):
                operators = db.execute(
                    "SELECT count(*) FROM users WHERE enabled=1 AND role='Operator'"
                ).fetchone()[0]
                if operators <= 1:
                    raise RemoteAuthError(409, "The last enabled Operator cannot be removed.")
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            if delete:
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
            else:
                db.execute(
                    "UPDATE users SET role=?, enabled=?, password=? WHERE id=?",
                    (
                        role.value if role else row["role"],
                        enabled if enabled is not None else row["enabled"],
                        encoded or row["password"],
                        user_id,
                    ),
                )

    def _throttle(self, email: str) -> None:
        now = self.clock()
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE start<=?", (now - 300,))
            keys = (("global", 30), ("email:" + digest(email), 5))
            for key, limit in keys:
                row = db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()
                if row and row[0] >= limit:
                    raise RemoteAuthError(429, "Too many login attempts. Try again later.")
            for key, _ in keys:
                db.execute(
                    "INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(key) "
                    "DO UPDATE SET count=count+1",
                    (key, now),
                )

    def login(self, email: str, password: str, previous: str | None = None) -> str:
        email = email.strip().casefold()
        self._throttle(email)  # Persist before verification, including unknown users.
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row is None:
                # Same Argon2 cost, no user enumeration through the cheap missing-user path.
                self.hasher.hash(password)
                raise RemoteAuthError(401, "Incorrect email or password.")
            try:
                self.hasher.verify(row["password"], password)
            except (VerificationError, InvalidHashError) as exc:
                raise RemoteAuthError(401, "Incorrect email or password.") from exc
            if not row["enabled"]:
                raise RemoteAuthError(401, "Incorrect email or password.")
            if self.hasher.check_needs_rehash(row["password"]):
                db.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (self.hasher.hash(password), row["id"]),
                )
            now = self.clock()
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            if previous:
                db.execute("DELETE FROM sessions WHERE token=?", (digest(previous),))
            # Bound session growth per account without limiting concurrent users.
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND token NOT IN "
                "(SELECT token FROM sessions WHERE user_id=? ORDER BY expires DESC LIMIT 9)",
                (row["id"], row["id"]),
            )
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (digest(token), row["id"], now + SESSION_SECONDS),
            )
            return token

    def session(self, token: str | None) -> RemoteSession | None:
        if not token or len(token) > 128:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT users.*, sessions.expires FROM sessions JOIN users "
                "ON users.id=sessions.user_id WHERE token=? AND expires>? AND enabled=1",
                (digest(token), self.clock()),
            ).fetchone()
            if row is None:
                return None
            return RemoteSession(row["id"], row["email"], RemoteRole(row["role"]), row["expires"])

    def reset_login_attempts(self) -> None:
        """Clear the login throttle counters for this installation.

        Idempotent and safe to run when no counters exist: it only drops rows
        from `attempts` (the same table `_throttle()` maintains and prunes),
        never users or sessions. Applied in response to an operator-queued
        `rate_limit_reset` action from the control plane.
        """

        with self._db() as db:
            db.execute("DELETE FROM attempts")

    def revoke(self, token: str, *, all_sessions: bool = False) -> None:
        with self._db() as db:
            if all_sessions:
                db.execute(
                    "DELETE FROM sessions WHERE user_id IN "
                    "(SELECT user_id FROM sessions WHERE token=?)",
                    (digest(token),),
                )
            else:
                db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))
