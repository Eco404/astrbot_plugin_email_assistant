from __future__ import annotations

import base64
import imaplib
import re
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


@dataclass(frozen=True, slots=True)
class MailFolder:
    name: str
    display_name: str
    delimiter: str
    attributes: tuple[str, ...]
    selectable: bool
    special_use: str


@dataclass(frozen=True, slots=True)
class MailTransferResult:
    operation: str
    source_folder: str
    target_folder: str
    source_uid: int
    used_uid_move: bool


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


def encode_mailbox_name(value: str) -> str:
    """Encode a Unicode mailbox name using IMAP modified UTF-7."""
    text = str(value or "")
    result: list[str] = []
    buffered: list[str] = []

    def flush() -> None:
        if not buffered:
            return
        raw = "".join(buffered).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        result.append(f"&{encoded}-")
        buffered.clear()

    for char in text:
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            buffered.append(char)
    flush()
    return "".join(result)


def decode_mailbox_name(value: str | bytes) -> str:
    """Decode an IMAP modified UTF-7 mailbox name."""
    text = value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value or "")
    result: list[str] = []
    position = 0
    while position < len(text):
        marker = text.find("&", position)
        if marker < 0:
            result.append(text[position:])
            break
        result.append(text[position:marker])
        end = text.find("-", marker)
        if end < 0:
            result.append(text[marker:])
            break
        token = text[marker + 1 : end]
        if not token:
            result.append("&")
        else:
            try:
                raw = base64.b64decode(
                    token.replace(",", "/") + "=" * (-len(token) % 4)
                )
                result.append(raw.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                result.append(text[marker : end + 1])
        position = end + 1
    return "".join(result)


def _validate_folder_name(value: Any, label: str = "文件夹") -> str:
    folder = str(value or "").strip()
    if not folder:
        raise ValueError(f"{label}不能为空。")
    if len(folder) > 512 or any(char in folder for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{label}名称无效。")
    return folder


def _mailbox_arg(value: str) -> str:
    encoded = encode_mailbox_name(_validate_folder_name(value))
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_PATTERN = re.compile(
    r"^\((?P<attrs>[^)]*)\)\s+(?P<delimiter>NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$"
)


def _unquote_list_token(value: str) -> str:
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
        token = re.sub(r"\\(.)", r"\1", token)
    return token


def parse_folder_list_item(value: Any) -> MailFolder | None:
    if isinstance(value, tuple):
        value = value[-1] if value else b""
    text = value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value or "")
    match = _LIST_PATTERN.match(text.strip())
    if not match:
        return None
    attributes = tuple(
        item.strip() for item in match.group("attrs").split() if item.strip()
    )
    delimiter_token = match.group("delimiter")
    delimiter = "" if delimiter_token.upper() == "NIL" else _unquote_list_token(delimiter_token)
    encoded_name = _unquote_list_token(match.group("name"))
    name = decode_mailbox_name(encoded_name)
    lowered = {item.lower() for item in attributes}
    special = next(
        (
            item[1:].lower()
            for item in attributes
            if item.lower()
            in {
                "\\inbox",
                "\\sent",
                "\\drafts",
                "\\trash",
                "\\junk",
                "\\all",
                "\\archive",
                "\\flagged",
                "\\important",
            }
        ),
        "inbox" if name.upper() == "INBOX" else "",
    )
    display_name = name.rsplit(delimiter, 1)[-1] if delimiter and delimiter in name else name
    return MailFolder(
        name=name,
        display_name=display_name,
        delimiter=delimiter,
        attributes=attributes,
        selectable="\\noselect" not in lowered,
        special_use=special,
    )


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
    def __init__(
        self,
        account: dict[str, Any],
        timeout: int = 20,
        *,
        folder: str | None = None,
        readonly: bool = True,
        select_mailbox: bool = True,
    ) -> None:
        self.account = account
        self.timeout = _positive_int(timeout, 20)
        self.connection: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        self.folder = str(folder or account.get("folder") or "INBOX").strip() or "INBOX"
        self.readonly = bool(readonly)
        self.select_mailbox = bool(select_mailbox)

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
        if self.select_mailbox:
            status, _ = self.connection.select(
                _mailbox_arg(self.folder), readonly=self.readonly
            )
            if status != "OK":
                raise RuntimeError(f"无法打开 IMAP 文件夹：{self.folder}")
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

    def list_folders(self) -> list[MailFolder]:
        # imaplib does not quote an explicitly supplied empty string. Calling
        # LIST with its defaults emits the required: LIST "" *
        status, data = self.conn.list()
        if status != "OK":
            detail = repr(data[:3] if isinstance(data, list) else data)[:300]
            raise RuntimeError(
                f"IMAP 服务器拒绝列出文件夹，status={status}, response={detail}"
            )
        folders = [parse_folder_list_item(item) for item in (data or [])]
        return sorted(
            [item for item in folders if item is not None],
            key=lambda item: (item.special_use != "inbox", item.name.casefold()),
        )

    def ensure_uidvalidity(self, expected_uidvalidity: int | None) -> None:
        if expected_uidvalidity is None:
            return
        current = self.uidvalidity
        if int(expected_uidvalidity) != current:
            raise MailboxChangedError(expected_uidvalidity, current)


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
    folder: str | None = None,
) -> HeaderSyncResult:
    mailbox_context = (
        ImapMailbox(account, timeout)
        if folder is None
        else ImapMailbox(account, timeout, folder=folder)
    )
    with mailbox_context as mailbox:
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
    folder: str | None = None,
) -> tuple[int, ParsedMail]:
    mailbox_context = (
        ImapMailbox(account, timeout)
        if folder is None
        else ImapMailbox(account, timeout, folder=folder)
    )
    with mailbox_context as mailbox:
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


def list_folders(account: dict[str, Any], timeout: int = 20) -> list[MailFolder]:
    with ImapMailbox(account, timeout, select_mailbox=False) as mailbox:
        return mailbox.list_folders()


def create_folder(
    account: dict[str, Any], folder: str, timeout: int = 20
) -> None:
    folder = _validate_folder_name(folder, "新文件夹")
    with ImapMailbox(account, timeout, select_mailbox=False) as mailbox:
        status, _ = mailbox.conn.create(_mailbox_arg(folder))
        if status != "OK":
            raise RuntimeError(f"创建 IMAP 文件夹失败：{folder}")
        try:
            mailbox.conn.subscribe(_mailbox_arg(folder))
        except Exception:
            pass


def transfer_message(
    account: dict[str, Any],
    source_folder: str,
    target_folder: str,
    uid: int,
    expected_uidvalidity: int | None,
    *,
    move: bool,
    timeout: int = 20,
) -> MailTransferResult:
    source = _validate_folder_name(source_folder, "源文件夹")
    target = _validate_folder_name(target_folder, "目标文件夹")
    if source == target:
        raise ValueError("源文件夹和目标文件夹不能相同。")
    normalized_uid = _positive_int(uid, 0)
    if normalized_uid <= 0:
        raise ValueError("邮件 UID 必须是正整数。")
    with ImapMailbox(
        account, timeout, folder=source, readonly=False
    ) as mailbox:
        mailbox.ensure_uidvalidity(expected_uidvalidity)
        mailbox.fetch_uid(normalized_uid, headers_only=True)
        capabilities = {
            item.decode("ascii", errors="ignore").upper()
            if isinstance(item, bytes)
            else str(item).upper()
            for item in getattr(mailbox.conn, "capabilities", ())
        }
        target_arg = _mailbox_arg(target)
        used_uid_move = False
        if move and "MOVE" in capabilities:
            status, _ = mailbox.conn.uid("MOVE", str(normalized_uid), target_arg)
            used_uid_move = True
        else:
            if move and "UIDPLUS" not in capabilities:
                raise RuntimeError(
                    "服务器不支持 UID MOVE 或安全的 UID EXPUNGE，已拒绝移动以避免误删其他邮件。"
                )
            status, _ = mailbox.conn.uid("COPY", str(normalized_uid), target_arg)
            if status == "OK" and move:
                status, _ = mailbox.conn.uid(
                    "STORE", str(normalized_uid), "+FLAGS.SILENT", r"(\Deleted)"
                )
                if status == "OK":
                    status, _ = mailbox.conn.uid("EXPUNGE", str(normalized_uid))
                    if status != "OK":
                        try:
                            mailbox.conn.uid(
                                "STORE",
                                str(normalized_uid),
                                "-FLAGS.SILENT",
                                r"(\Deleted)",
                            )
                        except Exception:
                            pass
        if status != "OK":
            raise RuntimeError("IMAP 移动邮件失败。" if move else "IMAP 复制邮件失败。")
    return MailTransferResult(
        operation="move" if move else "copy",
        source_folder=source,
        target_folder=target,
        source_uid=normalized_uid,
        used_uid_move=used_uid_move,
    )
