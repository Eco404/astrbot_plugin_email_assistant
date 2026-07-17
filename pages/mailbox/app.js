import { renderMarkdown } from "./markdown.js?v=2.2.3";

const state = {
  bridge: null,
  context: {},
  accounts: [],
  accountId: "",
  folders: [],
  folder: "",
  messages: [],
  nextCursor: "",
  hasMore: false,
  selectedUid: null,
  drafts: [],
  draft: null,
  draftDirty: false,
  autoShowCachedAi: "none",
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  accountList: $("#account-list"),
  mobileAccount: $("#mobile-account"),
  mobileFolder: $("#mobile-folder"),
  folderList: $("#folder-list"),
  runtimeState: $("#runtime-state"),
  syncButton: $("#sync-button"),
  messageList: $("#message-list"),
  messageDetail: $("#message-detail"),
  loadMore: $("#load-more-button"),
  syncWarning: $("#sync-warning"),
  mailSearch: $("#mail-search"),
  mailSince: $("#mail-since"),
  draftList: $("#draft-list"),
  draftAccount: $("#draft-account"),
  draftTo: $("#draft-to"),
  draftCc: $("#draft-cc"),
  draftBcc: $("#draft-bcc"),
  draftSubject: $("#draft-subject"),
  draftBody: $("#draft-body"),
  draftHeading: $("#draft-heading"),
  draftStatus: $("#draft-status"),
  saveDraft: $("#save-draft-button"),
  approveDraft: $("#approve-draft-button"),
  sendDraft: $("#send-draft-button"),
  deleteDraft: $("#delete-draft-button"),
};

function unwrap(response) {
  if (response?.status === "error" || response?.success === false) {
    throw new Error(response.message || response.error || "请求失败");
  }
  if (response?.status === "ok" && Object.hasOwn(response, "data")) return response.data;
  if (response?.success === true && Object.hasOwn(response, "data")) return response.data;
  return response || {};
}

async function apiGet(path, params = {}) {
  return unwrap(await state.bridge.apiGet(path, params));
}

async function apiPost(path, body = {}) {
  return unwrap(await state.bridge.apiPost(path, body));
}

function setBusy(button, busy, label = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.previousText = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.previousText || button.textContent;
    button.disabled = false;
  }
}

let toastTimer;
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = String(message || "");
  node.classList.toggle("error", error);
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 4200);
}

function confirmAction(title, message, confirmLabel = "确认") {
  const modal = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-message").textContent = message;
  $("#modal-confirm").textContent = confirmLabel;
  modal.classList.remove("hidden");
  return new Promise((resolve) => {
    const finish = (value) => {
      modal.classList.add("hidden");
      $("#modal-confirm").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(value);
    };
    $("#modal-confirm").onclick = () => finish(true);
    $("#modal-cancel").onclick = () => finish(false);
  });
}

function formatDate(timestamp, fallback = "") {
  if (!timestamp) return fallback || "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}

function draftStatusLabel(status) {
  return ({
    editing: "编辑中", pending_review: "待审核", approved: "已审核",
    sending: "发送中", sent: "已发送", failed: "发送失败", cancelled: "已取消",
  })[status] || status || "未保存";
}

function applyTheme(context = {}) {
  const theme = typeof context.isDark === "boolean"
    ? (context.isDark ? "dark" : "light")
    : (context.theme || context.colorScheme);
  if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
}

function applyContext(context = {}) {
  applyTheme(context);
  if (state.bridge?.t) document.title = state.bridge.t("pages.mailbox.title", "邮件中心");
}

async function loadOverview() {
  const data = await apiGet("overview");
  state.accounts = data.accounts || [];
  if (!state.accountId || !state.accounts.some((item) => item.account_id === state.accountId)) {
    state.accountId = state.accounts.find((item) => item.query_enabled)?.account_id || state.accounts[0]?.account_id || "";
  }
  renderAccounts();
  renderDraftAccountOptions();
  const enabled = Boolean(data.plugin?.index_enabled);
  state.autoShowCachedAi = data.plugin?.webui_auto_show_cached_ai || "none";
  elements.runtimeState.textContent = enabled ? `${state.accounts.length} 个账户 · 索引就绪` : "本地索引不可用";
  elements.runtimeState.classList.toggle("error", !enabled);
}

function renderAccounts() {
  elements.accountList.replaceChildren();
  elements.mobileAccount.replaceChildren();
  if (!state.accounts.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "暂无已启用账户";
    elements.accountList.append(empty);
    return;
  }
  for (const account of state.accounts) {
    if (account.query_enabled) {
      const option = document.createElement("option");
      option.value = account.account_id;
      option.textContent = account.name;
      elements.mobileAccount.append(option);
    }
    const button = document.createElement("button");
    button.className = `account-button${account.account_id === state.accountId ? " active" : ""}`;
    const name = document.createElement("strong");
    const dot = document.createElement("span");
    dot.className = `account-dot${account.runtime_status?.ok === false ? " off" : ""}`;
    name.append(dot, document.createTextNode(account.name));
    const meta = document.createElement("small");
    meta.textContent = `${account.account_id} · ${account.index?.active || 0} 封`;
    button.append(name, meta);
    button.disabled = !account.query_enabled;
    if (!account.query_enabled) button.title = "该账户的查询功能已关闭";
    button.onclick = async () => {
      state.accountId = account.account_id;
      state.folder = "";
      state.selectedUid = null;
      renderAccounts();
      await loadFolders();
      await loadMessages(true);
    };
    elements.accountList.append(button);
  }
  elements.mobileAccount.value = state.accountId;
}

function currentAccount() {
  return state.accounts.find((item) => item.account_id === state.accountId);
}

async function loadFolders(refresh = false) {
  if (!state.accountId) return;
  if (refresh) await apiPost("folders/refresh", { account_id: state.accountId });
  const data = await apiGet("folders", { account_id: state.accountId });
  if (data.warning) toast(data.warning, true);
  state.folders = (data.items || []).filter((item) => item.selectable);
  if (!state.folder || !state.folders.some((item) => item.name === state.folder)) {
    state.folder = state.folders.find((item) => item.name === data.primary_folder)?.name
      || state.folders[0]?.name || data.primary_folder || "INBOX";
  }
  renderFolders();
}

function renderFolders() {
  elements.folderList.replaceChildren();
  elements.mobileFolder.replaceChildren();
  for (const folder of state.folders) {
    const option = document.createElement("option");
    option.value = folder.name;
    option.textContent = folder.display_name || folder.name;
    elements.mobileFolder.append(option);
    const button = document.createElement("button");
    button.className = `account-button${folder.name === state.folder ? " active" : ""}`;
    const name = document.createElement("strong");
    name.textContent = folder.display_name || folder.name;
    const meta = document.createElement("small");
    meta.textContent = `${folder.name} · ${folder.stats?.active || 0} 封`;
    button.append(name, meta);
    button.onclick = async () => {
      state.folder = folder.name;
      state.selectedUid = null;
      renderFolders();
      await loadMessages(true);
    };
    elements.folderList.append(button);
  }
  elements.mobileFolder.value = state.folder;
}

function renderDraftAccountOptions() {
  const current = elements.draftAccount.value;
  elements.draftAccount.replaceChildren();
  for (const account of state.accounts.filter((item) => item.send_enabled)) {
    const option = document.createElement("option");
    option.value = account.account_id;
    option.textContent = `${account.name} (${account.account_id})`;
    elements.draftAccount.append(option);
  }
  elements.draftAccount.value = current || state.accountId;
}

async function loadMessages(reset = true) {
  if (!state.accountId) {
    elements.messageList.replaceChildren(emptyNode("没有可查询的邮箱账户。"));
    return;
  }
  if (reset) {
    state.messages = [];
    state.nextCursor = "";
    elements.messageList.replaceChildren(emptyNode("正在读取本地索引…", "loading"));
  }
  try {
    const data = await apiGet("messages", {
      account_id: state.accountId,
      folder: state.folder,
      q: elements.mailSearch.value.trim(),
      since: elements.mailSince.value,
      cursor: reset ? "" : state.nextCursor,
      limit: 50,
    });
    state.messages = reset ? (data.items || []) : state.messages.concat(data.items || []);
    state.nextCursor = data.next_cursor || "";
    state.hasMore = Boolean(data.has_more);
    const selectedFolder = state.folders.find((item) => item.name === state.folder);
    if (selectedFolder && data.index) {
      selectedFolder.stats = data.index;
      renderFolders();
    }
    elements.syncWarning.textContent = data.sync_warning || "";
    elements.syncWarning.classList.toggle("hidden", !data.sync_warning);
    renderMessageList();
  } catch (error) {
    elements.messageList.replaceChildren(emptyNode(error.message));
    toast(error.message, true);
  }
}

function emptyNode(text, className = "empty-list") {
  const node = document.createElement("div");
  node.className = className;
  node.textContent = text;
  return node;
}

function renderMessageList() {
  elements.messageList.replaceChildren();
  if (!state.messages.length) elements.messageList.append(emptyNode("当前条件下没有邮件。"));
  for (const message of state.messages) {
    const item = document.createElement("div");
    item.className = `message-item${message.uid === state.selectedUid ? " active" : ""}`;
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const sender = document.createElement("span");
    sender.className = "message-sender";
    sender.textContent = message.from_name || message.from_addr || "未知发件人";
    const date = document.createElement("time");
    date.textContent = formatDate(message.date_ts, message.date_text);
    meta.append(sender, date);
    const subject = document.createElement("div");
    subject.className = "message-subject";
    subject.textContent = message.subject || "(无主题)";
    const flags = document.createElement("div");
    flags.className = "message-flags";
    const normalFlags = document.createElement("span");
    normalFlags.textContent = `UID ${message.uid}${message.has_attachments ? " · 附件" : ""}${message.body_cached ? " · 已缓存正文" : ""}`;
    flags.append(normalFlags);
    if (["changed", "deleted"].includes(message.verificationStatus)) {
      const stale = document.createElement("span");
      stale.className = "message-stale";
      stale.textContent = message.verificationStatus === "deleted" ? "云端已删除或移动" : "云端内容已变化";
      const refresh = document.createElement("button");
      refresh.className = "message-refresh";
      refresh.textContent = "刷新";
      refresh.onclick = (event) => refreshValidatedMessage(event, message);
      flags.append(stale, refresh);
    }
    item.append(meta, subject, flags);
    item.onclick = () => openMessage(message.uid);
    item.onkeydown = (event) => {
      if (event.target === item && (event.key === "Enter" || event.key === " ")) {
        openMessage(message.uid);
      }
    };
    elements.messageList.append(item);
  }
  elements.loadMore.classList.toggle("hidden", !state.hasMore);
}

async function openMessage(uid) {
  const accountId = state.accountId;
  const folder = state.folder;
  state.selectedUid = uid;
  renderMessageList();
  elements.messageDetail.className = "detail-panel";
  const indexed = state.messages.find((item) => item.uid === uid);
  if (indexed?.body_cached) {
    try {
      const cached = await apiGet("message/cached", {
        account_id: accountId,
        folder,
        uid,
      });
      if (
        state.accountId !== accountId
        || state.folder !== folder
        || state.selectedUid !== uid
      ) return;
      renderMessageDetail(cached);
      void verifyCachedMessage(cached);
      return;
    } catch (_error) {
      // Cache may have been evicted between list and click; fall back to IMAP.
    }
  }
  elements.messageDetail.replaceChildren(emptyNode("正在从云端读取正文…", "loading"));
  try {
    const message = await apiGet("message", { account_id: accountId, folder, uid });
    if (state.accountId !== accountId || state.folder !== folder) return;
    const current = state.messages.find((item) => item.uid === uid);
    if (current && message.body_cached) {
      current.body_cached = true;
      current.body_truncated = Boolean(message.body_truncated);
      renderMessageList();
    }
    if (state.selectedUid !== uid) return;
    renderMessageDetail(message);
  } catch (error) {
    if (state.selectedUid !== uid) return;
    elements.messageDetail.className = "detail-panel empty-state";
    elements.messageDetail.replaceChildren(emptyNode(error.message));
    toast(error.message, true);
    await loadMessages(true);
  }
}

async function verifyCachedMessage(message) {
  try {
    const result = await apiPost("message/verify", {
      account_id: message.account_id,
      folder: message.folder,
      uid: message.uid,
    });
    if (
      state.accountId !== message.account_id
      || state.folder !== message.folder
    ) return;
    const indexed = state.messages.find((item) => item.uid === message.uid);
    if (indexed) indexed.verificationStatus = result.verification_status;
    renderMessageList();
    if (state.selectedUid !== message.uid) return;
    const banner = $("#detail-verification");
    if (!banner) return;
    if (["changed", "deleted"].includes(result.verification_status)) {
      banner.className = "detail-banner stale";
      banner.textContent = `${result.message} 当前仍显示打开时的缓存；请点击邮件标题旁的“刷新”，或下次打开时使用最新状态。`;
    } else {
      banner.className = "detail-banner verified";
      banner.textContent = "已在后台完成云端校验。";
    }
  } catch (error) {
    if (state.selectedUid !== message.uid) return;
    const banner = $("#detail-verification");
    if (banner) {
      banner.className = "detail-banner";
      banner.textContent = `后台云端校验暂时失败：${error.message}`;
    }
  }
}

async function refreshValidatedMessage(event, message) {
  event.stopPropagation();
  const status = message.verificationStatus;
  await loadMessages(true);
  const stillExists = state.messages.some((item) => item.uid === message.uid);
  if (status === "changed" && stillExists) {
    await openMessage(message.uid);
  } else if (!stillExists && state.selectedUid === message.uid) {
    state.selectedUid = null;
    elements.messageDetail.className = "detail-panel empty-state";
    elements.messageDetail.replaceChildren(emptyNode("该邮件已不在当前云端文件夹中。"));
  }
}

function renderMessageDetail(message) {
  elements.messageDetail.replaceChildren();
  const title = document.createElement("h2");
  title.className = "detail-subject";
  title.textContent = message.subject || "(无主题)";
  const meta = document.createElement("div");
  meta.className = "detail-meta";
  const sender = message.from_name ? `${message.from_name} <${message.from_addr}>` : message.from_addr;
  meta.append(
    document.createTextNode(`发件人：${sender || "未知"}`),
    document.createElement("br"),
    document.createTextNode(`时间：${message.date || "未知"} · UID ${message.uid}`),
  );
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  const reply = document.createElement("button");
  reply.className = "button secondary";
  reply.textContent = "回复此邮件";
  reply.onclick = () => startReply(message);
  const aiResult = document.createElement("div");
  aiResult.className = "mail-ai-result hidden";
  const summary = createProcessingButton(message, "summary", aiResult);
  const translate = createProcessingButton(message, "translate", aiResult);
  actions.append(reply, summary, translate);
  const body = document.createElement("div");
  body.className = "detail-body";
  body.textContent = message.body || "（无可显示的纯文本正文）";
  elements.messageDetail.append(title, meta);
  if (message.from_cache) {
    const verification = document.createElement("div");
    verification.id = "detail-verification";
    verification.className = "detail-banner cached";
    verification.textContent = "已立即显示本地缓存，正在后台校验云端状态…";
    elements.messageDetail.append(verification);
  }
  if (message.has_attachments) {
    const banner = document.createElement("div");
    banner.className = "detail-banner";
    banner.textContent = "这封邮件包含附件；当前版本只显示附件存在状态，不下载附件。";
    elements.messageDetail.append(banner);
  }
  if (message.body_truncated) {
    const banner = document.createElement("div");
    banner.className = "detail-banner";
    banner.textContent = "正文已按插件配置截断显示。";
    elements.messageDetail.append(banner);
  }
  elements.messageDetail.append(actions);
  if (currentAccount()?.organize_enabled && state.folders.length > 1) {
    const folderActions = document.createElement("div");
    folderActions.className = "folder-actions";
    const target = document.createElement("select");
    for (const folder of state.folders.filter((item) => item.name !== message.folder)) {
      const option = document.createElement("option");
      option.value = folder.name;
      option.textContent = folder.display_name || folder.name;
      target.append(option);
    }
    const copy = document.createElement("button");
    copy.className = "button secondary compact";
    copy.textContent = "复制到";
    copy.onclick = () => transferCurrentMessage(message, target.value, false, copy);
    const move = document.createElement("button");
    move.className = "button secondary compact";
    move.textContent = "移动到";
    move.onclick = () => transferCurrentMessage(message, target.value, true, move);
    folderActions.append(target, copy, move);
    elements.messageDetail.append(folderActions);
  }
  elements.messageDetail.append(aiResult, body);
  void showConfiguredCachedResult(message, aiResult);
}

function createProcessingButton(message, task, resultNode) {
  const group = document.createElement("span");
  group.className = "split-button";
  const main = document.createElement("button");
  main.className = "button secondary split-main";
  main.textContent = task === "summary" ? "总结邮件" : "翻译邮件";
  main.onclick = () => processMessage(message, task, main, resultNode);
  const regenerate = document.createElement("button");
  regenerate.className = "button secondary split-regenerate";
  regenerate.type = "button";
  regenerate.textContent = "↻";
  regenerate.title = task === "summary" ? "指定语言并重新总结" : "指定语言并重新翻译";
  regenerate.setAttribute("aria-label", regenerate.title);
  regenerate.onclick = async () => {
    const requested = await requestProcessingLanguage(task);
    if (requested === null) return;
    await processMessage(message, task, main, resultNode, {
      force: true,
      targetLanguage: requested,
    });
  };
  group.append(main, regenerate);
  return group;
}

function displayAiResult(resultNode, task, result) {
  resultNode.replaceChildren();
  const heading = document.createElement("strong");
  heading.className = "mail-ai-heading";
  heading.textContent = `${task === "summary" ? `邮件总结（${result.target_language}）` : `邮件翻译（${result.target_language}）`}${result.cached ? " · 已使用缓存" : ""}`;
  resultNode.append(heading, renderMarkdown(result.content));
  resultNode.classList.remove("hidden");
}

async function showConfiguredCachedResult(message, resultNode) {
  const task = state.autoShowCachedAi;
  if (!["summary", "translate"].includes(task)) return;
  const accountId = message.account_id;
  const folder = message.folder;
  const uid = message.uid;
  const requestVersion = resultNode.dataset.requestVersion || "0";
  try {
    const locale = state.bridge.getLocale?.() || state.context.locale || "zh-CN";
    const result = await apiGet("message/ai-cache", {
      account_id: accountId,
      folder,
      uid,
      task,
      locale,
    });
    if (
      !result.available
      || state.accountId !== accountId
      || state.folder !== folder
      || state.selectedUid !== uid
      || (resultNode.dataset.requestVersion || "0") !== requestVersion
    ) return;
    displayAiResult(resultNode, task, result);
  } catch (_error) {
    // Automatic display is best-effort; manual buttons still report errors.
  }
}

function requestProcessingLanguage(task) {
  const modal = $("#processing-modal");
  const input = $("#processing-language-input");
  $("#processing-modal-title").textContent = task === "summary" ? "重新总结邮件" : "重新翻译邮件";
  $("#processing-modal-message").textContent = "输入目标语言后会忽略已有缓存并重新调用模型；留空则使用插件配置或 AstrBot 界面语言。";
  input.value = "";
  modal.classList.remove("hidden");
  input.focus();
  return new Promise((resolve) => {
    const finish = (value) => {
      modal.classList.add("hidden");
      $("#processing-modal-confirm").onclick = null;
      $("#processing-modal-cancel").onclick = null;
      input.onkeydown = null;
      resolve(value);
    };
    $("#processing-modal-confirm").onclick = () => finish(input.value.trim());
    $("#processing-modal-cancel").onclick = () => finish(null);
    input.onkeydown = (event) => {
      if (event.key === "Enter") finish(input.value.trim());
      if (event.key === "Escape") finish(null);
    };
  });
}

async function processMessage(message, task, button, resultNode, options = {}) {
  const requestVersion = String(Number(resultNode.dataset.requestVersion || "0") + 1);
  resultNode.dataset.requestVersion = requestVersion;
  setBusy(button, true, task === "summary" ? "总结中…" : "翻译中…");
  try {
    const locale = state.bridge.getLocale?.() || state.context.locale || "zh-CN";
    const result = await apiPost(`message/${task}`, {
      account_id: message.account_id,
      folder: message.folder,
      uid: message.uid,
      locale,
      target_language: options.targetLanguage || "",
      force: options.force === true,
    });
    if (resultNode.dataset.requestVersion === requestVersion) {
      displayAiResult(resultNode, task, result);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function transferCurrentMessage(message, targetFolder, move, button) {
  if (!targetFolder) return;
  if (move) {
    const confirmed = await confirmAction(
      "移动这封邮件？",
      `将 UID ${message.uid} 从“${message.folder}”移动到“${targetFolder}”。这会修改云端邮箱。`,
      "确认移动",
    );
    if (!confirmed) return;
  }
  setBusy(button, true, move ? "移动中…" : "复制中…");
  try {
    await apiPost(`messages/${move ? "move" : "copy"}`, {
      account_id: message.account_id,
      source_folder: message.folder,
      target_folder: targetFolder,
      uid: message.uid,
    });
    toast(move ? "邮件已移动" : "邮件已复制");
    await loadFolders();
    if (move) {
      state.selectedUid = null;
      await loadMessages(true);
      elements.messageDetail.className = "detail-panel empty-state";
      elements.messageDetail.replaceChildren(emptyNode("邮件已移动，请选择另一封邮件。"));
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function syncCurrent() {
  setBusy(elements.syncButton, true, "同步中…");
  try {
    const data = await apiPost("sync", { account_id: state.accountId, folder: state.folder });
    const failed = (data.results || []).find((item) => !item.success);
    if (failed) throw new Error(failed.error || "同步失败");
    await loadOverview();
    await loadFolders();
    await loadMessages(true);
    toast("云端索引同步完成");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(elements.syncButton, false);
  }
}

async function switchTab(tab) {
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $("#mailbox-view").classList.toggle("active", tab === "mailbox");
  $("#drafts-view").classList.toggle("active", tab === "drafts");
  $("#page-title").textContent = tab === "mailbox" ? "收件箱" : "Bot 草稿箱";
  $("#page-subtitle").textContent = tab === "mailbox" ? "从本地索引快速浏览，打开正文时实时验证云端。" : "编辑、人工审核并发送 Bot 或用户创建的邮件草稿。";
  elements.syncButton.classList.toggle("hidden", tab !== "mailbox");
  if (tab === "drafts") await loadDrafts();
}

async function loadDrafts() {
  try {
    const data = await apiGet("drafts", { limit: 200 });
    state.drafts = data.items || [];
    renderDraftList();
  } catch (error) {
    elements.draftList.replaceChildren(emptyNode(error.message));
    toast(error.message, true);
  }
}

function renderDraftList() {
  elements.draftList.replaceChildren();
  if (!state.drafts.length) elements.draftList.append(emptyNode("暂无草稿。"));
  for (const draft of state.drafts) {
    const button = document.createElement("button");
    button.className = `draft-item${draft.draft_id === state.draft?.draft_id ? " active" : ""}`;
    const row = document.createElement("div");
    row.className = "draft-item-row";
    const subject = document.createElement("strong");
    subject.textContent = draft.subject || "(无主题)";
    const status = document.createElement("small");
    status.textContent = draftStatusLabel(draft.status);
    row.append(subject, status);
    const to = document.createElement("small");
    to.textContent = `${draft.account_id} → ${(draft.to_addrs || []).join(", ") || "未填写收件人"}`;
    button.append(row, to);
    button.onclick = () => openDraft(draft.draft_id);
    elements.draftList.append(button);
  }
}

async function openDraft(draftId) {
  try {
    const draft = await apiGet("draft", { draft_id: draftId });
    selectDraft(draft);
  } catch (error) {
    toast(error.message, true);
    await loadDrafts();
  }
}

function selectDraft(draft) {
  state.draft = draft;
  state.draftDirty = false;
  elements.draftAccount.value = draft.account_id;
  elements.draftAccount.disabled = true;
  elements.draftTo.value = (draft.to_addrs || []).join(", ");
  elements.draftCc.value = (draft.cc_addrs || []).join(", ");
  elements.draftBcc.value = (draft.bcc_addrs || []).join(", ");
  elements.draftSubject.value = draft.subject || "";
  elements.draftBody.value = draft.body_text || "";
  elements.draftHeading.textContent = draft.subject || "未命名草稿";
  elements.deleteDraft.classList.remove("hidden");
  renderDraftStatus();
  renderDraftList();
}

function newDraft(prefill = {}) {
  state.draft = null;
  state.draftDirty = true;
  elements.draftAccount.disabled = false;
  for (const input of [elements.draftTo, elements.draftCc, elements.draftBcc, elements.draftSubject, elements.draftBody]) {
    input.disabled = false;
  }
  elements.draftAccount.value = prefill.account_id || state.accountId || elements.draftAccount.options[0]?.value || "";
  elements.draftTo.value = prefill.to || "";
  elements.draftCc.value = "";
  elements.draftBcc.value = "";
  elements.draftSubject.value = prefill.subject || "";
  elements.draftBody.value = prefill.body || "";
  elements.draftHeading.textContent = prefill.subject || "新草稿";
  elements.deleteDraft.classList.add("hidden");
  elements.draftStatus.textContent = "未保存";
  elements.draftStatus.className = "status-chip neutral";
  elements.saveDraft.disabled = false;
  elements.sendDraft.disabled = true;
  elements.approveDraft.disabled = true;
  state.replyPrefill = prefill.reply_uid ? { reply_uid: prefill.reply_uid, reply_folder: prefill.reply_folder } : null;
  renderDraftList();
}

function renderDraftStatus() {
  const status = state.draft?.status || "editing";
  const immutable = ["sending", "sent", "cancelled"].includes(status);
  elements.draftStatus.textContent = state.draftDirty ? "有未保存修改" : draftStatusLabel(status);
  elements.draftStatus.className = `status-chip ${state.draftDirty ? "neutral" : status}`;
  $("#approve-draft-button").disabled = !state.draft || state.draftDirty || immutable;
  elements.sendDraft.disabled = !state.draft || state.draftDirty || status !== "approved";
  elements.deleteDraft.disabled = !state.draft || status === "sending";
  elements.saveDraft.disabled = immutable;
  for (const input of [elements.draftTo, elements.draftCc, elements.draftBcc, elements.draftSubject, elements.draftBody]) {
    input.disabled = immutable;
  }
}

function draftFormPayload() {
  return {
    account_id: elements.draftAccount.value,
    to_addrs: elements.draftTo.value,
    cc_addrs: elements.draftCc.value,
    bcc_addrs: elements.draftBcc.value,
    subject: elements.draftSubject.value,
    body_text: elements.draftBody.value,
  };
}

async function saveDraft() {
  const button = elements.saveDraft;
  setBusy(button, true, "保存中…");
  try {
    let saved;
    if (state.draft) {
      saved = await apiPost("drafts/update", { ...draftFormPayload(), draft_id: state.draft.draft_id, revision: state.draft.revision });
    } else {
      saved = await apiPost("drafts/create", { ...draftFormPayload(), ...(state.replyPrefill || {}) });
    }
    state.draft = saved;
    state.replyPrefill = null;
    state.draftDirty = false;
    elements.draftAccount.disabled = true;
    await loadDrafts();
    selectDraft(saved);
    toast("草稿已保存");
    return saved;
  } catch (error) {
    toast(error.message, true);
    return null;
  } finally {
    setBusy(button, false);
    renderDraftStatus();
  }
}

async function approveDraft() {
  if (!state.draft || state.draftDirty) return;
  const button = elements.approveDraft;
  setBusy(button, true, "审核中…");
  try {
    const approved = await apiPost("drafts/approve", { draft_id: state.draft.draft_id, revision: state.draft.revision });
    state.draft = approved;
    await loadDrafts();
    selectDraft(approved);
    toast("草稿已审核，可以发送");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
    renderDraftStatus();
  }
}

async function sendDraft() {
  if (!state.draft || state.draft.status !== "approved" || state.draftDirty) return;
  const recipient = (state.draft.to_addrs || []).join(", ");
  const confirmed = await confirmAction("发送这封邮件？", `将使用 ${state.draft.account_id} 向 ${recipient} 发送“${state.draft.subject}”。发送后无法撤回。`, "确认发送");
  if (!confirmed) return;
  setBusy(elements.sendDraft, true, "发送中…");
  try {
    const sent = await apiPost("drafts/send", { draft_id: state.draft.draft_id, revision: state.draft.revision });
    state.draft = sent;
    await loadDrafts();
    selectDraft(sent);
    toast("邮件已发送");
  } catch (error) {
    toast(error.message, true);
    await loadDrafts();
    if (state.draft) {
      const latest = state.drafts.find((item) => item.draft_id === state.draft.draft_id);
      if (latest) await openDraft(latest.draft_id);
    }
  } finally {
    setBusy(elements.sendDraft, false);
    renderDraftStatus();
  }
}

async function deleteDraft() {
  if (!state.draft) return;
  const confirmed = await confirmAction("删除草稿？", "只删除邮件助手本地草稿，不会删除云端邮件。", "删除");
  if (!confirmed) return;
  try {
    await apiPost("drafts/delete", { draft_id: state.draft.draft_id, revision: state.draft.revision });
    newDraft();
    await loadDrafts();
    toast("草稿已删除");
  } catch (error) {
    toast(error.message, true);
  }
}

async function startReply(message) {
  await switchTab("drafts");
  const subject = /^re:/i.test(message.subject || "") ? message.subject : `Re: ${message.subject || "(无主题)"}`;
  newDraft({
    account_id: message.account_id,
    to: message.reply_to || message.from_addr,
    subject,
    reply_uid: message.uid,
    reply_folder: message.folder,
  });
}

async function createFolderFromUi() {
  if (!currentAccount()?.organize_enabled) {
    toast("请先在该邮箱账户设置中开启“允许 WebUI 整理邮件和文件夹”。", true);
    return;
  }
  const modal = $("#input-modal");
  const input = $("#folder-name-input");
  input.value = "";
  modal.classList.remove("hidden");
  input.focus();
  const name = await new Promise((resolve) => {
    const finish = (value) => {
      modal.classList.add("hidden");
      $("#folder-modal-confirm").onclick = null;
      $("#folder-modal-cancel").onclick = null;
      resolve(value);
    };
    $("#folder-modal-confirm").onclick = () => finish(input.value.trim());
    $("#folder-modal-cancel").onclick = () => finish("");
  });
  if (!name) return;
  try {
    await apiPost("folders/create", { account_id: state.accountId, name });
    await loadFolders();
    toast("文件夹已创建");
  } catch (error) {
    toast(error.message, true);
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  elements.syncButton.addEventListener("click", syncCurrent);
  elements.mobileAccount.addEventListener("change", async () => {
    state.accountId = elements.mobileAccount.value;
    state.folder = "";
    state.selectedUid = null;
    renderAccounts();
    await loadFolders();
    await loadMessages(true);
  });
  elements.mobileFolder.addEventListener("change", async () => {
    state.folder = elements.mobileFolder.value;
    state.selectedUid = null;
    renderFolders();
    await loadMessages(true);
  });
  $("#new-folder-button").addEventListener("click", createFolderFromUi);
  $("#search-button").addEventListener("click", () => loadMessages(true));
  elements.mailSearch.addEventListener("keydown", (event) => { if (event.key === "Enter") loadMessages(true); });
  elements.loadMore.addEventListener("click", () => loadMessages(false));
  $("#new-draft-button").addEventListener("click", () => newDraft());
  $("#save-draft-button").addEventListener("click", saveDraft);
  $("#approve-draft-button").addEventListener("click", approveDraft);
  elements.sendDraft.addEventListener("click", sendDraft);
  elements.deleteDraft.addEventListener("click", deleteDraft);
  [elements.draftAccount, elements.draftTo, elements.draftCc, elements.draftBcc, elements.draftSubject, elements.draftBody].forEach((input) => {
    input.addEventListener("input", () => { state.draftDirty = true; elements.draftHeading.textContent = elements.draftSubject.value || "新草稿"; renderDraftStatus(); });
  });
}

async function start() {
  try {
    state.bridge = window.AstrBotPluginPage;
    if (!state.bridge) throw new Error("AstrBot Plugin Page Bridge 不可用");
    state.context = await state.bridge.ready();
    applyContext(state.context || {});
    state.bridge.onContext?.((context) => applyContext(context || {}));
    if (state.context?.username) $("#bridge-user").textContent = state.context.username;
    bindEvents();
    await loadOverview();
    await loadFolders();
    await loadMessages(true);
    newDraft();
  } catch (error) {
    elements.runtimeState.textContent = "页面初始化失败";
    elements.runtimeState.classList.add("error");
    toast(error.message, true);
  }
}

start();
