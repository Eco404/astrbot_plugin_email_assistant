from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain

from .account_utils import (
    account_owner_user_id,
    account_target_platform,
    command_payload,
    is_admin,
    parse_reply_payload,
    parse_send_payload,
    resolve_account,
    visible_accounts,
)
from .imap_client import fetch_after_uid, fetch_detail, get_max_uid, query_since, test_imap
from .smtp_client import send_mail, send_reply, test_smtp


PLUGIN_NAME = "astrbot_plugin_email_assistant"


def _safe_int(value: Any, default: int, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _one_line(value: Any, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


@register(
    PLUGIN_NAME,
    "econeco",
    "支持多账户 IMAP 收信通知、查询以及 SMTP 发送和回复的邮件助手",
    "1.2.0",
)
class EmailAssistantPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._status: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="email_assistant_poll")
        logger.info("[EmailAssistant] 后台邮件轮询已启动。")

    async def terminate(self) -> None:
        self._stop_event.set()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[EmailAssistant] 插件已停止。")

    def _accounts(self) -> list[dict[str, Any]]:
        raw = self.config.get("mail_accounts", [])
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _account_key(self, account: dict[str, Any]) -> str:
        account_id = _one_line(account.get("account_id"), 80)
        if account_id:
            return account_id
        address = _one_line(account.get("email"), 120)
        return hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]

    def _validate_account(self, account: dict[str, Any], *, require_owner: bool = False) -> str:
        account_id = _one_line(account.get("account_id"), 80)
        if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            return "account_id 不能为空，且只能包含字母、数字、下划线和短横线。"
        duplicates = [item for item in self._accounts() if _one_line(item.get("account_id"), 80) == account_id]
        if len(duplicates) != 1:
            return f"account_id“{account_id}”不唯一，请修正配置。"
        if require_owner and not account_owner_user_id(account):
            return "账户未绑定私聊目标用户 ID。"
        return ""

    def _cursor_key(self, account: dict[str, Any]) -> str:
        folder = _one_line(account.get("folder") or "INBOX", 120)
        digest = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:12]
        return f"email_assistant:last_uid:{self._account_key(account)}:{digest}"

    def _lock_for(self, account: dict[str, Any]) -> asyncio.Lock:
        return self._account_locks.setdefault(self._account_key(account), asyncio.Lock())

    def _timeout(self) -> int:
        return _safe_int(self.config.get("network_timeout_seconds", 20), 20, 5, 120)

    def _fetch_limit(self) -> int:
        return _safe_int(self.config.get("max_fetch_per_check", 20), 20, 1, 100)

    def _query_limit(self) -> int:
        return _safe_int(self.config.get("max_query_results", 20), 20, 1, 50)

    @staticmethod
    def _display_name(account: dict[str, Any]) -> str:
        return _one_line(account.get("name") or account.get("email") or account.get("account_id") or "邮箱", 80)

    def _record_status(self, account: dict[str, Any], *, ok: bool, detail: str) -> None:
        self._status[self._account_key(account)] = {
            "ok": bool(ok),
            "detail": _one_line(detail, 180),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _poll_loop(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop_event.is_set():
            for account in self._accounts():
                if self._stop_event.is_set():
                    break
                if not account.get("enabled", True) or not account.get("receive_enabled", True):
                    continue
                if not account_owner_user_id(account):
                    continue
                try:
                    await self._check_account(account)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_status(account, ok=False, detail=f"{type(exc).__name__}: {exc}")
                    logger.warning(
                        "[EmailAssistant] 账户 %s 检查失败: %s",
                        self._display_name(account),
                        _one_line(exc, 180),
                    )
            interval = _safe_int(self.config.get("poll_interval_seconds", 60), 60, 30, 86400)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _platform_meta_value(meta: Any, key: str) -> str:
        if isinstance(meta, dict):
            return str(meta.get(key) or "").strip()
        return str(getattr(meta, key, "") or "").strip()

    def _platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        try:
            return list(manager.get_insts())
        except Exception:
            return list(getattr(manager, "platform_insts", []) or [])

    def _resolve_notification_umo(self, account: dict[str, Any]) -> str:
        owner_id = _one_line(account_owner_user_id(account), 160)
        if not owner_id:
            raise ValueError("账户未绑定私聊目标用户 ID。")
        selector = _one_line(account_target_platform(account), 120)
        manager = getattr(self.context, "platform_manager", None)
        platforms = self._platform_instances()
        if manager is None:
            legacy_umo = _one_line(account.get("owner_umo"), 240)
            if legacy_umo:
                return legacy_umo
            return f"{selector}:FriendMessage:{owner_id}"
        if not platforms:
            raise RuntimeError("当前没有已加载的消息平台，无法发送新邮件通知。")

        matches: list[tuple[Any, str, str]] = []
        for platform in platforms:
            try:
                meta = platform.meta()
            except Exception:
                continue
            platform_id = self._platform_meta_value(meta, "id")
            platform_name = self._platform_meta_value(meta, "name")
            if selector in {platform_id, platform_name} or selector.lower() == platform_name.lower():
                matches.append((platform, platform_id, platform_name))
        if not matches:
            raise RuntimeError(
                f"未找到目标平台“{selector}”，请确认平台已启用，或填写平台实例 ID。"
            )
        exact_id_matches = [item for item in matches if item[1] == selector]
        if exact_id_matches:
            matches = exact_id_matches
        if len(matches) > 1:
            ids = "、".join(item[1] for item in matches if item[1])
            raise RuntimeError(
                f"目标平台“{selector}”匹配到多个实例，请在配置中填写平台实例 ID：{ids}"
            )
        platform_id = matches[0][1]
        if not platform_id:
            raise RuntimeError(f"目标平台“{selector}”没有可用的实例 ID。")
        return f"{platform_id}:FriendMessage:{owner_id}"

    def _account_matches_event_platform(self, account: dict[str, Any], umo: str) -> bool:
        event_platform_id = str(umo or "").split(":", 1)[0].strip()
        if not event_platform_id:
            return False
        selector = account_target_platform(account)
        if event_platform_id == selector:
            return True
        for platform in self._platform_instances():
            try:
                meta = platform.meta()
            except Exception:
                continue
            platform_id = self._platform_meta_value(meta, "id")
            platform_name = self._platform_meta_value(meta, "name")
            if event_platform_id == platform_id and (
                selector in {platform_id, platform_name}
                or selector.lower() == platform_name.lower()
            ):
                return True
        legacy_umo = str(account.get("owner_umo") or "").strip()
        return bool(legacy_umo and legacy_umo == str(umo or "").strip())

    async def _send_title_notification(self, account: dict[str, Any], subject: str) -> None:
        owner_umo = self._resolve_notification_umo(account)
        title = _one_line(subject or "(无主题)", 300)
        text = f"📧 [{self._display_name(account)}] 新邮件：{title}"
        sent = await self.context.send_message(owner_umo, MessageChain([Plain(text)]))
        if sent is False:
            raise RuntimeError(f"AstrBot 未找到目标平台，会话 {owner_umo} 未发送。")

    async def _check_account(self, account: dict[str, Any]) -> tuple[int, bool]:
        validation_error = self._validate_account(account, require_owner=True)
        if validation_error:
            raise ValueError(validation_error)
        if not account.get("enabled", True):
            raise ValueError("账户已关闭。")
        if not account.get("receive_enabled", True):
            raise ValueError("账户接收功能已关闭。")
        async with self._lock_for(account):
            cursor_key = self._cursor_key(account)
            last_uid = await self.get_kv_data(cursor_key, None)
            if last_uid is None:
                baseline = await asyncio.to_thread(get_max_uid, account, self._timeout())
                await self.put_kv_data(cursor_key, int(baseline))
                self._record_status(account, ok=True, detail=f"已建立 UID 基线 {baseline}")
                return 0, True
            try:
                last_uid_int = max(0, int(last_uid))
            except (TypeError, ValueError):
                last_uid_int = 0
            items = await asyncio.to_thread(
                fetch_after_uid,
                account,
                last_uid_int,
                self._fetch_limit(),
                self._timeout(),
            )
            sent = 0
            for item in items:
                if item.mail is None:
                    logger.warning(
                        "[EmailAssistant] 跳过无法解析的邮件 account=%s uid=%s error=%s",
                        self._account_key(account),
                        item.uid,
                        _one_line(item.error, 160),
                    )
                    await self.put_kv_data(cursor_key, int(item.uid))
                    continue
                await self._send_title_notification(account, item.mail.subject)
                await self.put_kv_data(cursor_key, int(item.uid))
                sent += 1
            self._record_status(account, ok=True, detail=f"检查完成，新邮件 {sent} 封")
            return sent, False

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return str(getattr(event, "sender_id", "") or "").strip()

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        try:
            if event.get_message_type() == MessageType.FRIEND_MESSAGE:
                return True
        except Exception:
            pass
        try:
            if bool(event.is_private_chat()):
                return True
        except Exception:
            pass
        return "FriendMessage" in str(getattr(event, "unified_msg_origin", "") or "")

    def _visible_accounts(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        accounts = visible_accounts(
            self.config,
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            sender_id=self._sender_id(event),
        )
        if is_admin(self._sender_id(event), self.config):
            return accounts
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return [item for item in accounts if self._account_matches_event_platform(item, umo)]

    async def _guard_private(self, event: AstrMessageEvent) -> bool:
        if self._is_private(event):
            return True
        return False

    def _resolve_for_event(self, event: AstrMessageEvent, selector: str):
        account, error = resolve_account(self._visible_accounts(event), selector)
        if account is not None:
            validation_error = self._validate_account(account)
            if validation_error:
                return None, validation_error
        return account, error

    @filter.command_group("email", alias={"邮箱"})
    def email_group(self):
        """管理邮箱账户，检查、查询、发送和回复邮件"""
        pass

    @email_group.command("help", alias={"帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看邮件助手命令帮助"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        yield event.plain_result(
            "📮 Email Assistant\n"
            "/email status [账户]\n"
            "/email test [账户]\n"
            "/email check [账户]\n"
            "/email list <账户> [YYYY-MM-DD]\n"
            "/email show <账户> <UID>\n"
            "/email send <账户> <收件人> <主题>|<正文>\n"
            "/email reply <账户> <UID> <正文>"
        )

    @email_group.command("status", alias={"状态"})
    async def cmd_status(self, event: AstrMessageEvent, account: str = ""):
        """查看可用邮箱及最近检查状态"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        accounts = self._visible_accounts(event)
        if account:
            selected, error = resolve_account(accounts, account)
            if not selected:
                yield event.plain_result(f"❌ {error}")
                return
            accounts = [selected]
        if not accounts:
            yield event.plain_result("📭 当前用户没有可用的邮箱账户。")
            return
        lines = ["📊 邮箱状态"]
        for item in accounts:
            status = self._status.get(self._account_key(item), {})
            state = "✅" if status.get("ok") else ("❌" if status else "⏳")
            abilities = "/".join(
                name
                for name, enabled in (
                    ("收", item.get("receive_enabled", True)),
                    ("查", item.get("query_enabled", True)),
                    ("发", item.get("send_enabled", True)),
                )
                if enabled
            ) or "无"
            lines.append(
                f"{state} {self._display_name(item)} ({self._account_key(item)}) [{abilities}]\n"
                f"   {status.get('detail') or '尚未检查'} {status.get('checked_at') or ''}".rstrip()
            )
        yield event.plain_result("\n".join(lines))

    @email_group.command("test", alias={"测试"})
    async def cmd_test(self, event: AstrMessageEvent, account: str = ""):
        """测试邮箱的 IMAP 和 SMTP 登录"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        selected, error = self._resolve_for_event(event, account)
        if not selected:
            yield event.plain_result(f"❌ {error}")
            return
        results: list[str] = []
        if selected.get("receive_enabled", True) or selected.get("query_enabled", True):
            try:
                await asyncio.to_thread(test_imap, selected, self._timeout())
                results.append("✅ IMAP 登录成功")
            except Exception as exc:
                results.append(f"❌ IMAP: {_one_line(exc)}")
        else:
            results.append("⏭️ IMAP 接收和查询均已关闭")
        if selected.get("send_enabled", True):
            try:
                await asyncio.to_thread(test_smtp, selected, self._timeout())
                results.append("✅ SMTP 登录成功")
            except Exception as exc:
                results.append(f"❌ SMTP: {_one_line(exc)}")
        else:
            results.append("⏭️ SMTP 发送已关闭")
        yield event.plain_result(f"🧪 {self._display_name(selected)}\n" + "\n".join(results))

    @email_group.command("check", alias={"检查"})
    async def cmd_check(self, event: AstrMessageEvent, account: str = ""):
        """立即检查新邮件并通知绑定用户"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        accounts = self._visible_accounts(event)
        if account:
            selected, error = resolve_account(accounts, account)
            if not selected:
                yield event.plain_result(f"❌ {error}")
                return
            accounts = [selected]
        accounts = [item for item in accounts if item.get("receive_enabled", True)]
        if not accounts:
            yield event.plain_result("📭 没有启用接收功能的可用邮箱。")
            return
        lines: list[str] = []
        for item in accounts:
            try:
                count, baseline = await self._check_account(item)
                detail = "已建立基线，不推送历史邮件" if baseline else f"新邮件 {count} 封"
                lines.append(f"✅ {self._display_name(item)}：{detail}")
            except Exception as exc:
                self._record_status(item, ok=False, detail=str(exc))
                lines.append(f"❌ {self._display_name(item)}：{_one_line(exc)}")
        yield event.plain_result("🔍 检查完成\n" + "\n".join(lines))

    @email_group.command("list", alias={"列表"})
    async def cmd_list(self, event: AstrMessageEvent, account: str, since_date: str = ""):
        """列出邮箱中指定日期以来的邮件"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        selected, error = self._resolve_for_event(event, account)
        if not selected:
            yield event.plain_result(f"❌ {error}")
            return
        if not selected.get("query_enabled", True):
            yield event.plain_result("❌ 该邮箱的查询功能已关闭。")
            return
        try:
            since = datetime.strptime(since_date, "%Y-%m-%d") if since_date else datetime.now() - timedelta(days=7)
        except ValueError:
            yield event.plain_result("❌ 日期格式应为 YYYY-MM-DD。")
            return
        try:
            mails = await asyncio.to_thread(query_since, selected, since, self._query_limit(), self._timeout())
        except Exception as exc:
            yield event.plain_result(f"❌ 查询失败：{_one_line(exc)}")
            return
        if not mails:
            yield event.plain_result("📭 指定日期范围内没有邮件。")
            return
        lines = [f"📬 {self._display_name(selected)}（{since.strftime('%Y-%m-%d')} 起）"]
        for mail in mails:
            sender = mail.from_name or mail.from_addr or "未知发件人"
            lines.append(f"UID {mail.uid}｜{mail.date}\n{_one_line(sender, 80)}｜{_one_line(mail.subject, 160)}")
        yield event.plain_result("\n\n".join(lines))

    @email_group.command("show", alias={"详情"})
    async def cmd_show(self, event: AstrMessageEvent, account: str, uid: int):
        """查看指定 UID 邮件的详情和正文摘要"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        selected, error = self._resolve_for_event(event, account)
        if not selected:
            yield event.plain_result(f"❌ {error}")
            return
        if not selected.get("query_enabled", True):
            yield event.plain_result("❌ 该邮箱的查询功能已关闭。")
            return
        try:
            mail = await asyncio.to_thread(fetch_detail, selected, int(uid), self._timeout())
        except Exception as exc:
            yield event.plain_result(f"❌ 获取邮件失败：{_one_line(exc)}")
            return
        limit = _safe_int(self.config.get("detail_body_max_chars", 4000), 4000, 200, 12000)
        body = mail.body_preview(limit) or "（无可显示的纯文本正文）"
        attachment = "有" if mail.has_attachments else "无"
        yield event.plain_result(
            f"📨 UID {mail.uid}\n"
            f"主题：{mail.subject}\n"
            f"发件人：{mail.from_name or '-'} <{mail.from_addr or '-'}>\n"
            f"回复地址：{mail.reply_to or '-'}\n"
            f"时间：{mail.date}\n附件：{attachment}\n\n{body}"
        )

    @email_group.command("send", alias={"发送"})
    async def cmd_send(self, event: AstrMessageEvent):
        """使用指定邮箱发送纯文本邮件"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        try:
            account_name, recipient, subject, body = parse_send_payload(command_payload(event.message_str, "send"))
        except ValueError as exc:
            yield event.plain_result(
                f"❌ {exc}\n用法：/email send <账户> <收件人> <主题>|<正文>"
            )
            return
        selected, error = self._resolve_for_event(event, account_name)
        if not selected:
            yield event.plain_result(f"❌ {error}")
            return
        if not selected.get("send_enabled", True):
            yield event.plain_result("❌ 该邮箱的发送功能已关闭。")
            return
        try:
            await asyncio.to_thread(send_mail, selected, recipient, subject, body, self._timeout())
        except Exception as exc:
            yield event.plain_result(f"❌ 发送失败：{_one_line(exc)}")
            return
        yield event.plain_result(f"✅ 邮件已发送\n收件人：{recipient}\n主题：{subject}")

    @email_group.command("reply", alias={"回复"})
    async def cmd_reply(self, event: AstrMessageEvent):
        """回复指定 UID 的邮件并保留线程信息"""
        if not await self._guard_private(event):
            yield event.plain_result("❌ 邮件命令只能在私聊中使用。")
            return
        try:
            account_name, uid, body = parse_reply_payload(command_payload(event.message_str, "reply"))
        except ValueError as exc:
            yield event.plain_result(f"❌ {exc}\n用法：/email reply <账户> <UID> <正文>")
            return
        selected, error = self._resolve_for_event(event, account_name)
        if not selected:
            yield event.plain_result(f"❌ {error}")
            return
        if not selected.get("send_enabled", True):
            yield event.plain_result("❌ 该邮箱的发送功能已关闭。")
            return
        if not selected.get("query_enabled", True):
            yield event.plain_result("❌ 回复前需要启用该邮箱的查询功能。")
            return
        try:
            original = await asyncio.to_thread(fetch_detail, selected, uid, self._timeout())
            await asyncio.to_thread(send_reply, selected, original, body, self._timeout())
        except Exception as exc:
            yield event.plain_result(f"❌ 回复失败：{_one_line(exc)}")
            return
        reply_subject = original.subject if original.subject.lower().startswith("re:") else f"Re: {original.subject}"
        yield event.plain_result(
            f"✅ 回复已发送\n收件人：{original.reply_to or original.from_addr}\n主题：{reply_subject}"
        )
