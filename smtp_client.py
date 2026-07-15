from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any

from .mail_parser import ParsedMail
from .proxy_utils import ProxyConfig, create_connection, proxy_config_from_account


class _ProxySMTP(smtplib.SMTP):
    def __init__(self, host: str, port: int, *, timeout: int, proxy: ProxyConfig) -> None:
        self._mail_proxy = proxy
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return create_connection((host, port), proxy=self._mail_proxy, timeout=timeout)


class _ProxySMTPSSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: int,
        context: ssl.SSLContext,
        proxy: ProxyConfig,
    ) -> None:
        self._mail_proxy = proxy
        super().__init__(host, port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        raw_socket = create_connection((host, port), proxy=self._mail_proxy, timeout=timeout)
        return self.context.wrap_socket(raw_socket, server_hostname=host)


def _smtp_identity(account: dict[str, Any]) -> tuple[str, str, str]:
    email_addr = str(account.get("email") or "").strip()
    username = str(account.get("smtp_username") or account.get("username") or email_addr).strip()
    password = str(account.get("smtp_password") or account.get("password") or "")
    if not email_addr or "@" not in parseaddr(email_addr)[1]:
        raise ValueError("发件邮箱地址无效。")
    if not username or not password:
        raise ValueError("SMTP 用户名或授权码未配置。")
    return email_addr, username, password


def _validate_header(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{label}不能为空。")
    if "\r" in cleaned or "\n" in cleaned:
        raise ValueError(f"{label}不能包含换行。")
    return cleaned


def _connect(account: dict[str, Any], timeout: int):
    host = str(account.get("smtp_host") or "").strip()
    try:
        port = int(account.get("smtp_port") or 465)
    except (TypeError, ValueError):
        port = 465
    security = str(account.get("smtp_security") or "ssl").lower()
    if not host or port <= 0:
        raise ValueError("SMTP 服务器或端口配置无效。")
    context = ssl.create_default_context()
    proxy = proxy_config_from_account(account)
    if security == "ssl":
        if proxy.enabled:
            return _ProxySMTPSSL(host, port, timeout=timeout, context=context, proxy=proxy)
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    if security == "starttls":
        client = (
            _ProxySMTP(host, port, timeout=timeout, proxy=proxy)
            if proxy.enabled
            else smtplib.SMTP(host, port, timeout=timeout)
        )
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        return client
    raise ValueError("SMTP 安全模式仅支持 ssl 或 starttls。")


def build_message(
    account: dict[str, Any], recipient: str, subject: str, body: str, *, original: ParsedMail | None = None
) -> EmailMessage:
    from_addr, _, _ = _smtp_identity(account)
    _, to_addr = parseaddr(_validate_header(recipient, "收件人"))
    if not to_addr or "@" not in to_addr:
        raise ValueError("收件人邮箱地址无效。")
    subject = _validate_header(subject, "主题")
    body = str(body or "").strip()
    if not body:
        raise ValueError("正文不能为空。")
    message = EmailMessage()
    sender_name = str(account.get("sender_name") or "AstrBot").strip()
    message["From"] = formataddr((sender_name, from_addr))
    message["To"] = to_addr
    message["Subject"] = subject
    if original and original.message_id:
        message["In-Reply-To"] = original.message_id
        references = " ".join(part for part in (original.references, original.message_id) if part).strip()
        if references:
            message["References"] = references
    message.set_content(body)
    return message


def send_message(account: dict[str, Any], message: EmailMessage, timeout: int = 20) -> None:
    _, username, password = _smtp_identity(account)
    client = _connect(account, timeout)
    try:
        client.login(username, password)
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTP 服务器拒收了部分或全部收件人。")
    finally:
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass


def send_mail(account: dict[str, Any], recipient: str, subject: str, body: str, timeout: int = 20) -> None:
    send_message(account, build_message(account, recipient, subject, body), timeout)


def send_reply(account: dict[str, Any], original: ParsedMail, body: str, timeout: int = 20) -> None:
    recipient = original.reply_to or original.from_addr
    if not recipient:
        raise ValueError("原邮件没有可用的回复地址。")
    subject = original.subject.strip() or "(无主题)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    send_message(account, build_message(account, recipient, subject, body, original=original), timeout)


def test_smtp(account: dict[str, Any], timeout: int = 20) -> None:
    _, username, password = _smtp_identity(account)
    client = _connect(account, timeout)
    try:
        client.login(username, password)
        code, _ = client.noop()
        if int(code) >= 400:
            raise RuntimeError("SMTP NOOP 测试失败。")
    finally:
        try:
            client.quit()
        except Exception:
            client.close()
