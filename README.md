# AstrBot Email Assistant

一个面向 AstrBot 的多账户邮件助手。v1 支持：

- 使用 IMAP SSL/STARTTLS 自动轮询新邮件，并把邮件标题发给绑定的私聊用户；
- 手动检查、按日期查询邮件列表、按 UID 查看正文详情；
- 使用 SMTP SSL/STARTTLS 发送纯文本邮件；
- 根据原邮件 `Reply-To`、`Message-ID` 和 `References` 发送线程回复；
- 每个邮箱独立开关接收、查询和发送权限。

v1 **不会调用 LLM，也不会写入 AstrBot 官方对话历史**。邮件正文只在用户主动执行详情命令时显示。

## 配置

在 AstrBot 插件配置页添加 `mail_accounts`。每个账户至少配置：

- `account_id`：稳定且唯一的账户标识，例如 `personal_qq`；
- `owner_umo`：绑定用户完整的私聊 `unified_msg_origin`，例如 `aiocqhttp:FriendMessage:12345678`；
- `email`、`username`、`password`；
- IMAP 主机、端口和安全模式；
- SMTP 主机、端口和安全模式。

不要填写邮箱网页登录密码。优先使用邮箱服务商签发的应用密码或客户端授权码。配置文件会保存凭据，请限制 AstrBot 数据目录的文件权限，不要把真实配置提交到 Git。

同一个 `owner_umo` 可以绑定多个账户；每个账户只能绑定一个私聊用户。管理员 UID 可以在私聊中管理全部账户，普通用户只能操作绑定给自己的账户。

首次启用接收时，插件只保存当前最大 IMAP UID 作为基线，不会推送历史邮件。关闭接收后游标会保留，重新开启会继续处理停用期间积累的新邮件，单次处理数量受 `max_fetch_per_check` 限制。

## 常见服务器配置

| 服务 | IMAP SSL | SMTP SSL | SMTP STARTTLS |
|---|---|---|---|
| QQ 邮箱 | `imap.qq.com:993` | `smtp.qq.com:465` | `smtp.qq.com:587` |
| Gmail | `imap.gmail.com:993` | `smtp.gmail.com:465` | `smtp.gmail.com:587` |
| Outlook / Microsoft 365 | `outlook.office365.com:993` | — | `smtp.office365.com:587` |

服务器设置可能调整，请以邮箱服务商当前文档为准。Gmail、Microsoft 365 等服务可能要求应用密码或 OAuth；v1 只支持用户名和应用密码认证，不支持 OAuth。

## 命令

所有命令只能在私聊中执行。账户参数可使用 `account_id` 或唯一显示名称；建议始终使用 `account_id`。

```text
/email help
/email status [账户]
/email test [账户]
/email check [账户]
/email list <账户> [YYYY-MM-DD]
/email show <账户> <UID>
/email send <账户> <收件人> <主题>|<正文>
/email reply <账户> <UID> <正文>
```

示例：

```text
/email list personal_qq 2026-07-01
/email show personal_qq 1288
/email send personal_qq user@example.com 测试邮件|你好，这是一封测试邮件。
/email reply personal_qq 1288 已收到，谢谢。
```

`/email test` 只登录并执行协议级 NOOP，不会发送测试邮件。v1 不支持附件、抄送、密送或附件下载。

## 新邮件通知

轮询发现新邮件后只发送：

```text
📧 [账户名] 新邮件：邮件标题
```

通知成功后才推进该账户的 UID 游标。发送失败时保留游标并在下次轮询重试；无法解析的损坏邮件会被记录并跳过，避免阻塞整个邮箱。

## 后续版本

邮件解析、通知构造和消息发送相互独立。后续可以在通知构造阶段加入当前人格 LLM 转述，并在发送成功后把“邮件主动承接占位 + assistant 回复”写入 AstrBot 官方历史，而不改动 IMAP/SMTP 层。

