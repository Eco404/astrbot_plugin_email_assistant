from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import stat
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .mail_index import MailHeaderIndex, attachment_storage_lock


class AttachmentQuotaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    attachment_id: str
    account_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    owner_umo: str
    owner_sender_id: str
    created_at: float
    last_accessed_at: float
    path: Path
    source_key: str = ""

    def payload(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "account_id": self.account_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
        }


_LEASES: dict[str, Counter] = {}
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
_TEMPORARY = re.compile(r"\.(?:partial|deleted)-[0-9a-f]{32}\Z")


class AttachmentStore:
    """Manage immutable attachment files within a shared disk quota."""

    def __init__(
        self, index: MailHeaderIndex, *, max_total_bytes: int, retention_days: int,
    ) -> None:
        self.index = index
        self.max_total_bytes = max(0, int(max_total_bytes))
        self.retention_days = max(0, int(retention_days))
        self.root = index.path.parent / "attachments"
        self._lock = attachment_storage_lock(index.path)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink():
                raise ValueError("附件目录不能是符号链接。")
            os.chmod(self.root, 0o700)
            self._leases = _LEASES.setdefault(str(index.path.resolve()), Counter())
            self._recover_orphans()

    def _path(self, attachment_id: str) -> Path:
        if not _IDENTIFIER.fullmatch(str(attachment_id)):
            raise ValueError("附件标识无效。")
        return self.root / str(attachment_id)

    def _record(self, row: sqlite3.Row) -> AttachmentRecord:
        return AttachmentRecord(
            **{key: row[key] for key in (
                "attachment_id", "account_id", "filename", "content_type", "size_bytes",
                "sha256", "owner_umo", "owner_sender_id", "created_at", "last_accessed_at",
                "source_key",
            )},
            path=self._path(row["attachment_id"]),
        )

    def _rows(self) -> list[sqlite3.Row]:
        with self.index._connection() as connection:
            return connection.execute(
                "SELECT * FROM mail_attachments ORDER BY last_accessed_at, created_at"
            ).fetchall()

    def _recover_orphans(self) -> None:
        known = {row["attachment_id"] for row in self._rows()}
        for path in self.root.iterdir():
            if path.name.startswith(".deleted-") and _TEMPORARY.fullmatch(path.name):
                attachment_id = path.name[len(".deleted-"):]
                original = self._path(attachment_id)
                if attachment_id in known:
                    if not original.exists():
                        path.rename(original)
                    continue
            if (
                _TEMPORARY.fullmatch(path.name)
                or (_IDENTIFIER.fullmatch(path.name) and path.name not in known)
            ) and not path.is_dir():
                path.unlink(missing_ok=True)

    def _disk_bytes(self) -> int:
        return sum(
            path.lstat().st_size for path in self.root.iterdir()
            if not path.is_dir()
        )

    def _sync_directory(self) -> None:
        if os.name == "posix":
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _protected(self) -> set[str]:
        protected = {key for key, count in self._leases.items() if count > 0}
        with self.index._connection() as connection:
            rows = connection.execute(
                "SELECT attachment_ids_json FROM mail_drafts "
                "WHERE status NOT IN ('sent', 'cancelled')"
            ).fetchall()
        for row in rows:
            try:
                ids = json.loads(row[0])
                if not isinstance(ids, list):
                    raise ValueError("invalid attachment list")
            except (TypeError, ValueError):
                # Preserve files if a draft reference cannot be interpreted safely.
                return {item["attachment_id"] for item in self._rows()}
            protected.update(str(value) for value in ids)
        return protected

    def _remove(self, row: sqlite3.Row) -> None:
        path = self._path(row["attachment_id"])
        tomb = self.root / f".deleted-{row['attachment_id']}"
        moved = False
        try:
            if path.exists() or path.is_symlink():
                path.rename(tomb)
                moved = True
            with self.index._connection() as connection:
                connection.execute(
                    "DELETE FROM mail_attachments WHERE attachment_id = ?",
                    (row["attachment_id"],),
                )
        except Exception:
            if moved:
                tomb.rename(path)
            raise
        if moved:
            tomb.unlink()

    def _make_room(self, required_bytes: int = 0, *, expire: bool = True) -> int:
        required_bytes = max(0, int(required_bytes))
        if required_bytes > self.max_total_bytes:
            raise AttachmentQuotaError("附件超出附件缓存总容量，请调整容量或选择更小的文件。")
        protected = self._protected()
        cutoff = time.time() - self.retention_days * 86400
        rows = self._rows()
        eligible = [row for row in rows if row["attachment_id"] not in protected]
        usage = self._disk_bytes()
        reclaimable = sum(int(row["size_bytes"]) for row in eligible)
        if usage + required_bytes - reclaimable > self.max_total_bytes:
            raise AttachmentQuotaError("附件缓存容量不足，草稿引用或正在使用的附件不能清理。")
        removed = 0
        for row in eligible:
            expired = expire and float(row["last_accessed_at"]) < cutoff
            if expired or usage + required_bytes > self.max_total_bytes:
                self._remove(row)
                usage = self._disk_bytes()
                removed += 1
        return removed

    def prune(self) -> int:
        with self._lock:
            self._recover_orphans()
            return self._make_room()

    def stats(self) -> dict:
        with self._lock:
            rows = self._rows()
            protected = self._protected()
            return {
                "attachment_count": len(rows),
                "total_bytes": self._disk_bytes(),
                "max_total_bytes": self.max_total_bytes,
                "retention_days": self.retention_days,
                "protected_bytes": sum(
                    int(row["size_bytes"]) for row in rows
                    if row["attachment_id"] in protected
                ),
            }

    @staticmethod
    def _integrity(record: AttachmentRecord) -> None:
        if record.path.is_symlink() or not record.path.is_file():
            raise ValueError("附件文件缺失，请重新添加或下载。")
        digest = hashlib.sha256()
        size = 0
        with record.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != record.size_bytes or digest.hexdigest() != record.sha256:
            raise ValueError("附件文件完整性校验失败，请重新添加或下载。")

    def get(self, attachment_id: str, account_id: str) -> AttachmentRecord:
        with self._lock, self.index._connection() as connection:
            self._path(attachment_id)
            row = connection.execute(
                "SELECT * FROM mail_attachments WHERE attachment_id = ? AND account_id = ?",
                (str(attachment_id), str(account_id)),
            ).fetchone()
            if row is None:
                raise ValueError("附件不存在、已过期或不属于当前邮箱。")
            record = self._record(row)
            if record.path.is_symlink() or not record.path.is_file():
                raise ValueError("附件文件缺失，请重新添加或下载。")
            now = time.time()
            connection.execute(
                "UPDATE mail_attachments SET last_accessed_at = ? WHERE attachment_id = ?",
                (now, record.attachment_id),
            )
            return replace(record, last_accessed_at=now)

    def find_source(self, account_id: str, source_key: str) -> AttachmentRecord | None:
        if not source_key:
            return None
        with self._lock:
            with self.index._connection() as connection:
                row = connection.execute(
                    "SELECT attachment_id FROM mail_attachments WHERE account_id = ? "
                    "AND source_key = ? ORDER BY created_at DESC LIMIT 1",
                    (str(account_id), str(source_key)),
                ).fetchone()
            if row is None:
                return None
            try:
                record = self.get(row[0], account_id)
                self._integrity(record)
                return record
            except ValueError:
                return None

    @contextmanager
    def lease(self, ids, account_id: str):
        if isinstance(ids, str):
            raise ValueError("附件必须是附件标识列表。")
        with self._lock:
            records = tuple(self.get(value, account_id) for value in dict.fromkeys(ids))
            for record in records:
                self._integrity(record)
            for record in records:
                self._leases[record.attachment_id] += 1
        try:
            yield records
        finally:
            with self._lock:
                for record in records:
                    self._leases[record.attachment_id] -= 1
                    if self._leases[record.attachment_id] <= 0:
                        del self._leases[record.attachment_id]

    def receive_file(
        self, account_id: str, writer: Callable[[Path, int], None], *,
        filename: str, content_type: str = "", owner_umo: str = "",
        owner_sender_id: str = "", expected_size: int = 0, source_key: str = "",
    ) -> AttachmentRecord:
        filename = str(filename or "attachment").replace("\\", "/").split("/")[-1]
        filename = "".join(c for c in filename if c >= " " and c != "\x7f")[:255] or "attachment"
        content_type = str(content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        if not re.fullmatch(r"[\w!#$&^.+-]+/[\w!#$&^.+-]+", content_type):
            content_type = "application/octet-stream"
        with self._lock:
            self._recover_orphans()
            protected = self._protected()
            reclaimable = sum(
                int(row["size_bytes"]) for row in self._rows()
                if row["attachment_id"] not in protected
            )
            writable = max(0, self.max_total_bytes - max(0, self._disk_bytes() - reclaimable))
            estimate = max(0, int(expected_size))
            # MIME metadata can describe encoded bytes; the writer enforces decoded size.
            reserve = min(estimate, writable) if estimate else writable
            self._make_room(reserve)
            available = self.max_total_bytes - self._disk_bytes()
            attachment_id = uuid.uuid4().hex
            temporary = self.root / f".partial-{attachment_id}"
            destination = self._path(attachment_id)
            committed = False
            try:
                writer(temporary, available)
                if temporary.is_symlink() or not stat.S_ISREG(temporary.stat().st_mode):
                    raise ValueError("附件必须是普通文件。")
                size = temporary.stat().st_size
                if size > available:
                    raise AttachmentQuotaError("附件写入超出缓存可用容量。")
                digest = hashlib.sha256()
                with temporary.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                    os.fsync(stream.fileno())
                now = time.time()
                record = AttachmentRecord(
                    attachment_id, str(account_id), filename, content_type, size,
                    digest.hexdigest(), str(owner_umo), str(owner_sender_id), now,
                    now, destination, str(source_key),
                )
                os.chmod(temporary, 0o400)
                temporary.replace(destination)
                self._sync_directory()
                with self.index._connection() as connection:
                    connection.execute(
                        "INSERT INTO mail_attachments (attachment_id, account_id, filename, "
                        "content_type, size_bytes, sha256, owner_umo, owner_sender_id, "
                        "created_at, last_accessed_at, source_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record.attachment_id, record.account_id, record.filename,
                         record.content_type, record.size_bytes, record.sha256,
                         record.owner_umo, record.owner_sender_id, now, now, record.source_key),
                    )
                committed = True
                return record
            finally:
                temporary.unlink(missing_ok=True)
                if not committed:
                    destination.unlink(missing_ok=True)

    def import_file(
        self, account_id: str, source_path: str | Path, *, filename: str,
        content_type: str = "", owner_umo: str = "", owner_sender_id: str = "",
        source_key: str = "",
    ) -> AttachmentRecord:
        source = Path(source_path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("附件来源必须是有效普通文件。")
        size = source.stat().st_size
        if size > self.max_total_bytes:
            raise AttachmentQuotaError("附件超出附件缓存总容量，请调整容量或选择更小的文件。")

        def writer(path: Path, max_bytes: int) -> None:
            written = 0
            with source.open("rb") as incoming, path.open("wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise AttachmentQuotaError("附件超出缓存可用容量。")
                    outgoing.write(chunk)

        return self.receive_file(
            account_id, writer, filename=filename, content_type=content_type,
            owner_umo=owner_umo, owner_sender_id=owner_sender_id,
            expected_size=size, source_key=source_key,
        )
