const markdownFactory = globalThis.markdownit;
const purifier = globalThis.DOMPurify;

const markdown = typeof markdownFactory === "function"
  ? markdownFactory({
      html: false,
      linkify: true,
      breaks: true,
      typographer: false,
    })
  : null;

const ALLOWED_TAGS = [
  "p", "br", "strong", "em", "s", "del", "a", "ul", "ol", "li",
  "blockquote", "pre", "code", "h1", "h2", "h3", "h4", "h5", "h6",
  "table", "thead", "tbody", "tr", "th", "td", "hr",
];
const ALLOWED_ATTR = ["href", "title", "target", "rel", "class", "style", "start"];

function plainTextFallback(root, source) {
  const paragraph = document.createElement("p");
  paragraph.className = "markdown-fallback";
  paragraph.textContent = String(source || "") || "（无结果）";
  root.replaceChildren(paragraph);
}

export function renderMarkdown(source) {
  const root = document.createElement("div");
  root.className = "markdown-body";
  const text = String(source || "");
  if (!text) {
    plainTextFallback(root, "");
    return root;
  }
  if (!markdown || !purifier || typeof purifier.sanitize !== "function") {
    plainTextFallback(root, text);
    return root;
  }

  try {
    const unsafeHtml = markdown.render(text);
    const fragment = purifier.sanitize(unsafeHtml, {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS,
      ALLOWED_ATTR,
      ALLOW_DATA_ATTR: false,
      ALLOW_ARIA_ATTR: false,
    });
    if (!(fragment instanceof DocumentFragment)) {
      throw new TypeError("DOMPurify did not return a DocumentFragment");
    }
    root.append(fragment);
    root.querySelectorAll("a[href]").forEach((link) => {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    });
    if (!root.childNodes.length) plainTextFallback(root, text);
  } catch (error) {
    console.error("[EmailAssistantPage] Markdown rendering failed", error);
    plainTextFallback(root, text);
  }
  return root;
}
