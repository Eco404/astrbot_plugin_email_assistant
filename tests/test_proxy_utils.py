import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.imap_client import ImapMailbox
from astrbot_plugin_email_assistant.proxy_utils import (
    ProxyConfig,
    create_connection,
    proxy_config_from_account,
)
from astrbot_plugin_email_assistant.smtp_client import _connect as smtp_connect


class ProxyUtilsTests(unittest.TestCase):
    def test_proxy_settings_are_account_scoped(self):
        first = proxy_config_from_account(
            {
                "proxy_type": "socks5",
                "proxy_host": "127.0.0.1",
                "proxy_port": 1080,
                "proxy_username": "user-a",
                "proxy_password": "secret-a",
                "proxy_dns": True,
            }
        )
        second = proxy_config_from_account(
            {"proxy_type": "http", "proxy_host": "10.0.0.2", "proxy_port": 8080}
        )
        self.assertEqual((first.kind, first.host, first.username), ("socks5", "127.0.0.1", "user-a"))
        self.assertEqual((second.kind, second.host, second.port), ("http", "10.0.0.2", 8080))

    def test_invalid_enabled_proxy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "主机和端口"):
            proxy_config_from_account({"proxy_type": "socks5", "proxy_host": "", "proxy_port": 1080})

    def test_pysocks_connection_receives_auth_and_remote_dns(self):
        fake_socks = Mock(HTTP=1, SOCKS4=2, SOCKS5=3)
        fake_socket = object()
        fake_socks.create_connection.return_value = fake_socket
        proxy = ProxyConfig("socks5", "proxy.local", 1080, "alice", "password", True)
        with patch("astrbot_plugin_email_assistant.proxy_utils._load_socks", return_value=fake_socks):
            result = create_connection(("imap.example.com", 993), proxy=proxy, timeout=12)
        self.assertIs(result, fake_socket)
        fake_socks.create_connection.assert_called_once_with(
            ("imap.example.com", 993),
            timeout=12,
            proxy_type=3,
            proxy_addr="proxy.local",
            proxy_port=1080,
            proxy_rdns=True,
            proxy_username="alice",
            proxy_password="password",
        )

    def test_imap_uses_proxy_specific_client(self):
        account = {
            "email": "bot@example.com",
            "password": "mail-secret",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
            "folder": "INBOX",
            "proxy_type": "socks5",
            "proxy_host": "proxy-a",
            "proxy_port": 1080,
        }
        client = Mock()
        client.select.return_value = ("OK", [])
        with patch("astrbot_plugin_email_assistant.imap_client._ProxyIMAP4SSL", return_value=client) as constructor:
            with ImapMailbox(account):
                pass
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs["proxy"].host, "proxy-a")
        client.login.assert_called_once_with("bot@example.com", "mail-secret")

    def test_smtp_uses_proxy_specific_client(self):
        account = {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_security": "ssl",
            "proxy_type": "http",
            "proxy_host": "proxy-b",
            "proxy_port": 3128,
        }
        client = object()
        with patch("astrbot_plugin_email_assistant.smtp_client._ProxySMTPSSL", return_value=client) as constructor:
            result = smtp_connect(account, 20)
        self.assertIs(result, client)
        self.assertEqual(constructor.call_args.kwargs["proxy"].host, "proxy-b")


if __name__ == "__main__":
    unittest.main()

