from __future__ import annotations

import base64
import binascii
import hashlib
import imaplib
import quopri
import re
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from .mail_parser import ParsedMail, extract_text_body, parse_mail, safe_attachment_filename
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


_ATTACHMENT_CHUNK_BYTES = 64 * 1024
_SECTION_PATTERN = re.compile(r"[1-9]\d*(?:\.[1-9]\d*)*")


@contextmanager
def _literal_budget(connection: Any, limit: int):
    """Reject oversized server literals before imaplib allocates their payload."""
    original = getattr(connection, "read", None)
    if original is None:
        yield
        return
    consumed = 0

    def bounded_read(size: int):
        nonlocal consumed
        if size < 0 or consumed + size > limit:
            raise ValueError("IMAP 返回的数据超过本次请求的容量限制。")
        consumed += size
        return original(size)

    connection.read = bounded_read
    try:
        yield
    finally:
        connection.read = original


def _parse_structure_value(raw: bytes, offset: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > 50:
        raise ValueError("邮件 MIME 结构嵌套过深。")
    while offset < len(raw) and raw[offset:offset + 1].isspace():
        offset += 1
    if offset >= len(raw):
        raise ValueError("邮件 MIME 结构不完整。")
    char = raw[offset:offset + 1]
    if char == b"(":
        result = []
        offset += 1
        while True:
            while offset < len(raw) and raw[offset:offset + 1].isspace():
                offset += 1
            if raw[offset:offset + 1] == b")":
                return result, offset + 1
            value, offset = _parse_structure_value(raw, offset, depth + 1)
            result.append(value)
            if len(result) > 10000:
                raise ValueError("邮件 MIME 结构过大。")
    if char == b'"':
        result = bytearray()
        offset += 1
        while offset < len(raw):
            char = raw[offset]
            offset += 1
            if char == 34:
                return result.decode("utf-8", errors="replace"), offset
            if char == 92:
                if offset >= len(raw):
                    break
                char = raw[offset]
                offset += 1
            result.append(char)
        raise ValueError("邮件 MIME 字符串不完整。")
    if char == b"{":
        match = re.match(rb"\{(\d+)\}\r?\n", raw[offset:])
        if not match:
            raise ValueError("邮件 MIME literal 格式无效。")
        start = offset + match.end()
        end = start + int(match.group(1))
        if end > len(raw):
            raise ValueError("邮件 MIME literal 不完整。")
        return raw[start:end].decode("utf-8", errors="replace"), end
    match = re.match(rb"[^\s()]+", raw[offset:])
    if not match:
        raise ValueError("邮件 MIME 结构无效。")
    token = match.group(0)
    value = None if token.upper() == b"NIL" else int(token) if token.isdigit() else token.decode("ascii", errors="replace")
    return value, offset + match.end()


def _structure_params(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    return [(str(value[i]).lower(), str(value[i + 1])) for i in range(0, len(value) - 1, 2) if value[i] is not None and value[i + 1] is not None]


def _parameter_header(main: str, params: list[tuple[str, str]]) -> str:
    pieces = [main]
    for key, value in params:
        if not re.fullmatch(r"[a-z0-9*_-]+", key) or any(char in value for char in "\r\n\x00"):
            continue
        pieces.append(key + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "; ".join(pieces)


def _structure_parts(structure: Any, prefix: str = "") -> list[dict[str, Any]]:
    if not isinstance(structure, list) or not structure:
        raise ValueError("邮件 MIME 结构无效。")
    if isinstance(structure[0], list):
        parts = []
        for number, child in enumerate(structure, 1):
            if not isinstance(child, list):
                break
            parts.extend(_structure_parts(child, f"{prefix}.{number}" if prefix else str(number)))
        return parts
    if len(structure) < 7:
        raise ValueError("邮件 MIME 字段不完整。")
    main_type, subtype = str(structure[0]).lower(), str(structure[1]).lower()
    content_type = f"{main_type}/{subtype}"
    params = _structure_params(structure[2])
    disposition_index = 9 if main_type == "text" else 11 if content_type == "message/rfc822" else 8
    disposition = structure[disposition_index] if len(structure) > disposition_index else None
    message = Message(policy=policy.default)
    message["Content-Type"] = _parameter_header(content_type, params)
    if isinstance(disposition, list) and disposition:
        disposition_name = str(disposition[0]).lower()
        message["Content-Disposition"] = _parameter_header(disposition_name, _structure_params(disposition[1] if len(disposition) > 1 else None))
    filename = message.get_filename()
    is_attachment = bool(filename or message.get_content_disposition() == "attachment" or main_type not in {"text", "multipart"})
    encoding = str(structure[5] or "7bit").lower()
    try:
        size = int(structure[6])
    except (ValueError, TypeError) as exc:
        raise ValueError("邮件 MIME 大小无效。") from exc
    if size < 0:
        raise ValueError("邮件 MIME 大小无效。")
    return [{
        "part_id": prefix or "1",
        "filename": safe_attachment_filename(filename, content_type),
        "content_type": content_type,
        "size_bytes": size,
        "encoded_size_bytes": size,
        "size_is_estimate": encoding in {"base64", "quoted-printable"},
        "transfer_encoding": encoding,
        "content_type_header": str(message["Content-Type"]),
        "is_attachment": is_attachment,
    }]


class _TransferDecoder:
    def __init__(self, encoding: str):
        if encoding not in {"base64", "quoted-printable", "7bit", "8bit", "binary"}:
            raise ValueError("不支持此附件的 MIME 传输编码。")
        self.encoding = encoding
        self.pending = b""
        self.ended = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        if self.encoding == "base64":
            data = re.sub(rb"[\t\r\n ]", b"", data)
            if self.ended and data:
                raise ValueError("附件 Base64 编码在结束标记后仍有内容。")
            buffered = self.pending + data
            length = len(buffered) if final else len(buffered) // 4 * 4
            chunk, self.pending = buffered[:length], buffered[length:]
            try:
                decoded = base64.b64decode(chunk, validate=True)
            except binascii.Error as exc:
                raise ValueError("附件 Base64 编码不完整或已损坏。") from exc
            self.ended = self.ended or b"=" in chunk
            return decoded
        if self.encoding == "quoted-printable":
            buffered = self.pending + data
            cut = len(buffered)
            if not final:
                last_equals = buffered.rfind(b"=", max(0, len(buffered) - 2))
                if last_equals >= 0:
                    cut = last_equals
            self.pending = buffered[cut:]
            return quopri.decodestring(buffered[:cut])
        return data


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

    def _supports_gmail_msgid(self) -> bool:
        capabilities = getattr(self.conn, "capabilities", ()) or ()
        normalized = {
            (item.decode(errors="ignore") if isinstance(item, bytes) else str(item))
            .strip()
            .upper()
            for item in capabilities
        }
        return "X-GM-EXT-1" in normalized

    def fetch_uid(self, uid: int, *, headers_only: bool = False) -> ParsedMail:
        if not headers_only:
            parts = self.fetch_structure(uid)
            mail = self.fetch_uid(uid, headers_only=True)
            body_message = Message(policy=policy.default)
            body_message.set_type("multipart/mixed")
            for part in parts:
                if part["is_attachment"] or part["content_type"] not in {"text/plain", "text/html"}:
                    continue
                decoder = _TransferDecoder(part["transfer_encoding"])
                raw = bytearray()
                for chunk in self.iter_section(uid, part["part_id"], part["encoded_size_bytes"]):
                    raw.extend(decoder.feed(chunk))
                raw.extend(decoder.feed(b"", final=True))
                header = ("Content-Type: " + part["content_type_header"] + "\r\n\r\n").encode("utf-8")
                body_message.attach(BytesParser(policy=policy.default).parsebytes(header + raw))
            mail.body, _ = extract_text_body(body_message)
            mail.has_attachments = any(part["is_attachment"] for part in parts)
            return mail
        payload = (
            "BODY.PEEK[HEADER.FIELDS "
            "(SUBJECT FROM REPLY-TO DATE MESSAGE-ID REFERENCES)]"
            if headers_only
            else "RFC822"
        )
        query = (
            f"(X-GM-MSGID {payload})"
            if self._supports_gmail_msgid()
            else f"({payload})"
        )
        status, data = self.conn.uid("fetch", str(uid), query)
        if status != "OK" or not data:
            raise MailNotFoundError(f"IMAP UID {uid} 不存在或已无法读取。")
        raw = next(
            (
                entry[1]
                for entry in data
                if isinstance(entry, tuple)
                and len(entry) > 1
                and isinstance(entry[1], bytes)
            ),
            None,
        )
        if raw is None:
            raise MailNotFoundError(f"IMAP UID {uid} 不存在或已无法读取。")
        mail = parse_mail(raw, uid)
        if self._supports_gmail_msgid():
            metadata = b" ".join(
                entry[0]
                if isinstance(entry, tuple) and isinstance(entry[0], bytes)
                else entry
                for entry in data
                if (
                    isinstance(entry, bytes)
                    or (
                        isinstance(entry, tuple)
                        and entry
                        and isinstance(entry[0], bytes)
                    )
                )
            )
            match = re.search(rb"\bX-GM-MSGID\s+(\d+)\b", metadata, flags=re.I)
            if match:
                mail.gmail_msgid = match.group(1).decode("ascii")
        return mail

    def fetch_structure(self, uid: int) -> list[dict[str, Any]]:
        if int(uid) <= 0:
            raise ValueError("邮件 UID 必须是正整数。")
        with _literal_budget(self.conn, 2 * 1024 * 1024):
            status, data = self.conn.uid("fetch", str(int(uid)), "(UID BODYSTRUCTURE)")
        if status != "OK" or not data:
            raise MailNotFoundError(f"IMAP UID {uid} 不存在或已无法读取。")
        segments = []
        for entry in data:
            if isinstance(entry, tuple) and len(entry) == 2 and all(isinstance(item, bytes) for item in entry):
                segments.append(entry[0] + b"\r\n" + entry[1])
            elif isinstance(entry, bytes):
                segments.append(entry)
        raw = b" ".join(segments)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("邮件 MIME 结构过大。")
        marker = re.search(rb"\bBODYSTRUCTURE\s+", raw, flags=re.I)
        if marker is None:
            raise RuntimeError("IMAP 服务器未返回邮件 MIME 结构。")
        structure, end = _parse_structure_value(raw, marker.end())
        envelope = raw[:marker.start()] + raw[end:]
        uid_values = re.findall(rb"\bUID\s+(\d+)\b", envelope, flags=re.I)
        if not uid_values or any(int(value) != int(uid) for value in uid_values):
            raise ValueError("IMAP 返回的邮件 UID 与请求不符。")
        return _structure_parts(structure)

    def iter_section(self, uid: int, part_id: str, encoded_size: int):
        if not _SECTION_PATTERN.fullmatch(str(part_id)):
            raise ValueError("附件 MIME 标识无效。")
        offset = 0
        while offset < encoded_size:
            count = min(_ATTACHMENT_CHUNK_BYTES, encoded_size - offset)
            with _literal_budget(self.conn, count):
                status, data = self.conn.uid("fetch", str(int(uid)), f"(UID BODY.PEEK[{part_id}]<{offset}.{count}>)")
            if status != "OK" or not data:
                raise MailNotFoundError("附件不存在或已无法读取。")
            metadata = b" ".join(entry[0] if isinstance(entry, tuple) else entry for entry in data if isinstance(entry, bytes) or (isinstance(entry, tuple) and entry and isinstance(entry[0], bytes)))
            uid_values = re.findall(rb"\bUID\s+(\d+)\b", metadata, flags=re.I)
            if not uid_values or any(int(value) != int(uid) for value in uid_values):
                raise ValueError("IMAP 返回的邮件 UID 与请求不符。")
            literals = [(entry[0], entry[1]) for entry in data if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], bytes)]
            if len(literals) != 1:
                raise ValueError("IMAP 返回的附件分段无效。")
            header, chunk = literals[0]
            match = re.search(rb"BODY\[([^\]]+)\](?:<(\d+)>)?\s+\{(\d+)\}", header, flags=re.I)
            if match is None or match.group(1).decode("ascii", errors="replace") != part_id or int(match.group(2) or 0) != offset:
                raise ValueError("IMAP 返回了不匹配的附件分段。")
            if not chunk or len(chunk) > count or int(match.group(3)) != len(chunk):
                raise ValueError("IMAP 附件分段长度与请求不符。")
            offset += len(chunk)
            yield chunk

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


def list_attachments(
    account: dict[str, Any],
    uid: int,
    expected_uidvalidity: int | None,
    timeout: int = 20,
    *,
    folder: str | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    with ImapMailbox(account, timeout, folder=folder) as mailbox:
        current_uidvalidity = mailbox.uidvalidity
        if expected_uidvalidity is not None and int(expected_uidvalidity) != current_uidvalidity:
            raise MailboxChangedError(expected_uidvalidity, current_uidvalidity)
        parts = mailbox.fetch_structure(int(uid))
        return current_uidvalidity, [
            {key: value for key, value in part.items() if key not in {"is_attachment", "content_type_header"}}
            for part in parts if part["is_attachment"]
        ]


def download_attachment(
    account: dict[str, Any],
    uid: int,
    part_id: str,
    destination: Path,
    expected_uidvalidity: int | None,
    timeout: int = 20,
    *,
    folder: str | None = None,
    max_bytes: int,
) -> dict[str, Any]:
    """Download a selected MIME part into a new file, removing partial failures."""
    if not _SECTION_PATTERN.fullmatch(str(part_id)) or int(max_bytes) <= 0:
        raise ValueError("附件标识或下载容量限制无效。")
    destination = Path(destination)
    created = False
    try:
        with ImapMailbox(account, timeout, folder=folder) as mailbox:
            current_uidvalidity = mailbox.uidvalidity
            if expected_uidvalidity is not None and int(expected_uidvalidity) != current_uidvalidity:
                raise MailboxChangedError(expected_uidvalidity, current_uidvalidity)
            parts = mailbox.fetch_structure(int(uid))
            part = next((item for item in parts if item["part_id"] == str(part_id) and item["is_attachment"]), None)
            if part is None:
                raise MailNotFoundError("指定的附件不存在。")
            if part["encoded_size_bytes"] > int(max_bytes) * 2 + _ATTACHMENT_CHUNK_BYTES:
                raise ValueError("附件超过可用缓存容量。")
            decoder = _TransferDecoder(part["transfer_encoding"])
            digest = hashlib.sha256()
            size = 0
            with destination.open("xb") as output:
                created = True
                destination.chmod(0o600)
                for encoded in mailbox.iter_section(int(uid), str(part_id), part["encoded_size_bytes"]):
                    decoded = decoder.feed(encoded)
                    size += len(decoded)
                    if size > int(max_bytes):
                        raise ValueError("附件超过可用缓存容量。")
                    output.write(decoded)
                    digest.update(decoded)
                tail = decoder.feed(b"", final=True)
                size += len(tail)
                if size > int(max_bytes):
                    raise ValueError("附件超过可用缓存容量。")
                output.write(tail)
                digest.update(tail)
            return {
                "part_id": str(part_id),
                "filename": part["filename"],
                "content_type": part["content_type"],
                "size_bytes": size,
                "size_is_estimate": False,
                "sha256": digest.hexdigest(),
                "uidvalidity": current_uidvalidity,
            }
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


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
