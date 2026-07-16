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
        folder = str(self.account.get("folder") or "INBOX").strip() or "INBOX"
        status, _ = self.connection.select(folder, readonly=True)
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

    def fetch_uid(self, uid: int, *, headers_only: bool = False) -> ParsedMail:
        query = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM REPLY-TO DATE MESSAGE-ID REFERENCES)])" if headers_only else "(RFC822)"
        status, data = self.conn.uid("fetch", str(uid), query)
        if status != "OK" or not data:
            raise RuntimeError(f"IMAP 获取 UID {uid} 失败。")
        raw = next(
            (entry[1] for entry in data if isinstance(entry, tuple) and len(entry) > 1 and isinstance(entry[1], bytes)),
            None,
        )
        if raw is None:
            raise RuntimeError(f"IMAP UID {uid} 没有可解析的邮件内容。")
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


def fetch_detail(account: dict[str, Any], uid: int, timeout: int = 20) -> ParsedMail:
    with ImapMailbox(account, timeout) as mailbox:
        return mailbox.fetch_uid(uid)


def test_imap(account: dict[str, Any], timeout: int = 20) -> None:
    with ImapMailbox(account, timeout) as mailbox:
        status, _ = mailbox.conn.noop()
        if status != "OK":
            raise RuntimeError("IMAP NOOP 测试失败。")
