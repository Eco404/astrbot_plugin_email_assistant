from __future__ import annotations

import sqlite3
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DatabaseVersionError(RuntimeError):
    pass


class MailDatabaseVersion:
    """Version gate for the plugin-owned SQLite schema."""

    CURRENT_VERSION = "2"
    VERSION_HISTORY = {
        "1": "稳定邮件实体、跨文件夹缓存与草稿存储",
        "2": "附件存储、草稿附件引用与完整正文缓存",
    }

    @classmethod
    def normalize_version(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text.startswith("v"):
            text = text[1:]
        parts = text.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            raise DatabaseVersionError(f"无效数据库版本号：{value}")
        normalized = [str(int(part)) for part in parts]
        while len(normalized) > 1 and normalized[-1] == "0":
            normalized.pop()
        return ".".join(normalized)

    @classmethod
    def storage_version(cls, value: Any) -> str:
        return f"v{cls.normalize_version(value)}"

    @staticmethod
    def _application_tables(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        return {str(row[0]) for row in rows}

    @classmethod
    def current_database_version(
        cls, connection: sqlite3.Connection
    ) -> str | None:
        if "db_version" not in cls._application_tables(connection):
            return None
        row = connection.execute(
            "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return cls.normalize_version(row[0])

    @classmethod
    def verify_or_prepare(cls, connection: sqlite3.Connection) -> bool:
        """Return True for a new database, reject unversioned or future schemas."""
        tables = cls._application_tables(connection)
        if not tables:
            return True
        if "db_version" not in tables:
            raise DatabaseVersionError(
                "检测到未受版本管理的旧版邮件索引数据库。"
                "请保留原数据库及备份，由对应版本导出数据后再升级。"
            )
        version = cls.current_database_version(connection)
        if version is None:
            raise DatabaseVersionError("邮件索引数据库缺少有效的版本记录。")
        if version == "1":
            cls.migrate_v1_to_v2(connection)
        elif version != cls.CURRENT_VERSION:
            raise DatabaseVersionError(
                f"邮件索引数据库版本为 v{version}，"
                f"当前插件需要 v{cls.CURRENT_VERSION}；请先执行对应迁移。"
            )
        return False

    @staticmethod
    def create_attachment_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_attachments (
                attachment_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                sha256 TEXT NOT NULL,
                source_key TEXT NOT NULL DEFAULT '',
                owner_umo TEXT NOT NULL DEFAULT '',
                owner_sender_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_attachments_access "
            "ON mail_attachments (last_accessed_at, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_attachments_source "
            "ON mail_attachments (account_id, source_key)"
        )

    @classmethod
    def migrate_v1_to_v2(cls, connection: sqlite3.Connection) -> None:
        """Back up the released schema, then commit the complete upgrade atomically."""
        database = next(
            (str(row[2]) for row in connection.execute("PRAGMA database_list")
             if row[1] == "main"),
            "",
        )
        if not database or connection.in_transaction:
            raise DatabaseVersionError("数据库迁移需要独立的磁盘数据库连接。")
        backup_path = Path(f"{database}.v1-backup-{uuid.uuid4().hex}.db")
        started_at = time.monotonic()
        try:
            descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            backup = sqlite3.connect(backup_path)
            try:
                connection.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DatabaseVersionError("升级前数据库备份校验失败。")
            finally:
                backup.close()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "ALTER TABLE mail_drafts ADD COLUMN attachment_ids_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
            cls.create_attachment_table(connection)
            connection.execute("DELETE FROM mail_bodies WHERE truncated != 0")
            cls.record_current_version(connection)
            connection.execute(
                "UPDATE db_version SET migration_duration_seconds = ? "
                "WHERE id = (SELECT MAX(id) FROM db_version)",
                (time.monotonic() - started_at,),
            )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise DatabaseVersionError(
                f"数据库 v1 → v2 升级失败，原数据库事务已回滚。"
                f"升级前备份位置：{backup_path}。原因：{exc}"
            ) from exc

    @classmethod
    def create_version_table(cls, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS db_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                migrated_at TEXT NOT NULL,
                migration_duration_seconds REAL NOT NULL DEFAULT 0
            )
            """
        )

    @classmethod
    def record_current_version(cls, connection: sqlite3.Connection) -> None:
        cls.create_version_table(connection)
        connection.execute(
            """
            INSERT INTO db_version (
                version, description, migrated_at, migration_duration_seconds
            ) VALUES (?, ?, ?, 0)
            """,
            (
                cls.storage_version(cls.CURRENT_VERSION),
                cls.VERSION_HISTORY[cls.CURRENT_VERSION],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
