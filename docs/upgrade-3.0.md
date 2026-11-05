# 升级到 3.0.0

3.0.0 将精简后的七个 `email_assistant_` 工具作为公共契约发布，并提供附件收发与数据库 v2。升级不需要删除数据库。

## 工具与配置

显式配置了人格工具白名单、工作流或工具调用模板时，使用以下映射。旧名称不注册兼容别名。

| 2.x 工具 | 3.0.0 调用 |
|---|---|
| `email_assistant_list_messages` | `email_assistant_query_mailbox(operation="list")` |
| `email_assistant_get_latest_message` | `email_assistant_query_mailbox(operation="latest")` |
| `email_assistant_show_message` | `email_assistant_query_mailbox(operation="read", uid=...)` |
| `email_assistant_summarize_message` | `email_assistant_process_message(task="summary", uid=...)` |
| `email_assistant_translate_message` | `email_assistant_process_message(task="translate", uid=...)` |

其余工具保持 `email_assistant_list_accounts`、`email_assistant_create_draft`、`email_assistant_create_reply_draft`、`email_assistant_confirm_send`、`email_assistant_cancel_draft`。插件不增加独立附件工具：查询工具提供 `attachments`、`download_attachment` 操作，两个创建草稿工具接受 `attachments` 字符串数组。工具总数固定为七个；运行时仍按账户能力、草稿状态和真实确认消息裁剪。

`storage_settings.body_cache_max_item_kb` 已移除，旧配置中的该值不会生效。完整正文只受 `body_cache_max_total_mb` 总预算约束，单封超过总预算仍可读取但不缓存。显示与模型输入的字符限制保持独立。

新增配置：

- `storage_settings.attachment_retention_days`：默认 90；
- `storage_settings.attachment_cache_max_total_mb`：默认 1024 MiB（1 GiB），所有账户共用；
- `notification_settings.official_history_placeholder`：默认空白，即使用内置占位；仅填写 `{none}`（允许首尾空白）时写入空用户内容。

空用户内容可以由 AstrBot 消息模型、数据库和历史解析保存，但上游模型是否接受空输入取决于 Provider 和服务端。若出现空内容拒绝，改用非空占位；插件不会修改宿主或全局 Provider 请求规则。占位设置只影响直接 LLM 转述的历史归档，不影响 Cron。

本机 AstrBot 4.29.3 已通过真实 ConversationManager 和 SQLite 验证空内容写入及重新解析；离线检查中 OpenAI、Anthropic 适配器保留空用户内容，Gemini 适配器在出站请求层转换为单个空格，数据库仍保存 `""`。这项检查未请求上游模型，不能据此保证所有服务商接受空输入。

## 数据升级与恢复

升级前停止插件或 AstrBot，备份插件配置、AstrBot 保存的插件 KV 状态以及整个 `data/plugin_data/astrbot_plugin_email_assistant/`。正文、草稿、附件和凭据可能分别位于这些位置；只备份 SQLite 不足以恢复附件文件和通知游标。

启动时检测 `db_version`：

1. 新数据库直接创建 v2。
2. 已发布 v1 数据库先通过 SQLite backup API 生成同目录 `mail_headers.db.v1-backup-<随机标识>.db`，进行完整性检查，再开启迁移事务。
3. 同一次事务内增加附件元数据表和草稿附件引用字段，清除已截断的正文缓存，记录 v2；其余已存在数据保留。正文缓存之后按需重新读取。
4. 任一步失败即回滚。原数据库和备份保留，索引及草稿功能暂不可用；查看启动日志，解决磁盘空间或权限等原因后重启。不要删除原库。

没有版本表或版本未知的数据库不会自动猜测迁移，应保留原文件，在能够读取它的对应版本中导出或确认恢复途径。已发布 v1 的迁移使用独立 SQL 夹具验证；一个实际发布周期内的 schema 调整只形成一次版本迁移。

降级恢复需先停止插件，另外保留当前 v2 数据库、附件目录及相关 `-wal`/`-shm` 文件，然后把经检查的 v1 备份作为旧版本的 `mail_headers.db` 使用，避免旧 WAL 与恢复文件混用。恢复到旧快照后，升级期间新增的草稿和附件不会出现在旧版本中；要保留它们，应先在 v2 中导出需要的内容。不要让 2.x 直接打开 v2 数据库。

迁移备份不自动删除，也不计入正文或附件预算。确认升级和自己的收发验证通过后，再按个人备份策略保留或归档。SQLite 与附件目录本身未加密，需依靠宿主文件权限、磁盘加密及备份权限保护。

## 附件与宿主要求

正文与附件分开读取。附件下载通过 UIDVALIDITY、邮件 UID 和 MIME part 验证远端来源，分块写入受控目录；草稿发送前检查本地文件大小和 SHA-256。待处理/失败草稿和正在传输的附件不会被缓存清理删除，但全部计入总配额。空间不足会拒绝新增附件；取消、删除或发送草稿后才解除对应的长期保护。

Bot 附件上传来源是平台交付的 File/Image 组件，文件下载到聊天依赖平台支持 File 发送。工具不能用模型提供的任意路径或 URL 读取宿主文件。

WebUI 要求宿主 Plugin Page Bridge 支持 `upload`、`download`，后端请求支持 multipart 文件以及 ASGI 二进制 `FileResponse` 返回。仅能展示 Plugin Page 的旧宿主并不保证附件收发可用；请使用具备这些接口的 AstrBot 版本。页面通过宿主认证操作个人邮箱，不提供多用户账户隔离。

浏览器、宿主 multipart 解析、平台文件接收可能先创建插件目录以外的临时副本；附件配额只约束插件管理的目录和暂存文件。大附件的实际使用还受宿主上传限制、可用内存、服务商单封邮件大小及 MIME 编码开销限制。

## 离线验证命令

在插件目录使用具备项目依赖的 Python 虚拟环境运行 `python -m pytest -q`。已安装 Playwright 及 Chromium/WebKit 时，执行 `python tests/browser_attachment_workflow.py --help` 查看浏览器检查参数。

真实宿主契约检查独立于普通测试集，使用对应 AstrBot 虚拟环境运行：

```sh
python scripts/verify_astrbot_contracts.py --astrbot-root /path/to/AstrBot
```

该脚本将宿主配置与数据库隔离在临时目录，检查七个工具、附件参数、空用户历史及文件组件，不连接邮箱或请求模型。脚本依赖所指向宿主的内部接口，升级 AstrBot 后可重新运行检查。

## 升级后的核验

确认版本和数据库为 3.0.0 / v2、已有索引和草稿可读、通知游标没有重置。再用自己的测试邮箱核对：邮件正文与附件下载、多附件草稿修改后的重新审核、一次性确认、回复线程及失败恢复。手机浏览器需检查文件选择器和下载行为。自动化测试使用模拟 IMAP/SMTP 与页面桥接，不替代真实邮箱服务商和消息平台的联调。
