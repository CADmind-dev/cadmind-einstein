"""The validator's durable state (DESIGN.md): one SQLite file in WAL mode, written atomically.

Everything a restart must not lose lives here -- the jobs with their attempts and held failures, the
miners (key, admission, blocks, last poll), the scored outcomes with their client signals (the
decayed history is recomputed from them), the copy check's recent outputs, the rejection
telemetry, the results not yet taken by the platform, the keys still to revoke, the last hour
closed and the commitments still to send.

Rows are JSON in one key-value table, namespaced (``job``, ``miner``, ``outcome``, ``copy``,
``meta``). A desk mutation and the ledger writes it causes share ONE transaction
(:meth:`Store.tx`, re-entrant), so a crash leaves either the state before the mutation or the state
after it, never a scored outcome without the job that ended. ``synchronous=FULL``: a committed
transaction survives a power cut, not just a process crash.

The store's lock is also the ledger's lock (the ledger is given the store), so the order is always
the desk's lock, then this one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = 1


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._depth = 0
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL,"
            " PRIMARY KEY (ns, k))"
        )
        with self.tx():
            have = self.get("meta", "schema")
            if have is None:
                self.put("meta", "schema", SCHEMA)
            elif int(have) != SCHEMA:
                raise RuntimeError(f"{self.path}: schema {have}, this validator reads {SCHEMA}")

    @contextmanager
    def tx(self) -> Iterator[None]:
        """One atomic transaction; nested calls join the outer one."""
        with self.lock:
            if self._depth == 0:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            ok = False
            try:
                yield
                ok = True
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self.db.execute("COMMIT" if ok else "ROLLBACK")

    def put(self, ns: str, k: str, value: Any) -> None:
        with self.tx():
            self.db.execute(
                "INSERT INTO kv (ns, k, v) VALUES (?, ?, ?)"
                " ON CONFLICT (ns, k) DO UPDATE SET v = excluded.v",
                (ns, k, json.dumps(value, sort_keys=True)),
            )

    def put_raw(self, ns: str, k: str, text: str) -> None:
        """A row already serialized (the desk diffs its rows as text)."""
        with self.tx():
            self.db.execute(
                "INSERT INTO kv (ns, k, v) VALUES (?, ?, ?)"
                " ON CONFLICT (ns, k) DO UPDATE SET v = excluded.v",
                (ns, k, text),
            )

    def delete(self, ns: str, k: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM kv WHERE ns = ? AND k = ?", (ns, k))

    def get(self, ns: str, k: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE ns = ? AND k = ?", (ns, k)).fetchone()
        return json.loads(row[0]) if row else default

    def items(self, ns: str) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute("SELECT k, v FROM kv WHERE ns = ? ORDER BY k", (ns,)).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def close(self) -> None:
        with self.lock:
            self.db.close()
