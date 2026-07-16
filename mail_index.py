from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .mail_parser import ParsedMail


@dataclass(slots=True)
class MailboxState:
    account_id: str
    folder: str
    uidvalidity: int
    last_synced_uid: int
    last_sync_at: float
    last_reconcile_at: float


class MailHeaderIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mailboxes (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    last_synced_uid INTEGER NOT NULL DEFAULT 0,
                    last_sync_at REAL NOT NULL DEFAULT 0,
                    last_reconcile_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, folder)
                );

                CREATE TABLE IF NOT EXISTS mail_headers (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    from_name TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '',
                    reply_to TEXT NOT NULL DEFAULT '',
                    date_text TEXT NOT NULL DEFAULT '',
                    date_ts REAL NOT NULL DEFAULT 0,
                    has_attachments INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '',
                    references_text TEXT NOT NULL DEFAULT '',
                    remote_state TEXT NOT NULL DEFAULT 'active',
                    last_seen_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, folder, uidvalidity, uid)
                );

                CREATE INDEX IF NOT EXISTS idx_mail_headers_query
                ON mail_headers (
                    account_id, folder, uidvalidity, remote_state, date_ts DESC, uid DESC
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get_state(self, account_id: str, folder: str) -> MailboxState | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT account_id, folder, uidvalidity, last_synced_uid,
                       last_sync_at, last_reconcile_at
                FROM mailboxes WHERE account_id = ? AND folder = ?
                """,
                (account_id, folder),
            ).fetchone()
        if row is None:
            return None
        return MailboxState(
            account_id=str(row["account_id"]),
            folder=str(row["folder"]),
            uidvalidity=int(row["uidvalidity"]),
            last_synced_uid=int(row["last_synced_uid"]),
            last_sync_at=float(row["last_sync_at"]),
            last_reconcile_at=float(row["last_reconcile_at"]),
        )

    @staticmethod
    def _header_values(
        account_id: str,
        folder: str,
        uidvalidity: int,
        mail: ParsedMail,
        now: float,
    ) -> tuple:
        return (
            account_id,
            folder,
            int(uidvalidity),
            int(mail.uid),
            str(mail.subject or ""),
            str(mail.from_name or ""),
            str(mail.from_addr or ""),
            str(mail.reply_to or ""),
            str(mail.date or ""),
            float(mail.timestamp or 0),
            1 if mail.has_attachments else 0,
            str(mail.message_id or ""),
            str(mail.references or ""),
            "active",
            now,
        )

    def apply_sync(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        scanned_through_uid: int,
        headers: list[ParsedMail],
        remote_uids: set[int] | None = None,
    ) -> bool:
        now = time.time()
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT uidvalidity FROM mailboxes WHERE account_id = ? AND folder = ?",
                (account_id, folder),
            ).fetchone()
            changed = previous is not None and int(previous["uidvalidity"]) != int(
                uidvalidity
            )
            if changed:
                connection.execute(
                    """
                    UPDATE mail_headers SET remote_state = 'stale_uidvalidity'
                    WHERE account_id = ? AND folder = ? AND remote_state != 'stale_uidvalidity'
                    """,
                    (account_id, folder),
                )

            previous_synced = 0
            previous_reconcile = 0.0
            if previous is not None and not changed:
                state = connection.execute(
                    """
                    SELECT last_synced_uid, last_reconcile_at FROM mailboxes
                    WHERE account_id = ? AND folder = ?
                    """,
                    (account_id, folder),
                ).fetchone()
                if state is not None:
                    previous_synced = int(state["last_synced_uid"])
                    previous_reconcile = float(state["last_reconcile_at"])

            connection.execute(
                """
                INSERT INTO mailboxes (
                    account_id, folder, uidvalidity, last_synced_uid,
                    last_sync_at, last_reconcile_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, folder) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_synced_uid = excluded.last_synced_uid,
                    last_sync_at = excluded.last_sync_at,
                    last_reconcile_at = excluded.last_reconcile_at
                """,
                (
                    account_id,
                    folder,
                    int(uidvalidity),
                    max(previous_synced, int(scanned_through_uid)),
                    now,
                    now if remote_uids is not None else previous_reconcile,
                ),
            )

            if headers:
                connection.executemany(
                    """
                    INSERT INTO mail_headers (
                        account_id, folder, uidvalidity, uid, subject,
                        from_name, from_addr, reply_to, date_text, date_ts,
                        has_attachments, message_id, references_text,
                        remote_state, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, folder, uidvalidity, uid) DO UPDATE SET
                        subject = excluded.subject,
                        from_name = excluded.from_name,
                        from_addr = excluded.from_addr,
                        reply_to = excluded.reply_to,
                        date_text = excluded.date_text,
                        date_ts = excluded.date_ts,
                        has_attachments = excluded.has_attachments,
                        message_id = excluded.message_id,
                        references_text = excluded.references_text,
                        remote_state = 'active',
                        last_seen_at = excluded.last_seen_at
                    """,
                    [
                        self._header_values(
                            account_id, folder, uidvalidity, mail, now
                        )
                        for mail in headers
                    ],
                )

            if remote_uids is not None:
                rows = connection.execute(
                    """
                    SELECT uid FROM mail_headers
                    WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                    """,
                    (account_id, folder, int(uidvalidity)),
                ).fetchall()
                states = [
                    (
                        "active" if int(row["uid"]) in remote_uids else "remote_missing",
                        now,
                        account_id,
                        folder,
                        int(uidvalidity),
                        int(row["uid"]),
                    )
                    for row in rows
                ]
                if states:
                    connection.executemany(
                        """
                        UPDATE mail_headers SET remote_state = ?, last_seen_at = ?
                        WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                        """,
                        states,
                    )
        return changed

    def upsert_header(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        mail: ParsedMail,
    ) -> None:
        state = self.get_state(account_id, folder)
        scanned = max(int(mail.uid), state.last_synced_uid if state else 0)
        self.apply_sync(
            account_id,
            folder,
            uidvalidity,
            scanned,
            [mail],
        )

    def mark_remote_missing(
        self, account_id: str, folder: str, uidvalidity: int, uid: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE mail_headers
                SET remote_state = 'remote_missing', last_seen_at = ?
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (time.time(), account_id, folder, int(uidvalidity), int(uid)),
            )

    @staticmethod
    def _row_to_mail(row: sqlite3.Row) -> ParsedMail:
        return ParsedMail(
            uid=int(row["uid"]),
            subject=str(row["subject"]),
            from_name=str(row["from_name"]),
            from_addr=str(row["from_addr"]),
            reply_to=str(row["reply_to"]),
            date=str(row["date_text"]),
            timestamp=float(row["date_ts"]),
            body="",
            has_attachments=bool(row["has_attachments"]),
            message_id=str(row["message_id"]),
            references=str(row["references_text"]),
        )

    def query_since(
        self,
        account_id: str,
        folder: str,
        since: datetime,
        limit: int,
    ) -> list[ParsedMail]:
        state = self.get_state(account_id, folder)
        if state is None:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mail_headers
                WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                  AND remote_state = 'active' AND date_ts >= ?
                ORDER BY date_ts DESC, uid DESC LIMIT ?
                """,
                (
                    account_id,
                    folder,
                    state.uidvalidity,
                    since.timestamp(),
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [self._row_to_mail(row) for row in rows]

    def latest(
        self, account_id: str, folder: str, since: datetime | None = None
    ) -> ParsedMail | None:
        state = self.get_state(account_id, folder)
        if state is None:
            return None
        params: list[object] = [account_id, folder, state.uidvalidity]
        date_clause = ""
        if since is not None:
            date_clause = " AND date_ts >= ?"
            params.append(since.timestamp())
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM mail_headers
                WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                  AND remote_state = 'active'{date_clause}
                ORDER BY uid DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return self._row_to_mail(row) if row is not None else None

    def stats(self, account_id: str, folder: str) -> dict[str, int | float]:
        state = self.get_state(account_id, folder)
        if state is None:
            return {"active": 0, "remote_missing": 0, "last_sync_at": 0.0}
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT remote_state, COUNT(*) AS count FROM mail_headers
                WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                GROUP BY remote_state
                """,
                (account_id, folder, state.uidvalidity),
            ).fetchall()
        counts = {str(row["remote_state"]): int(row["count"]) for row in rows}
        return {
            "active": counts.get("active", 0),
            "remote_missing": counts.get("remote_missing", 0),
            "last_sync_at": state.last_sync_at,
        }
