from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    decoded: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            for encoding in (charset, "utf-8", "gb18030", "latin-1"):
                if not encoding:
                    continue
                try:
                    decoded.append(part.decode(encoding))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else str(content or "")
    except Exception:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def extract_text_body(message: Message) -> tuple[str, bool]:
    plain: list[str] = []
    html: list[str] = []
    has_attachments = False
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            has_attachments = True
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            text = _part_text(part).strip()
            if text:
                plain.append(text)
        elif content_type == "text/html":
            text = _html_to_text(_part_text(part))
            if text:
                html.append(text)
    body = "\n\n".join(plain or html)
    return re.sub(r"\r\n?", "\n", body).strip(), has_attachments


def _format_date(value: str | None) -> tuple[str, float]:
    if not value:
        return "未知时间", 0.0
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S"), parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return decode_mime_header(value), 0.0


@dataclass(slots=True)
class ParsedMail:
    uid: int
    subject: str
    from_name: str
    from_addr: str
    reply_to: str
    date: str
    timestamp: float
    body: str
    has_attachments: bool
    message_id: str
    references: str

    def body_preview(self, limit: int) -> str:
        if limit <= 0 or len(self.body) <= limit:
            return self.body
        return self.body[:limit].rstrip() + "…"


def parse_mail(raw: bytes, uid: int) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = decode_mime_header(message.get("Subject")) or "(无主题)"
    from_name_raw, from_addr = parseaddr(decode_mime_header(message.get("From")))
    _, reply_addr = parseaddr(decode_mime_header(message.get("Reply-To")))
    date_text, timestamp = _format_date(message.get("Date"))
    body, has_attachments = extract_text_body(message)
    return ParsedMail(
        uid=int(uid),
        subject=subject,
        from_name=decode_mime_header(from_name_raw),
        from_addr=from_addr.strip(),
        reply_to=(reply_addr or from_addr).strip(),
        date=date_text,
        timestamp=timestamp,
        body=body,
        has_attachments=has_attachments,
        message_id=str(message.get("Message-ID") or "").strip(),
        references=str(message.get("References") or "").strip(),
    )


def parse_since_date(value: str | None, *, default_days: int = 7) -> datetime:
    if value:
        return datetime.strptime(value, "%Y-%m-%d")
    return datetime.now().replace(microsecond=0) - timedelta(days=default_days)

