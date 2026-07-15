from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    kind: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    remote_dns: bool = True

    @property
    def enabled(self) -> bool:
        return self.kind != "none"


def proxy_config_from_account(account: dict[str, Any]) -> ProxyConfig:
    kind = str(account.get("proxy_type") or "none").strip().lower()
    if kind not in {"none", "http", "socks4", "socks5"}:
        raise ValueError("代理类型仅支持 none、http、socks4 或 socks5。")
    if kind == "none":
        return ProxyConfig(kind="none", host="", port=0)
    host = str(account.get("proxy_host") or "").strip()
    try:
        port = int(account.get("proxy_port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("代理端口必须是正整数。") from exc
    if not host or port <= 0 or port > 65535:
        raise ValueError("启用代理后必须配置有效的代理主机和端口。")
    return ProxyConfig(
        kind=kind,
        host=host,
        port=port,
        username=str(account.get("proxy_username") or "").strip(),
        password=str(account.get("proxy_password") or ""),
        remote_dns=bool(account.get("proxy_dns", True)),
    )


def _load_socks():
    try:
        import socks
    except ImportError as exc:
        raise RuntimeError("已启用邮箱代理，但未安装 PySocks；请安装插件 requirements.txt。") from exc
    return socks


def create_connection(
    destination: tuple[str, int],
    *,
    proxy: ProxyConfig,
    timeout: float | None = None,
) -> socket.socket:
    if not proxy.enabled:
        return socket.create_connection(destination, timeout=timeout)
    socks = _load_socks()
    proxy_types = {
        "http": socks.HTTP,
        "socks4": socks.SOCKS4,
        "socks5": socks.SOCKS5,
    }
    return socks.create_connection(
        destination,
        timeout=timeout,
        proxy_type=proxy_types[proxy.kind],
        proxy_addr=proxy.host,
        proxy_port=proxy.port,
        proxy_rdns=proxy.remote_dns,
        proxy_username=proxy.username or None,
        proxy_password=proxy.password or None,
    )

