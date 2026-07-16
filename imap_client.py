from __future__ import annotations

import imaplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .mail_parser import ParsedMail, parse_mail
from .proxy_utils import ProxyConfig, create_connection, proxy_config_from_account


@dataclass(slots=True)
class FetchItem:
    uid: int
    mail: ParsedMail | None = None
    error: str = ""


@dataclass(slots=True)
class HeaderSyncResult:
    uidvalidity: int
    uidnext: int
    scanned_through_uid: int
    headers: list[ParsedMail]
    remote_uids: set[int] | None
    uidvalidity_changed: bool
    history_before_uid: int | None = None
    history_complete: bool | None = None


class MailNotFoundError(RuntimeError):
    pass


class MailboxChangedError(RuntimeError):
    def __init__(self, expected_uidvalidity: int, actual_uidvalidity: int) -> None:
        self.expected_uidvalidity = int(expected_uidvalidity)
        self.actual_uidvalidity = int(actual_uidvalidity)
        super().__init__(
            "IMAP 文件夹 UIDVALIDITY 已变化，原 UID 已失效。"
        )


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class _ProxyIMAP4(imaplib.IMAP4):
    def __init__(self, host: str, port: int, *, timeout: int, proxy: ProxyConfig) -> None:
        self._mail_proxy = proxy
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout):
        return create_connection((self.host, self.port), proxy=self._mail_proxy, timeout=timeout)


class _ProxyIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        timeout: int,
        proxy: ProxyConfig,
    ) -> None:
        self._mail_proxy = proxy
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        raw_socket = create_connection((self.host, self.port), proxy=self._mail_proxy, timeout=timeout)
        return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


class ImapMailbox:
    def __init__(self, account: dict[str, Any], timeout: int = 20) -> None:
        self.account = account
        self.timeout = _positive_int(timeout, 20)
        self.connection: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        self.folder = "INBOX"

    def __enter__(self) -> "ImapMailbox":
        host = str(self.account.get("imap_host") or "").strip()
        port = _positive_int(self.account.get("imap_port"), 993)
        username = str(self.account.get("username") or self.account.get("email") or "").strip()
        password = str(self.account.get("password") or "")
        security = str(self.account.get("imap_security") or "ssl").lower()
        if not host or not username or not password:
            raise ValueError("IMAP 配置不完整，请检查服务器、用户名和授权码。")
        context = ssl.create_default_context()
        proxy = proxy_config_from_account(self.account)
        if security == "ssl":
            if proxy.enabled:
                self.connection = _ProxyIMAP4SSL(
                    host,
                    port,
                    ssl_context=context,
                    timeout=self.timeout,
                    proxy=proxy,
                )
            else:
                self.connection = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=self.timeout)
        elif security == "starttls":
            self.connection = (
                _ProxyIMAP4(host, port, timeout=self.timeout, proxy=proxy)
                if proxy.enabled
                else imaplib.IMAP4(host, port, timeout=self.timeout)
            )
            self.connection.starttls(ssl_context=context)
        else:
            raise ValueError("IMAP 安全模式仅支持 ssl 或 starttls。")
        self.connection.login(username, password)
        self.folder = str(self.account.get("folder") or "INBOX").strip() or "INBOX"
        status, _ = self.connection.select(self.folder, readonly=True)
        if status != "OK":
            raise RuntimeError("无法打开配置的 IMAP 文件夹。")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.connection is None:
            return
        try:
            self.connection.close()
        except Exception:
            pass
        try:
            self.connection.logout()
        except Exception:
            pass

    @property
    def conn(self):
        if self.connection is None:
            raise RuntimeError("IMAP 尚未连接。")
        return self.connection

    def search_uids(self, criterion: str) -> list[int]:
        status, data = self.conn.uid("search", None, criterion)
        if status != "OK" or not data or not data[0]:
            return []
        return sorted(int(item) for item in data[0].split() if item.isdigit())

    def _response_int(self, name: str) -> int:
        status, data = self.conn.response(name)
        if status is None or not data:
            return 0
        for item in reversed(data):
            text = item.decode(errors="ignore") if isinstance(item, bytes) else str(item)
            digits = "".join(char for char in text if char.isdigit())
            if digits:
                return int(digits)
        return 0

    @property
    def uidvalidity(self) -> int:
        value = self._response_int("UIDVALIDITY")
        if value <= 0:
            raise RuntimeError("IMAP 服务器未返回有效的 UIDVALIDITY。")
        return value

    @property
    def uidnext(self) -> int:
        value = self._response_int("UIDNEXT")
        if value > 0:
            return value
        uids = self.search_uids("ALL")
        return (uids[-1] + 1) if uids else 1

    def fetch_uid(self, uid: int, *, headers_only: bool = False) -> ParsedMail:
        query = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM REPLY-TO DATE MESSAGE-ID REFERENCES)])" if headers_only else "(RFC822)"
        status, data = self.conn.uid("fetch", str(uid), query)
        if status != "OK" or not data:
            raise MailNotFoundError(f"IMAP UID {uid} 不存在或已无法读取。")
        raw = next(
            (entry[1] for entry in data if isinstance(entry, tuple) and len(entry) > 1 and isinstance(entry[1], bytes)),
            None,
        )
        if raw is None:
            raise MailNotFoundError(f"IMAP UID {uid} 不存在或已无法读取。")
        return parse_mail(raw, uid)


def get_max_uid(account: dict[str, Any], timeout: int = 20) -> int:
    with ImapMailbox(account, timeout) as mailbox:
        uids = mailbox.search_uids("ALL")
        return uids[-1] if uids else 0


def fetch_after_uid(
    account: dict[str, Any], last_uid: int, limit: int = 20, timeout: int = 20
) -> list[FetchItem]:
    with ImapMailbox(account, timeout) as mailbox:
        uids = [uid for uid in mailbox.search_uids(f"UID {int(last_uid) + 1}:*") if uid > int(last_uid)]
        items: list[FetchItem] = []
        for uid in uids[: _positive_int(limit, 20)]:
            try:
                items.append(FetchItem(uid=uid, mail=mailbox.fetch_uid(uid)))
            except Exception as exc:
                items.append(FetchItem(uid=uid, error=f"{type(exc).__name__}: {exc}"))
        return items


def query_since(
    account: dict[str, Any], since: datetime, limit: int = 20, timeout: int = 20
) -> list[ParsedMail]:
    criterion = f'SINCE "{since.strftime("%d-%b-%Y")}"'
    with ImapMailbox(account, timeout) as mailbox:
        uids = mailbox.search_uids(criterion)
        results: list[ParsedMail] = []
        for uid in reversed(uids[-_positive_int(limit, 20) :]):
            try:
                results.append(mailbox.fetch_uid(uid, headers_only=True))
            except Exception:
                continue
        return results


def fetch_latest(
    account: dict[str, Any], since: datetime | None = None, timeout: int = 20
) -> ParsedMail | None:
    criterion = f'SINCE "{since.strftime("%d-%b-%Y")}"' if since else "ALL"
    with ImapMailbox(account, timeout) as mailbox:
        uids = mailbox.search_uids(criterion)
        for uid in reversed(uids):
            try:
                return mailbox.fetch_uid(uid)
            except Exception:
                continue
    return None


def sync_headers(
    account: dict[str, Any],
    known_uidvalidity: int | None,
    after_uid: int,
    initial_since: datetime,
    initial_limit: int = 200,
    batch_limit: int = 100,
    timeout: int = 20,
    reconcile_all: bool = False,
    force_initial: bool = False,
    known_history_before_uid: int = 0,
    known_history_complete: bool = False,
) -> HeaderSyncResult:
    with ImapMailbox(account, timeout) as mailbox:
        current_uidvalidity = mailbox.uidvalidity
        uidnext = mailbox.uidnext
        changed = (
            known_uidvalidity is not None
            and int(known_uidvalidity) != current_uidvalidity
        )
        new_generation = known_uidvalidity is None or changed
        date_scan = force_initial or new_generation
        history_before_uid: int | None = None
        history_complete: bool | None = None
        if date_scan:
            candidates = mailbox.search_uids(
                f'SINCE "{initial_since.strftime("%d-%b-%Y")}"'
            )
            selected = candidates[-_positive_int(initial_limit, 200) :]
            scanned_through = selected[-1] if selected else max(0, uidnext - 1)
            if new_generation:
                history_before_uid = selected[0] if selected else max(1, uidnext)
                history_complete = history_before_uid <= 1
        else:
            candidates = [
                uid
                for uid in mailbox.search_uids(f"UID {int(after_uid) + 1}:*")
                if uid > int(after_uid)
            ]
            selected = candidates[: _positive_int(batch_limit, 100)]
            scanned_through = selected[-1] if selected else int(after_uid)

            history_before_uid = max(0, int(known_history_before_uid or 0))
            history_complete = bool(known_history_complete)
            remaining = max(0, _positive_int(batch_limit, 100) - len(selected))
            if not history_complete and remaining:
                if history_before_uid <= 0:
                    history_before_uid = max(1, min(int(after_uid) + 1, uidnext))
                if history_before_uid <= 1:
                    history_complete = True
                else:
                    older_candidates = mailbox.search_uids(
                        f"UID 1:{history_before_uid - 1}"
                    )
                    older_selected = older_candidates[-remaining:]
                    selected.extend(older_selected)
                    if older_selected:
                        history_before_uid = older_selected[0]
                    history_complete = len(older_candidates) <= remaining

        headers: list[ParsedMail] = []
        for uid in selected:
            try:
                headers.append(mailbox.fetch_uid(uid, headers_only=True))
            except Exception:
                continue
        remote_uids = set(mailbox.search_uids("ALL")) if reconcile_all else None
        return HeaderSyncResult(
            uidvalidity=current_uidvalidity,
            uidnext=uidnext,
            scanned_through_uid=scanned_through,
            headers=headers,
            remote_uids=remote_uids,
            uidvalidity_changed=changed,
            history_before_uid=history_before_uid,
            history_complete=history_complete,
        )


def fetch_detail_checked(
    account: dict[str, Any],
    uid: int,
    expected_uidvalidity: int | None,
    timeout: int = 20,
) -> tuple[int, ParsedMail]:
    with ImapMailbox(account, timeout) as mailbox:
        current_uidvalidity = mailbox.uidvalidity
        if (
            expected_uidvalidity is not None
            and int(expected_uidvalidity) != current_uidvalidity
        ):
            raise MailboxChangedError(expected_uidvalidity, current_uidvalidity)
        return current_uidvalidity, mailbox.fetch_uid(int(uid))


def fetch_detail(account: dict[str, Any], uid: int, timeout: int = 20) -> ParsedMail:
    with ImapMailbox(account, timeout) as mailbox:
        return mailbox.fetch_uid(uid)


def test_imap(account: dict[str, Any], timeout: int = 20) -> None:
    with ImapMailbox(account, timeout) as mailbox:
        status, _ = mailbox.conn.noop()
        if status != "OK":
            raise RuntimeError("IMAP NOOP 测试失败。")
