"""SQLite WAL-backed task lease store with strictly monotonic fencing tokens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class LeaseRecord:
    """Snapshot of a task lease and its current monotonic fencing token."""

    task_id: str
    owner_node: str
    fencing_token: int
    lease_expires_at: float
    updated_at: float


class LeaseStore:
    """Atomic lease coordinator preventing split-brain agent writes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY,
                    owner_node TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> LeaseRecord:
        return LeaseRecord(
            task_id=str(row["task_id"]),
            owner_node=str(row["owner_node"]),
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=float(row["lease_expires_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get(self, task_id: str) -> LeaseRecord | None:
        """Return the current lease record for a task, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_record(row) if row is not None else None

    def acquire(
        self,
        task_id: str,
        node_id: str,
        ttl_seconds: float = 60.0,
        now: float | None = None,
    ) -> LeaseRecord | None:
        """Acquire or reclaim a task lease with atomic monotonic fencing.

        Returns None if another node currently holds an unexpired lease.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = time.time() if now is None else float(now)
        expires_at = ts + float(ttl_seconds)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()

            if row is None:
                token = 1
                conn.execute(
                    """
                    INSERT INTO leases (
                        task_id, owner_node, fencing_token, lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (task_id, node_id, token, expires_at, ts),
                )
            else:
                current = self._row_to_record(row)
                if current.lease_expires_at > ts:
                    if current.owner_node != node_id:
                        conn.execute("ROLLBACK")
                        return None
                    # Idempotent extension by the active owner keeps the same epoch.
                    token = current.fencing_token
                    conn.execute(
                        """
                        UPDATE leases
                        SET lease_expires_at = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (expires_at, ts, task_id),
                    )
                else:
                    # Expired lease reclaimed: bump fencing token monotonically.
                    token = current.fencing_token + 1
                    conn.execute(
                        """
                        UPDATE leases
                        SET owner_node = ?, fencing_token = ?,
                            lease_expires_at = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (node_id, token, expires_at, ts, task_id),
                    )

            conn.execute("COMMIT")
            return LeaseRecord(
                task_id=task_id,
                owner_node=node_id,
                fencing_token=token,
                lease_expires_at=expires_at,
                updated_at=ts,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def renew(
        self,
        task_id: str,
        node_id: str,
        fencing_token: int,
        ttl_seconds: float = 60.0,
        now: float | None = None,
    ) -> LeaseRecord | None:
        """Extend an active lease only if owner and fencing token match."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = time.time() if now is None else float(now)
        expires_at = ts + float(ttl_seconds)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None

            current = self._row_to_record(row)
            if (
                current.owner_node != node_id
                or current.fencing_token != fencing_token
                or current.lease_expires_at <= ts
            ):
                conn.execute("ROLLBACK")
                return None

            conn.execute(
                """
                UPDATE leases
                SET lease_expires_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (expires_at, ts, task_id),
            )
            conn.execute("COMMIT")
            return LeaseRecord(
                task_id=task_id,
                owner_node=node_id,
                fencing_token=fencing_token,
                lease_expires_at=expires_at,
                updated_at=ts,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def verify_fencing_token(
        self,
        task_id: str,
        fencing_token: int,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Verify that a push or mutation carries the active, unexpired epoch."""
        ts = time.time() if now is None else float(now)
        record = self.get(task_id)
        if record is None:
            return False, f"no lease found for task '{task_id}'"
        if fencing_token != record.fencing_token:
            return (
                False,
                f"stale fencing token {fencing_token} != active {record.fencing_token} "
                f"for task '{task_id}'",
            )
        if record.lease_expires_at <= ts:
            return (
                False,
                f"expired lease (epoch {record.fencing_token}) for task '{task_id}'",
            )
        return True, "ok"
