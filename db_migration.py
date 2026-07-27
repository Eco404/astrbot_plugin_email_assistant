from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


class DatabaseVersionError(RuntimeError):
    pass


class MailDatabaseVersion:
    """Version gate for the plugin-owned SQLite schema."""

    CURRENT_VERSION = "1"
    VERSION_HISTORY = {
        "1": "稳定邮件实体、跨文件夹缓存与草稿存储",
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
                "本版本不提供旧库迁移，请删除 mail_headers.db 后重新启动插件。"
            )
        version = cls.current_database_version(connection)
        if version is None:
            raise DatabaseVersionError("邮件索引数据库缺少有效的版本记录。")
        if version != cls.CURRENT_VERSION:
            raise DatabaseVersionError(
                f"邮件索引数据库版本为 v{version}，"
                f"当前插件需要 v{cls.CURRENT_VERSION}；请先执行对应迁移。"
            )
        return False

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
