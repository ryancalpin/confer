"""Local node state in one SQLite file. Nothing here ever leaves the node."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    agent_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    endpoint   TEXT NOT NULL DEFAULT '',
    relay      TEXT NOT NULL DEFAULT '',
    grants     TEXT NOT NULL DEFAULT '[]',   -- what THEY may do with MY node
    status     TEXT NOT NULL DEFAULT 'active', -- pending | active
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    secret_hash TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    grants      TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS seen (
    env_id     TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    contact    TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    actionable INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'open',  -- open | done
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    to_id      TEXT NOT NULL,
    envelope   TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    REAL NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mailbox (          -- relay role only
    env_id     TEXT PRIMARY KEY,
    to_id      TEXT NOT NULL,
    envelope   TEXT NOT NULL,
    stored_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mailbox_to ON mailbox(to_id, stored_at);
CREATE TABLE IF NOT EXISTS intros (           -- introductions offered TO me
    intro_id    TEXT PRIMARY KEY,
    peer_id     TEXT NOT NULL,
    peer_name   TEXT NOT NULL,
    endpoint    TEXT NOT NULL DEFAULT '',
    relay       TEXT NOT NULL DEFAULT '',
    introducer  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,                  -- offered | accepted | declined | done
    peer_ready  INTEGER NOT NULL DEFAULT 0,     -- the peer already accepted their side
    expires_at  REAL NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lists (            -- shared lists (owned or joined)
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (           -- expenses and settlements with one contact
    id         TEXT PRIMARY KEY,
    contact    TEXT NOT NULL,
    data       TEXT NOT NULL,
    status     TEXT NOT NULL,                 -- pending | accepted | disputed | cancelled
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_contact ON ledger(contact, created_at);
CREATE TABLE IF NOT EXISTS presence (         -- latest shared status/ETA/location per contact
    contact    TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


@dataclass
class Contact:
    agent_id: str
    name: str
    endpoint: str = ""
    relay: str = ""
    grants: list[str] = field(default_factory=list)
    status: str = "active"
    created_at: float = 0.0

    def can(self, grant: str) -> bool:
        return self.status == "active" and grant in self.grants


@dataclass
class InboxItem:
    id: int
    kind: str
    ref: str
    contact: str
    summary: str
    payload: dict
    actionable: bool
    status: str
    created_at: float


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def transaction(self) -> "_Tx":
        return _Tx(self)

    # contacts --------------------------------------------------------------
    @staticmethod
    def _contact(r: sqlite3.Row) -> Contact:
        return Contact(r["agent_id"], r["name"], r["endpoint"], r["relay"], json.loads(r["grants"]), r["status"], r["created_at"])

    def upsert_contact(self, c: Contact) -> None:
        self._x(
            "INSERT INTO contacts(agent_id,name,endpoint,relay,grants,status,created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET name=excluded.name, endpoint=excluded.endpoint, relay=excluded.relay, "
            "grants=excluded.grants, status=excluded.status",
            (c.agent_id, c.name, c.endpoint, c.relay, json.dumps(sorted(set(c.grants))), c.status, c.created_at or time.time()),
        )

    def contact(self, agent_id: str) -> Contact | None:
        rows = self._q("SELECT * FROM contacts WHERE agent_id=?", (agent_id,))
        return self._contact(rows[0]) if rows else None

    def contact_by_name(self, name: str) -> Contact | None:
        rows = self._q("SELECT * FROM contacts WHERE lower(name)=lower(?)", (name,))
        return self._contact(rows[0]) if rows else None

    def contacts(self) -> list[Contact]:
        return [self._contact(r) for r in self._q("SELECT * FROM contacts ORDER BY name")]

    def remove_contact(self, agent_id: str) -> bool:
        return self._x("DELETE FROM contacts WHERE agent_id=?", (agent_id,)).rowcount > 0

    # invites ---------------------------------------------------------------
    def add_invite(self, secret_hash: str, name: str, grants: list[str], expires_at: float) -> None:
        self._x("INSERT INTO invites(secret_hash,name,grants,expires_at) VALUES(?,?,?,?)", (secret_hash, name, json.dumps(grants), expires_at))

    def consume_invite(self, secret_hash: str, now: float) -> tuple[str, list[str]] | None:
        """Atomically mark a valid invite used; single-use by construction."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE invites SET used_at=? WHERE secret_hash=? AND used_at IS NULL AND expires_at>?",
                (now, secret_hash, now),
            )
            if cur.rowcount != 1:
                return None
            r = self._db.execute("SELECT name, grants FROM invites WHERE secret_hash=?", (secret_hash,)).fetchone()
            return r["name"], json.loads(r["grants"])

    # replay protection -----------------------------------------------------
    def mark_seen(self, env_id: str, ttl: float, now: float) -> bool:
        """True if newly seen; False if this envelope id was already processed."""
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE expires_at<?", (now,))
            try:
                self._db.execute("INSERT INTO seen(env_id,expires_at) VALUES(?,?)", (env_id, now + ttl))
                return True
            except sqlite3.IntegrityError:
                return False

    # plans -----------------------------------------------------------------
    def save_plan(self, plan: dict) -> None:
        self._x(
            "INSERT INTO plans(id,data,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (plan["id"], json.dumps(plan), time.time()),
        )

    def plan(self, plan_id: str) -> dict | None:
        """Exact lookup — the only form protocol handlers may use."""
        rows = self._q("SELECT data FROM plans WHERE id=?", (plan_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def find_plan(self, prefix: str) -> dict | None:
        """Owner convenience: exact id, or a unique id prefix (as typed in the CLI)."""
        if (plan := self.plan(prefix)) or not prefix.isalnum():
            return plan
        rows = self._q("SELECT data FROM plans WHERE substr(id, 1, ?) = ?", (len(prefix), prefix))
        return json.loads(rows[0]["data"]) if len(rows) == 1 else None

    def plans(self) -> list[dict]:
        return [json.loads(r["data"]) for r in self._q("SELECT data FROM plans ORDER BY updated_at DESC")]

    # inbox -----------------------------------------------------------------
    def add_inbox(self, kind: str, summary: str, *, ref: str = "", contact: str = "", payload: dict | None = None, actionable: bool = False) -> int:
        cur = self._x(
            "INSERT INTO inbox(kind,ref,contact,summary,payload,actionable,created_at) VALUES(?,?,?,?,?,?,?)",
            (kind, ref, contact, summary, json.dumps(payload or {}), int(actionable), time.time()),
        )
        return int(cur.lastrowid)

    def inbox(self, include_done: bool = False) -> list[InboxItem]:
        sql = "SELECT * FROM inbox" + ("" if include_done else " WHERE status='open'") + " ORDER BY id"
        return [
            InboxItem(r["id"], r["kind"], r["ref"], r["contact"], r["summary"], json.loads(r["payload"]), bool(r["actionable"]), r["status"], r["created_at"])
            for r in self._q(sql)
        ]

    def close_inbox(self, item_id: int | None = None, *, ref: str | None = None, kind: str | None = None) -> int:
        if item_id is not None:
            return self._x("UPDATE inbox SET status='done' WHERE id=?", (item_id,)).rowcount
        sql, args = "UPDATE inbox SET status='done' WHERE status='open' AND ref=?", [ref]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self._x(sql, tuple(args)).rowcount

    # outbox ----------------------------------------------------------------
    def enqueue(self, to_id: str, envelope: dict, now: float) -> int:
        cur = self._x("INSERT INTO outbox(to_id,envelope,next_at,created_at) VALUES(?,?,?,?)", (to_id, json.dumps(envelope), now, now))
        return int(cur.lastrowid)

    def due(self, now: float) -> Iterator[tuple[int, str, dict, int]]:
        for r in self._q("SELECT * FROM outbox WHERE next_at<=? ORDER BY id", (now,)):
            yield r["id"], r["to_id"], json.loads(r["envelope"]), r["attempts"]

    def queue(self) -> Iterator[tuple[int, str, dict, int, float]]:
        """Whole outbox in send order: (id, to_id, envelope, attempts, next_at)."""
        for r in self._q("SELECT * FROM outbox ORDER BY id"):
            yield r["id"], r["to_id"], json.loads(r["envelope"]), r["attempts"], r["next_at"]

    def delivered(self, out_id: int) -> None:
        self._x("DELETE FROM outbox WHERE id=?", (out_id,))

    def retry_later(self, out_id: int, attempts: int, next_at: float, error: str) -> None:
        self._x("UPDATE outbox SET attempts=?, next_at=?, last_error=? WHERE id=?", (attempts, next_at, error[:500], out_id))

    def outbox(self) -> list[dict]:
        return [dict(r) | {"envelope": None} for r in self._q("SELECT * FROM outbox ORDER BY id")]

    # introductions ---------------------------------------------------------
    def save_intro(self, intro: dict) -> None:
        cols = ("intro_id", "peer_id", "peer_name", "endpoint", "relay", "introducer", "note", "status", "peer_ready", "expires_at", "created_at")
        self._x(
            f"INSERT INTO intros({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT(intro_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols[1:]),
            tuple(intro[c] for c in cols),
        )

    def intro(self, intro_id: str) -> dict | None:
        rows = self._q("SELECT * FROM intros WHERE intro_id=?", (intro_id,))
        return dict(rows[0]) if rows else None

    def intros(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM intros ORDER BY created_at DESC")]

    # shared lists ----------------------------------------------------------
    def save_list(self, lst: dict) -> None:
        self._x("INSERT INTO lists(id,data,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (lst["id"], json.dumps(lst), time.time()))

    def get_list(self, list_id: str) -> dict | None:
        rows = self._q("SELECT data FROM lists WHERE id=?", (list_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def find_list(self, prefix: str) -> dict | None:
        if (lst := self.get_list(prefix)) or not prefix.isalnum():
            return lst
        rows = self._q("SELECT data FROM lists WHERE substr(id, 1, ?) = ?", (len(prefix), prefix))
        return json.loads(rows[0]["data"]) if len(rows) == 1 else None

    def all_lists(self) -> list[dict]:
        return [json.loads(r["data"]) for r in self._q("SELECT data FROM lists ORDER BY updated_at DESC")]

    def delete_list(self, list_id: str) -> None:
        self._x("DELETE FROM lists WHERE id=?", (list_id,))

    # money ledger ----------------------------------------------------------
    def save_entry(self, entry: dict) -> None:
        self._x(
            "INSERT INTO ledger(id,contact,data,status,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, status=excluded.status, contact=excluded.contact",
            (entry["id"], entry["contact"], json.dumps(entry), entry["status"], entry.get("created_at", time.time())),
        )

    def entry(self, entry_id: str) -> dict | None:
        rows = self._q("SELECT data FROM ledger WHERE id=?", (entry_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def find_entry(self, prefix: str) -> dict | None:
        if (e := self.entry(prefix)) or not prefix.isalnum():
            return e
        rows = self._q("SELECT data FROM ledger WHERE substr(id, 1, ?) = ?", (len(prefix), prefix))
        return json.loads(rows[0]["data"]) if len(rows) == 1 else None

    def entries(self, contact: str | None = None) -> list[dict]:
        if contact:
            rows = self._q("SELECT data FROM ledger WHERE contact=? ORDER BY created_at", (contact,))
        else:
            rows = self._q("SELECT data FROM ledger ORDER BY created_at")
        return [json.loads(r["data"]) for r in rows]

    # presence --------------------------------------------------------------
    def set_presence(self, contact: str, data: dict, expires_at: float) -> None:
        self._x("INSERT INTO presence(contact,data,expires_at) VALUES(?,?,?) ON CONFLICT(contact) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at",
                (contact, json.dumps(data), expires_at))

    def clear_presence(self, contact: str) -> None:
        self._x("DELETE FROM presence WHERE contact=?", (contact,))

    def presence(self, now: float) -> dict[str, dict]:
        self._x("DELETE FROM presence WHERE expires_at<=?", (now,))  # never keep a location history
        return {r["contact"]: json.loads(r["data"]) for r in self._q("SELECT contact, data FROM presence")}

    # key rotation ----------------------------------------------------------
    def rename_agent(self, old: str, new: str) -> None:
        """A contact (or this node) moved to a new key: re-point every local record."""
        with self._lock:
            self._db.execute("UPDATE contacts SET agent_id=? WHERE agent_id=?", (new, old))
            self._db.execute("UPDATE outbox SET to_id=? WHERE to_id=?", (new, old))
            self._db.execute("UPDATE inbox SET contact=? WHERE contact=?", (new, old))
            self._db.execute("UPDATE intros SET peer_id=? WHERE peer_id=?", (new, old))
            self._db.execute("UPDATE intros SET introducer=? WHERE introducer=?", (new, old))
            self._db.execute("UPDATE ledger SET contact=? WHERE contact=?", (new, old))
            self._db.execute("UPDATE presence SET contact=? WHERE contact=?", (new, old))
            for table in ("lists", "ledger"):
                for r in self._db.execute(f"SELECT id, data FROM {table} WHERE instr(data, ?) > 0", (old,)).fetchall():  # noqa: S608
                    self._db.execute(f"UPDATE {table} SET data=? WHERE id=?", (_swap_json(r["data"], old, new), r["id"]))  # noqa: S608
            for r in self._db.execute("SELECT id, data FROM plans WHERE instr(data, ?) > 0", (old,)).fetchall():
                self._db.execute("UPDATE plans SET data=? WHERE id=?", (_swap_json(r["data"], old, new), r["id"]))

    # relay mailbox ---------------------------------------------------------
    def mailbox_put(self, env: dict, now: float, per_recipient_cap: int) -> bool:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM mailbox WHERE to_id=?", (env["to"],)).fetchone()[0]
            if n >= per_recipient_cap:
                return False
            self._db.execute(
                "INSERT OR IGNORE INTO mailbox(env_id,to_id,envelope,stored_at) VALUES(?,?,?,?)",
                (env["id"], env["to"], json.dumps(env), now),
            )
            return True

    def mailbox_get(self, to_id: str, limit: int = 100) -> list[dict]:
        rows = self._q("SELECT envelope FROM mailbox WHERE to_id=? ORDER BY stored_at LIMIT ?", (to_id, limit))
        return [json.loads(r["envelope"]) for r in rows]

    def mailbox_ack(self, to_id: str, env_ids: list[str]) -> int:
        n = 0
        for eid in env_ids:
            n += self._x("DELETE FROM mailbox WHERE to_id=? AND env_id=?", (to_id, eid)).rowcount
        return n

    def mailbox_expire(self, before: float) -> int:
        return self._x("DELETE FROM mailbox WHERE stored_at<?", (before,)).rowcount


def _swap_json(data: str, old: str, new: str) -> str:
    """Replace an agent id where it *is* a value or key — never inside free text
    (a note could legitimately quote someone's id)."""

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {(new if k == old else k): walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return new if o == old else o

    return json.dumps(walk(json.loads(data)))


class _Tx:
    """Serialise a read-modify-write sequence (e.g. plan updates) across threads."""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self) -> Store:
        self.store._lock.acquire()
        return self.store

    def __exit__(self, *exc: Any) -> None:
        self.store._lock.release()
