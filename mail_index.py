from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .mail_parser import ParsedMail


def mail_content_hash(mail: ParsedMail) -> str:
    normalized = "\n".join(
        [
            str(mail.subject or ""),
            str(mail.from_name or ""),
            str(mail.from_addr or ""),
            str(mail.reply_to or ""),
            str(mail.date or ""),
            str(mail.body or ""),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class MailboxState:
    account_id: str
    folder: str
    uidvalidity: int
    last_synced_uid: int
    history_before_uid: int
    history_complete: bool
    last_sync_at: float
    last_reconcile_at: float


@dataclass(frozen=True, slots=True)
class IndexApplyResult:
    uidvalidity_changed: bool
    header_changes: int = 0
    remote_state_changes: int = 0
    history_state_changed: bool = False

    def __bool__(self) -> bool:
        return self.uidvalidity_changed


@dataclass(frozen=True, slots=True)
class CachedMailBody:
    account_id: str
    folder: str
    uidvalidity: int
    uid: int
    body_text: str
    content_hash: str
    size_bytes: int
    truncated: bool
    fetched_at: float


@dataclass(frozen=True, slots=True)
class IndexedMailFolder:
    account_id: str
    name: str
    display_name: str
    delimiter: str
    attributes: tuple[str, ...]
    selectable: bool
    special_use: str
    remote_state: str
    last_seen_at: float


@dataclass(frozen=True, slots=True)
class CachedMailAIResult:
    account_id: str
    folder: str
    uidvalidity: int
    uid: int
    content_hash: str
    task: str
    target_language: str
    result_text: str
    provider_id: str
    created_at: float


@dataclass(frozen=True, slots=True)
class MailDraft:
    draft_id: str
    account_id: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    bcc_addrs: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str
    reply_folder: str
    reply_uid: int | None
    reply_message_id: str
    source: str
    status: str
    revision: int
    created_at: float
    updated_at: float
    sent_at: float | None
    last_error: str


@dataclass(frozen=True, slots=True)
class IndexedMailHeader:
    account_id: str
    folder: str
    uidvalidity: int
    uid: int
    subject: str
    from_name: str
    from_addr: str
    reply_to: str
    date_text: str
    date_ts: float
    has_attachments: bool
    message_id: str
    references: str
    remote_state: str
    body_cached: bool
    body_truncated: bool
    body_fetched_at: float


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
                    history_before_uid INTEGER NOT NULL DEFAULT 0,
                    history_complete INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS mail_bodies (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    body_text TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    fetched_at REAL NOT NULL DEFAULT 0,
                    last_accessed_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, folder, uidvalidity, uid),
                    FOREIGN KEY (account_id, folder, uidvalidity, uid)
                        REFERENCES mail_headers (account_id, folder, uidvalidity, uid)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_mail_bodies_eviction
                ON mail_bodies (last_accessed_at, fetched_at);

                CREATE TABLE IF NOT EXISTS mail_folders (
                    account_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    delimiter TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '[]',
                    selectable INTEGER NOT NULL DEFAULT 1,
                    special_use TEXT NOT NULL DEFAULT '',
                    remote_state TEXT NOT NULL DEFAULT 'active',
                    last_seen_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_mail_folders_list
                ON mail_folders (account_id, remote_state, special_use, name);

                CREATE TABLE IF NOT EXISTS mail_ai_cache (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    task TEXT NOT NULL,
                    target_language TEXT NOT NULL DEFAULT '',
                    result_text TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    PRIMARY KEY (
                        account_id, folder, uidvalidity, uid,
                        content_hash, task, target_language
                    ),
                    FOREIGN KEY (account_id, folder, uidvalidity, uid)
                        REFERENCES mail_headers (account_id, folder, uidvalidity, uid)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_mail_ai_cache_access
                ON mail_ai_cache (last_accessed_at);

                CREATE TABLE IF NOT EXISTS mail_drafts (
                    draft_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    to_json TEXT NOT NULL DEFAULT '[]',
                    cc_json TEXT NOT NULL DEFAULT '[]',
                    bcc_json TEXT NOT NULL DEFAULT '[]',
                    subject TEXT NOT NULL DEFAULT '',
                    body_text TEXT NOT NULL DEFAULT '',
                    body_html TEXT NOT NULL DEFAULT '',
                    reply_folder TEXT NOT NULL DEFAULT '',
                    reply_uid INTEGER,
                    reply_message_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'user'
                        CHECK (source IN ('user', 'bot')),
                    status TEXT NOT NULL DEFAULT 'editing'
                        CHECK (status IN (
                            'editing', 'pending_review', 'approved', 'sent', 'failed'
                        )),
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL,
                    last_error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_mail_drafts_list
                ON mail_drafts (account_id, status, updated_at DESC);
                """
            )
            mailbox_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(mailboxes)")
            }
            if "history_before_uid" not in mailbox_columns:
                connection.execute(
                    "ALTER TABLE mailboxes ADD COLUMN history_before_uid "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "history_complete" not in mailbox_columns:
                connection.execute(
                    "ALTER TABLE mailboxes ADD COLUMN history_complete "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                UPDATE mailboxes
                SET history_before_uid = COALESCE(
                    (
                        SELECT MIN(headers.uid) FROM mail_headers AS headers
                        WHERE headers.account_id = mailboxes.account_id
                          AND headers.folder = mailboxes.folder
                          AND headers.uidvalidity = mailboxes.uidvalidity
                          AND headers.remote_state = 'active'
                    ),
                    last_synced_uid + 1
                )
                WHERE history_before_uid <= 0 AND history_complete = 0
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _row_to_folder(row: sqlite3.Row) -> IndexedMailFolder:
        try:
            attributes = json.loads(str(row["attributes_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            attributes = []
        return IndexedMailFolder(
            account_id=str(row["account_id"]),
            name=str(row["name"]),
            display_name=str(row["display_name"]),
            delimiter=str(row["delimiter"]),
            attributes=tuple(str(item) for item in attributes),
            selectable=bool(row["selectable"]),
            special_use=str(row["special_use"]),
            remote_state=str(row["remote_state"]),
            last_seen_at=float(row["last_seen_at"]),
        )

    def replace_folders(self, account_id: str, folders) -> int:
        now = time.time()
        rows = []
        for folder in folders:
            getter = folder.get if isinstance(folder, dict) else lambda key, default=None: getattr(folder, key, default)
            name = str(getter("name", "") or "").strip()
            if not name:
                continue
            rows.append(
                (
                    account_id,
                    name,
                    str(getter("display_name", name) or name),
                    str(getter("delimiter", "") or ""),
                    json.dumps(list(getter("attributes", ()) or ()), ensure_ascii=False),
                    1 if getter("selectable", True) else 0,
                    str(getter("special_use", "") or ""),
                    "active",
                    now,
                )
            )
        with self._connection() as connection:
            connection.execute(
                "UPDATE mail_folders SET remote_state = 'remote_missing' WHERE account_id = ?",
                (account_id,),
            )
            if rows:
                connection.executemany(
                    """
                    INSERT INTO mail_folders (
                        account_id, name, display_name, delimiter, attributes_json,
                        selectable, special_use, remote_state, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, name) DO UPDATE SET
                        display_name = excluded.display_name,
                        delimiter = excluded.delimiter,
                        attributes_json = excluded.attributes_json,
                        selectable = excluded.selectable,
                        special_use = excluded.special_use,
                        remote_state = 'active',
                        last_seen_at = excluded.last_seen_at
                    """,
                    rows,
                )
        return len(rows)

    def list_folders(
        self, account_id: str, *, active_only: bool = True
    ) -> list[IndexedMailFolder]:
        where = " AND remote_state = 'active'" if active_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM mail_folders
                WHERE account_id = ?{where}
                ORDER BY CASE special_use WHEN 'inbox' THEN 0 ELSE 1 END,
                         name COLLATE NOCASE
                """,
                (account_id,),
            ).fetchall()
        return [self._row_to_folder(row) for row in rows]

    def get_state(self, account_id: str, folder: str) -> MailboxState | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT account_id, folder, uidvalidity, last_synced_uid,
                       history_before_uid, history_complete,
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
            history_before_uid=int(row["history_before_uid"]),
            history_complete=bool(row["history_complete"]),
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
        history_before_uid: int | None = None,
        history_complete: bool | None = None,
    ) -> IndexApplyResult:
        now = time.time()
        header_changes = 0
        remote_state_changes = 0
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
                connection.execute(
                    """
                    DELETE FROM mail_bodies
                    WHERE account_id = ? AND folder = ? AND uidvalidity != ?
                    """,
                    (account_id, folder, int(uidvalidity)),
                )
                connection.execute(
                    """
                    DELETE FROM mail_ai_cache
                    WHERE account_id = ? AND folder = ? AND uidvalidity != ?
                    """,
                    (account_id, folder, int(uidvalidity)),
                )

            previous_synced = 0
            previous_history_before = 0
            previous_history_complete = False
            previous_reconcile = 0.0
            if previous is not None and not changed:
                state = connection.execute(
                    """
                    SELECT last_synced_uid, history_before_uid,
                           history_complete, last_reconcile_at FROM mailboxes
                    WHERE account_id = ? AND folder = ?
                    """,
                    (account_id, folder),
                ).fetchone()
                if state is not None:
                    previous_synced = int(state["last_synced_uid"])
                    previous_history_before = int(state["history_before_uid"])
                    previous_history_complete = bool(state["history_complete"])
                    previous_reconcile = float(state["last_reconcile_at"])

            next_history_before = (
                max(0, int(history_before_uid))
                if history_before_uid is not None
                else previous_history_before
            )
            next_history_complete = (
                bool(history_complete)
                if history_complete is not None
                else previous_history_complete
            )
            history_state_changed = (
                previous is None
                or changed
                or next_history_before != previous_history_before
                or next_history_complete != previous_history_complete
            )

            connection.execute(
                """
                INSERT INTO mailboxes (
                    account_id, folder, uidvalidity, last_synced_uid,
                    history_before_uid, history_complete,
                    last_sync_at, last_reconcile_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, folder) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_synced_uid = excluded.last_synced_uid,
                    history_before_uid = excluded.history_before_uid,
                    history_complete = excluded.history_complete,
                    last_sync_at = excluded.last_sync_at,
                    last_reconcile_at = excluded.last_reconcile_at
                """,
                (
                    account_id,
                    folder,
                    int(uidvalidity),
                    max(previous_synced, int(scanned_through_uid)),
                    next_history_before,
                    1 if next_history_complete else 0,
                    now,
                    now if remote_uids is not None else previous_reconcile,
                ),
            )

            if headers:
                header_values = [
                    self._header_values(account_id, folder, uidvalidity, mail, now)
                    for mail in headers
                ]
                changed_header_uids: list[int] = []
                for values in header_values:
                    existing = connection.execute(
                        """
                        SELECT subject, from_name, from_addr, reply_to, date_text,
                               date_ts, has_attachments, message_id,
                               references_text, remote_state
                        FROM mail_headers
                        WHERE account_id = ? AND folder = ?
                          AND uidvalidity = ? AND uid = ?
                        """,
                        values[:4],
                    ).fetchone()
                    expected = values[4:14]
                    if existing is None or tuple(existing) != expected:
                        header_changes += 1
                        if existing is not None:
                            changed_header_uids.append(int(values[3]))
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
                    header_values,
                )
                if changed_header_uids:
                    placeholders = ",".join("?" for _ in changed_header_uids)
                    connection.execute(
                        f"""
                        DELETE FROM mail_ai_cache
                        WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                          AND uid IN ({placeholders})
                        """,
                        (
                            account_id,
                            folder,
                            int(uidvalidity),
                            *changed_header_uids,
                        ),
                    )

            if remote_uids is not None:
                rows = connection.execute(
                    """
                    SELECT uid, remote_state FROM mail_headers
                    WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                    """,
                    (account_id, folder, int(uidvalidity)),
                ).fetchall()
                states = []
                for row in rows:
                    target_state = (
                        "active"
                        if int(row["uid"]) in remote_uids
                        else "remote_missing"
                    )
                    if str(row["remote_state"]) == target_state:
                        continue
                    states.append(
                        (
                            target_state,
                            now,
                            account_id,
                            folder,
                            int(uidvalidity),
                            int(row["uid"]),
                        )
                    )
                remote_state_changes = len(states)
                if states:
                    connection.executemany(
                        """
                        UPDATE mail_headers SET remote_state = ?, last_seen_at = ?
                        WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                        """,
                        states,
                    )
        return IndexApplyResult(
            changed,
            header_changes,
            remote_state_changes,
            history_state_changed,
        )

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
    def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, int, bool]:
        raw = str(value or "").encode("utf-8")
        limit = max(1, int(max_bytes))
        if len(raw) <= limit:
            return str(value or ""), len(raw), False
        truncated = raw[:limit].decode("utf-8", errors="ignore")
        return truncated, len(truncated.encode("utf-8")), True

    def cache_body(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        body_text: str,
        max_item_bytes: int,
    ) -> CachedMailBody:
        original = str(body_text or "")
        content_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        cached_text, size_bytes, truncated = self._truncate_utf8(
            original, max_item_bytes
        )
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO mail_bodies (
                    account_id, folder, uidvalidity, uid, body_text,
                    content_hash, size_bytes, truncated,
                    fetched_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, folder, uidvalidity, uid) DO UPDATE SET
                    body_text = excluded.body_text,
                    content_hash = excluded.content_hash,
                    size_bytes = excluded.size_bytes,
                    truncated = excluded.truncated,
                    fetched_at = excluded.fetched_at,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (
                    account_id,
                    folder,
                    int(uidvalidity),
                    int(uid),
                    cached_text,
                    content_hash,
                    size_bytes,
                    1 if truncated else 0,
                    now,
                    now,
                ),
            )
        return CachedMailBody(
            account_id,
            folder,
            int(uidvalidity),
            int(uid),
            cached_text,
            content_hash,
            size_bytes,
            truncated,
            now,
        )

    def get_cached_body(
        self, account_id: str, folder: str, uidvalidity: int, uid: int
    ) -> CachedMailBody | None:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM mail_bodies
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (account_id, folder, int(uidvalidity), int(uid)),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE mail_bodies SET last_accessed_at = ?
                    WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                    """,
                    (now, account_id, folder, int(uidvalidity), int(uid)),
                )
        if row is None:
            return None
        return CachedMailBody(
            account_id=str(row["account_id"]),
            folder=str(row["folder"]),
            uidvalidity=int(row["uidvalidity"]),
            uid=int(row["uid"]),
            body_text=str(row["body_text"]),
            content_hash=str(row["content_hash"]),
            size_bytes=int(row["size_bytes"]),
            truncated=bool(row["truncated"]),
            fetched_at=float(row["fetched_at"]),
        )

    def get_header(
        self,
        account_id: str,
        folder: str,
        uid: int,
        *,
        active_only: bool = True,
    ) -> IndexedMailHeader | None:
        state = self.get_state(account_id, folder)
        if state is None:
            return None
        active_clause = " AND headers.remote_state = 'active'" if active_only else ""
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT headers.*,
                       CASE WHEN bodies.uid IS NULL THEN 0 ELSE 1 END AS body_cached,
                       COALESCE(bodies.truncated, 0) AS body_truncated,
                       COALESCE(bodies.fetched_at, 0) AS body_fetched_at
                FROM mail_headers AS headers
                LEFT JOIN mail_bodies AS bodies
                  ON bodies.account_id = headers.account_id
                 AND bodies.folder = headers.folder
                 AND bodies.uidvalidity = headers.uidvalidity
                 AND bodies.uid = headers.uid
                WHERE headers.account_id = ? AND headers.folder = ?
                  AND headers.uidvalidity = ? AND headers.uid = ?{active_clause}
                """,
                (account_id, folder, state.uidvalidity, int(uid)),
            ).fetchone()
        return self._row_to_indexed_header(row) if row is not None else None

    def delete_cached_body(
        self, account_id: str, folder: str, uidvalidity: int, uid: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM mail_bodies
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (account_id, folder, int(uidvalidity), int(uid)),
            )

    def get_ai_result(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        content_hash: str,
        task: str,
        target_language: str,
    ) -> CachedMailAIResult | None:
        now = time.time()
        key = (
            account_id,
            folder,
            int(uidvalidity),
            int(uid),
            str(content_hash),
            str(task),
            str(target_language),
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM mail_ai_cache
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                  AND content_hash = ? AND task = ? AND target_language = ?
                """,
                key,
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE mail_ai_cache SET last_accessed_at = ?
                    WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                      AND content_hash = ? AND task = ? AND target_language = ?
                    """,
                    (now, *key),
                )
        if row is None:
            return None
        return CachedMailAIResult(
            account_id=str(row["account_id"]),
            folder=str(row["folder"]),
            uidvalidity=int(row["uidvalidity"]),
            uid=int(row["uid"]),
            content_hash=str(row["content_hash"]),
            task=str(row["task"]),
            target_language=str(row["target_language"]),
            result_text=str(row["result_text"]),
            provider_id=str(row["provider_id"]),
            created_at=float(row["created_at"]),
        )

    def cache_ai_result(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        content_hash: str,
        task: str,
        target_language: str,
        result_text: str,
        provider_id: str,
    ) -> CachedMailAIResult:
        now = time.time()
        values = (
            account_id,
            folder,
            int(uidvalidity),
            int(uid),
            str(content_hash),
            str(task),
            str(target_language),
            str(result_text),
            str(provider_id),
            now,
            now,
        )
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM mail_ai_cache
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                  AND task LIKE ? AND target_language = ?
                  AND (task != ? OR content_hash != ?)
                """,
                (
                    account_id,
                    folder,
                    int(uidvalidity),
                    int(uid),
                    str(task).split(":", 1)[0] + ":%",
                    str(target_language),
                    str(task),
                    str(content_hash),
                ),
            )
            connection.execute(
                """
                INSERT INTO mail_ai_cache (
                    account_id, folder, uidvalidity, uid, content_hash,
                    task, target_language, result_text, provider_id,
                    created_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    account_id, folder, uidvalidity, uid,
                    content_hash, task, target_language
                ) DO UPDATE SET
                    result_text = excluded.result_text,
                    provider_id = excluded.provider_id,
                    created_at = excluded.created_at,
                    last_accessed_at = excluded.last_accessed_at
                """,
                values,
            )
        return CachedMailAIResult(*values[:-1])

    def delete_ai_results(
        self, account_id: str, folder: str, uidvalidity: int, uid: int
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM mail_ai_cache
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (account_id, folder, int(uidvalidity), int(uid)),
            )
            return max(0, int(cursor.rowcount))

    def purge_stale_ai_results(
        self,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        current_content_hash: str,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM mail_ai_cache
                WHERE account_id = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                  AND content_hash != ?
                """,
                (
                    account_id,
                    folder,
                    int(uidvalidity),
                    int(uid),
                    str(current_content_hash),
                ),
            )
            return max(0, int(cursor.rowcount))

    def clear_body_cache(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM mail_bodies")
            return max(0, int(cursor.rowcount))

    def purge_remote_missing_bodies(self, account_id: str, folder: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM mail_bodies
                WHERE account_id = ? AND folder = ? AND EXISTS (
                    SELECT 1 FROM mail_headers AS headers
                    WHERE headers.account_id = mail_bodies.account_id
                      AND headers.folder = mail_bodies.folder
                      AND headers.uidvalidity = mail_bodies.uidvalidity
                      AND headers.uid = mail_bodies.uid
                      AND headers.remote_state != 'active'
                )
                """,
                (account_id, folder),
            )
            return max(0, int(cursor.rowcount))

    def purge_remote_missing_ai_results(self, account_id: str, folder: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM mail_ai_cache
                WHERE account_id = ? AND folder = ? AND EXISTS (
                    SELECT 1 FROM mail_headers AS headers
                    WHERE headers.account_id = mail_ai_cache.account_id
                      AND headers.folder = mail_ai_cache.folder
                      AND headers.uidvalidity = mail_ai_cache.uidvalidity
                      AND headers.uid = mail_ai_cache.uid
                      AND headers.remote_state != 'active'
                )
                """,
                (account_id, folder),
            )
            return max(0, int(cursor.rowcount))

    def prune_body_cache(self, retention_days: int, max_total_bytes: int) -> int:
        now = time.time()
        removed = 0
        with self._connection() as connection:
            if int(retention_days) > 0:
                cutoff = now - int(retention_days) * 86400
                cursor = connection.execute(
                    "DELETE FROM mail_bodies WHERE last_accessed_at < ?",
                    (cutoff,),
                )
                removed += max(0, int(cursor.rowcount))
            budget = max(0, int(max_total_bytes))
            total = int(
                connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM mail_bodies"
                ).fetchone()[0]
            )
            if budget and total > budget:
                rows = connection.execute(
                    """
                    SELECT account_id, folder, uidvalidity, uid, size_bytes
                    FROM mail_bodies
                    ORDER BY last_accessed_at ASC, fetched_at ASC
                    """
                ).fetchall()
                for row in rows:
                    if total <= budget:
                        break
                    connection.execute(
                        """
                        DELETE FROM mail_bodies
                        WHERE account_id = ? AND folder = ?
                          AND uidvalidity = ? AND uid = ?
                        """,
                        (
                            row["account_id"],
                            row["folder"],
                            row["uidvalidity"],
                            row["uid"],
                        ),
                    )
                    total -= int(row["size_bytes"])
                    removed += 1
        return removed

    @staticmethod
    def _normalize_addresses(values) -> tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, str):
            values = [values]
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _decode_addresses(value: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(str(item) for item in decoded if str(item).strip())

    @classmethod
    def _row_to_draft(cls, row: sqlite3.Row) -> MailDraft:
        return MailDraft(
            draft_id=str(row["draft_id"]),
            account_id=str(row["account_id"]),
            to_addrs=cls._decode_addresses(str(row["to_json"])),
            cc_addrs=cls._decode_addresses(str(row["cc_json"])),
            bcc_addrs=cls._decode_addresses(str(row["bcc_json"])),
            subject=str(row["subject"]),
            body_text=str(row["body_text"]),
            body_html=str(row["body_html"]),
            reply_folder=str(row["reply_folder"]),
            reply_uid=(int(row["reply_uid"]) if row["reply_uid"] is not None else None),
            reply_message_id=str(row["reply_message_id"]),
            source=str(row["source"]),
            status=str(row["status"]),
            revision=int(row["revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            sent_at=(float(row["sent_at"]) if row["sent_at"] is not None else None),
            last_error=str(row["last_error"]),
        )

    def create_draft(
        self,
        account_id: str,
        *,
        to_addrs=(),
        cc_addrs=(),
        bcc_addrs=(),
        subject: str = "",
        body_text: str = "",
        body_html: str = "",
        reply_folder: str = "",
        reply_uid: int | None = None,
        reply_message_id: str = "",
        source: str = "user",
        status: str = "editing",
    ) -> MailDraft:
        allowed_statuses = {"editing", "pending_review", "approved", "sent", "failed"}
        if status not in allowed_statuses:
            raise ValueError("无效的草稿状态。")
        if source not in {"user", "bot"}:
            raise ValueError("草稿来源只能是 user 或 bot。")
        draft_id = uuid.uuid4().hex
        now = time.time()
        normalized_to = self._normalize_addresses(to_addrs)
        normalized_cc = self._normalize_addresses(cc_addrs)
        normalized_bcc = self._normalize_addresses(bcc_addrs)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO mail_drafts (
                    draft_id, account_id, to_json, cc_json, bcc_json,
                    subject, body_text, body_html, reply_folder, reply_uid,
                    reply_message_id, source, status, revision,
                    created_at, updated_at, sent_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, '')
                """,
                (
                    draft_id,
                    str(account_id),
                    json.dumps(normalized_to, ensure_ascii=False),
                    json.dumps(normalized_cc, ensure_ascii=False),
                    json.dumps(normalized_bcc, ensure_ascii=False),
                    str(subject or ""),
                    str(body_text or ""),
                    str(body_html or ""),
                    str(reply_folder or ""),
                    int(reply_uid) if reply_uid is not None else None,
                    str(reply_message_id or ""),
                    source,
                    status,
                    now,
                    now,
                ),
            )
        draft = self.get_draft(draft_id)
        if draft is None:
            raise RuntimeError("草稿创建后无法读取。")
        return draft

    def get_draft(self, draft_id: str) -> MailDraft | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mail_drafts WHERE draft_id = ?",
                (str(draft_id),),
            ).fetchone()
        return self._row_to_draft(row) if row is not None else None

    def list_drafts(
        self,
        account_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[MailDraft]:
        clauses = []
        params: list[object] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(str(account_id))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM mail_drafts {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def update_draft(
        self,
        draft_id: str,
        expected_revision: int,
        **changes,
    ) -> MailDraft:
        allowed = {
            "to_addrs",
            "cc_addrs",
            "bcc_addrs",
            "subject",
            "body_text",
            "body_html",
            "reply_folder",
            "reply_uid",
            "reply_message_id",
            "status",
            "last_error",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不支持的草稿字段：{', '.join(sorted(unknown))}")
        if changes.get("status") not in {
            None,
            "editing",
            "pending_review",
            "approved",
            "sent",
            "failed",
        }:
            raise ValueError("无效的草稿状态。")
        column_map = {
            "to_addrs": "to_json",
            "cc_addrs": "cc_json",
            "bcc_addrs": "bcc_json",
        }
        assignments = []
        params: list[object] = []
        for key, value in changes.items():
            column = column_map.get(key, key)
            if key in column_map:
                value = json.dumps(
                    self._normalize_addresses(value), ensure_ascii=False
                )
            elif key == "reply_uid":
                value = int(value) if value is not None else None
            else:
                value = str(value or "")
            assignments.append(f"{column} = ?")
            params.append(value)
        now = time.time()
        assignments.extend(["revision = revision + 1", "updated_at = ?"])
        params.append(now)
        if changes.get("status") == "sent":
            assignments.append("sent_at = ?")
            params.append(now)
        params.extend([str(draft_id), int(expected_revision)])
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE mail_drafts SET {', '.join(assignments)}
                WHERE draft_id = ? AND revision = ?
                """,
                params,
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM mail_drafts WHERE draft_id = ?",
                    (str(draft_id),),
                ).fetchone()
                if exists is None:
                    raise KeyError("草稿不存在。")
                raise RuntimeError("草稿已被其他操作修改，请刷新后重试。")
        updated = self.get_draft(draft_id)
        if updated is None:
            raise KeyError("草稿不存在。")
        return updated

    def delete_draft(self, draft_id: str, expected_revision: int | None = None) -> bool:
        params: list[object] = [str(draft_id)]
        revision_clause = ""
        if expected_revision is not None:
            revision_clause = " AND revision = ?"
            params.append(int(expected_revision))
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM mail_drafts WHERE draft_id = ?{revision_clause}",
                params,
            )
            return cursor.rowcount == 1

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

    @staticmethod
    def _row_to_indexed_header(row: sqlite3.Row) -> IndexedMailHeader:
        return IndexedMailHeader(
            account_id=str(row["account_id"]),
            folder=str(row["folder"]),
            uidvalidity=int(row["uidvalidity"]),
            uid=int(row["uid"]),
            subject=str(row["subject"]),
            from_name=str(row["from_name"]),
            from_addr=str(row["from_addr"]),
            reply_to=str(row["reply_to"]),
            date_text=str(row["date_text"]),
            date_ts=float(row["date_ts"]),
            has_attachments=bool(row["has_attachments"]),
            message_id=str(row["message_id"]),
            references=str(row["references_text"]),
            remote_state=str(row["remote_state"]),
            body_cached=bool(row["body_cached"]),
            body_truncated=bool(row["body_truncated"]),
            body_fetched_at=float(row["body_fetched_at"]),
        )

    def list_headers_page(
        self,
        account_id: str,
        folder: str,
        *,
        limit: int = 50,
        keyword: str = "",
        since_ts: float | None = None,
        before_date_ts: float | None = None,
        before_uid: int | None = None,
    ) -> tuple[list[IndexedMailHeader], bool]:
        """Return one keyset-paginated page from the current mailbox generation."""
        state = self.get_state(account_id, folder)
        if state is None:
            return [], False
        page_limit = max(1, min(int(limit), 100))
        clauses = [
            "headers.account_id = ?",
            "headers.folder = ?",
            "headers.uidvalidity = ?",
            "headers.remote_state = 'active'",
        ]
        params: list[object] = [account_id, folder, state.uidvalidity]
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            escaped = (
                normalized_keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            clauses.append(
                "(headers.subject LIKE ? ESCAPE '\\' "
                "OR headers.from_name LIKE ? ESCAPE '\\' "
                "OR headers.from_addr LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        if since_ts is not None:
            clauses.append("headers.date_ts >= ?")
            params.append(float(since_ts))
        if before_date_ts is not None and before_uid is not None:
            clauses.append(
                "(headers.date_ts < ? OR "
                "(headers.date_ts = ? AND headers.uid < ?))"
            )
            params.extend([float(before_date_ts), float(before_date_ts), int(before_uid)])
        params.append(page_limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT headers.*,
                       CASE WHEN bodies.uid IS NULL THEN 0 ELSE 1 END AS body_cached,
                       COALESCE(bodies.truncated, 0) AS body_truncated,
                       COALESCE(bodies.fetched_at, 0) AS body_fetched_at
                FROM mail_headers AS headers
                LEFT JOIN mail_bodies AS bodies
                  ON bodies.account_id = headers.account_id
                 AND bodies.folder = headers.folder
                 AND bodies.uidvalidity = headers.uidvalidity
                 AND bodies.uid = headers.uid
                WHERE {' AND '.join(clauses)}
                ORDER BY headers.date_ts DESC, headers.uid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        has_more = len(rows) > page_limit
        return [self._row_to_indexed_header(row) for row in rows[:page_limit]], has_more

    def stats(self, account_id: str, folder: str) -> dict[str, int | float]:
        state = self.get_state(account_id, folder)
        if state is None:
            return {
                "active": 0,
                "remote_missing": 0,
                "cached_bodies": 0,
                "cached_body_bytes": 0,
                "history_complete": False,
                "history_before_uid": 0,
                "last_sync_at": 0.0,
            }
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT remote_state, COUNT(*) AS count FROM mail_headers
                WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                GROUP BY remote_state
                """,
                (account_id, folder, state.uidvalidity),
            ).fetchall()
            body_stats = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes
                FROM mail_bodies
                WHERE account_id = ? AND folder = ? AND uidvalidity = ?
                """,
                (account_id, folder, state.uidvalidity),
            ).fetchone()
        counts = {str(row["remote_state"]): int(row["count"]) for row in rows}
        return {
            "active": counts.get("active", 0),
            "remote_missing": counts.get("remote_missing", 0),
            "cached_bodies": int(body_stats["count"]),
            "cached_body_bytes": int(body_stats["bytes"]),
            "history_complete": state.history_complete,
            "history_before_uid": state.history_before_uid,
            "last_sync_at": state.last_sync_at,
        }
