const INLINE_PATTERN = /(`+)(.*?)\1|\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|\*([^*\n]+)\*|_([^_\n]+)_/g;

function appendText(parent, value) {
  const parts = String(value || "").split("\n");
  parts.forEach((part, index) => {
    if (index) parent.append(document.createElement("br"));
    if (part) parent.append(document.createTextNode(part));
  });
}

function safeLink(value) {
  try {
    const url = new URL(value, window.location.href);
    if (["http:", "https:", "mailto:"].includes(url.protocol)) return url.href;
  } catch (_) {
    // Invalid links are rendered as plain text below.
  }
  return "";
}

function appendInline(parent, source) {
  const text = String(source || "");
  INLINE_PATTERN.lastIndex = 0;
  let cursor = 0;
  let match;
  while ((match = INLINE_PATTERN.exec(text)) !== null) {
    appendText(parent, text.slice(cursor, match.index));
    if (match[1]) {
      const code = document.createElement("code");
      code.textContent = match[2];
      parent.append(code);
    } else if (match[3]) {
      const href = safeLink(match[4]);
      if (href) {
        const link = document.createElement("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        appendInline(link, match[3]);
        parent.append(link);
      } else {
        appendText(parent, match[3]);
      }
    } else {
      const element = document.createElement(
        match[5] || match[6] ? "strong" : match[7] ? "del" : "em",
      );
      appendInline(element, match[5] || match[6] || match[7] || match[8] || match[9]);
      parent.append(element);
    }
    cursor = match.index + match[0].length;
  }
  appendText(parent, text.slice(cursor));
}

function splitTableRow(line) {
  let value = String(line || "").trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  const cells = [];
  let cell = "";
  let escaped = false;
  for (const character of value) {
    if (escaped) {
      cell += character === "|" ? "|" : `\\${character}`;
      escaped = false;
    } else if (character === "\\") {
      escaped = true;
    } else if (character === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += character;
    }
  }
  if (escaped) cell += "\\";
  cells.push(cell.trim());
  return cells;
}

function tableAlignment(line) {
  const cells = splitTableRow(line);
  if (!cells.length || cells.some((cell) => !/^:?-{3,}:?$/.test(cell))) return null;
  return cells.map((cell) => {
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "left";
  });
}

function isFence(line) {
  return /^\s{0,3}(`{3,}|~{3,})/.test(line);
}

function isBlockStart(lines, index) {
  const line = lines[index] || "";
  if (!line.trim()) return true;
  if (isFence(line)) return true;
  if (/^\s{0,3}#{1,6}\s+/.test(line)) return true;
  if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) return true;
  if (/^\s{0,3}>\s?/.test(line)) return true;
  if (/^\s{0,3}(?:[-+*]|\d+[.)])\s+/.test(line)) return true;
  return Boolean(index + 1 < lines.length && line.includes("|") && tableAlignment(lines[index + 1]));
}

function appendTable(root, lines, start) {
  const alignment = tableAlignment(lines[start + 1]);
  const headers = splitTableRow(lines[start]);
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  headers.forEach((value, column) => {
    const cell = document.createElement("th");
    cell.style.textAlign = alignment[column] || "left";
    appendInline(cell, value);
    headerRow.append(cell);
  });
  thead.append(headerRow);
  table.append(thead);
  const tbody = document.createElement("tbody");
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
    const row = document.createElement("tr");
    splitTableRow(lines[index]).forEach((value, column) => {
      const cell = document.createElement("td");
      cell.style.textAlign = alignment[column] || "left";
      appendInline(cell, value);
      row.append(cell);
    });
    tbody.append(row);
    index += 1;
  }
  table.append(tbody);
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-wrap";
  wrapper.append(table);
  root.append(wrapper);
  return index;
}

export function renderMarkdown(source) {
  const root = document.createElement("div");
  root.className = "markdown-body";
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)?.*$/);
    if (fence) {
      const marker = fence[1][0];
      const size = fence[1].length;
      const language = String(fence[2] || "").replace(/[^a-zA-Z0-9_+#.-]/g, "");
      const content = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s{0,3}${marker}{${size},}\\s*$`).test(lines[index])) {
        content.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.className = `language-${language}`;
      code.textContent = content.join("\n");
      pre.append(code);
      root.append(pre);
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const node = document.createElement(`h${heading[1].length}`);
      appendInline(node, heading[2]);
      root.append(node);
      index += 1;
      continue;
    }

    if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) {
      root.append(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && tableAlignment(lines[index + 1])) {
      index = appendTable(root, lines, index);
      continue;
    }

    if (/^\s{0,3}>\s?/.test(line)) {
      const quoted = [];
      while (index < lines.length && /^\s{0,3}>\s?/.test(lines[index])) {
        quoted.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      quote.append(...renderMarkdown(quoted.join("\n")).childNodes);
      root.append(quote);
      continue;
    }

    const listMatch = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = document.createElement(ordered ? "ol" : "ul");
      if (ordered) list.start = Number.parseInt(listMatch[1], 10) || 1;
      while (index < lines.length) {
        const item = lines[index].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const node = document.createElement("li");
        appendInline(node, item[2]);
        list.append(node);
        index += 1;
      }
      root.append(list);
      continue;
    }

    const paragraphLines = [line];
    index += 1;
    while (index < lines.length && !isBlockStart(lines, index)) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = document.createElement("p");
    appendInline(paragraph, paragraphLines.join("\n"));
    root.append(paragraph);
  }

  if (!root.childNodes.length) {
    const paragraph = document.createElement("p");
    paragraph.textContent = "（无结果）";
    root.append(paragraph);
  }
  return root;
}
