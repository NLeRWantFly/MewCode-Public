/* Shared Markdown + KaTeX helpers for desktop and Android shells. */
(function (global) {
  "use strict";

  var MATH_ENV = "equation\\*?|align\\*?|alignat\\*?|gather\\*?|CD";

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isCodeChunk(chunk) {
    return /^```[\s\S]*```$/.test(chunk) || /^`[^`\n]+`$/.test(chunk);
  }

  function looksLikeTex(body) {
    return /[\\^_{}]|[A-Za-z]/.test(String(body || ""));
  }

  function renderTex(body, display) {
    var source = String(body || "").trim();
    if (!source) return "";
    if (global.katex && typeof global.katex.renderToString === "function") {
      try {
        return global.katex.renderToString(source, {
          displayMode: !!display,
          throwOnError: false,
          output: "html",
          trust: false,
        });
      } catch (_error) {
        /* Keep a visible TeX fallback. */
      }
    }
    var safe = esc(source);
    return display ? ("\\[" + safe + "\\]") : ("\\(" + safe + "\\)");
  }

  // marked treats `\[` / `\(` as escaped punctuation and strips the backslash
  // before KaTeX can see it. Stash math first, restore after Markdown.
  function extractMath(source) {
    const slots = [];
    function stash(display, body) {
      const token = "@@MEWMATH" + slots.length + "@@";
      slots.push({ display: !!display, body: String(body || "").trim() });
      return token;
    }

    const envRe = new RegExp("\\\\begin\\{(" + MATH_ENV + ")\\}([\\s\\S]*?)\\\\end\\{\\1\\}", "g");
    const chunks = String(source ?? "").split(/(```[\s\S]*?```|`[^`\n]+`)/g);
    const text = chunks.map((chunk) => {
      if (!chunk || isCodeChunk(chunk)) return chunk;
      let next = chunk;
      next = next.replace(/\$\$([\s\S]+?)\$\$/g, (_, body) => stash(true, body));
      next = next.replace(/\\\[([\s\S]+?)\\\]/g, (_, body) => stash(true, body));
      next = next.replace(envRe, (match) => stash(true, match));
      next = next.replace(/\\\(([\s\S]+?)\\\)/g, (_, body) => stash(false, body));
      next = next.replace(/\$([^$\n]+?)\$/g, (match, body) => (
        looksLikeTex(body) ? stash(false, body) : match
      ));
      next = next.replace(/(^|\n)\s*\[\s*\n([\s\S]*?)\n\s*\]\s*(?=\n|$)/g, (match, pre, body) => {
        if (!/[\\^_{}]|=/.test(body)) return match;
        return pre + stash(true, body);
      });
      return next;
    }).join("");
    return { text, slots };
  }

  function restoreMath(html, slots) {
    return String(html || "").replace(/@@MEWMATH(\d+)@@/g, (match, indexText) => {
      const slot = slots[Number(indexText)];
      if (!slot) return match;
      return renderTex(slot.body, slot.display);
    });
  }

  // GFM autolinks treat CJK as valid IRI characters, so
  // `https://example.com/path后续说明` becomes one blue <a>.
  // Insert a terminator so the URL ends before following prose.
  function isolateAutolinks(text) {
    return String(text ?? "").replace(
      /(https?:\/\/[\w\-._~:/?#\[\]@!$&'()*+,;=%]+)(?=[^\s\w\-._~:/?#\[\]@!$&'()*+,;=%])/gi,
      "$1 "
    );
  }

  function renderMarkdown(text) {
    const extracted = extractMath(isolateAutolinks(text));
    let html;
    if (global.marked && typeof global.marked.parse === "function") {
      global.marked.setOptions({ breaks: true, gfm: true });
      const options = global.MewFileLinks ? {renderer: global.MewFileLinks.renderer(new global.marked.Renderer())} : {};
      html = global.marked.parse(extracted.text, options);
    } else {
      html = "<pre style='white-space:pre-wrap'>" + esc(extracted.text) + "</pre>";
    }
    if (global.DOMPurify && typeof global.DOMPurify.sanitize === "function") {
      html = global.DOMPurify.sanitize(html);
    }
    // 纯 <br> 空段（marked 空行残留）：这里按字符串剔除，交给 CSS 判断会
    // 误伤 "文字<br>文字" 的段落——:only-child 不数文本节点。
    html = html.replace(/<p[^>]*>\s*(?:<br\s*\/?>\s*)+<\/p>/gi, "");
    return restoreMath(html, extracted.slots);
  }

  function enhanceMath(root) {
    if (!root || typeof global.renderMathInElement !== "function") return;
    try {
      global.renderMathInElement(root, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "\\begin{equation}", right: "\\end{equation}", display: true },
          { left: "\\begin{equation*}", right: "\\end{equation*}", display: true },
          { left: "\\begin{align}", right: "\\end{align}", display: true },
          { left: "\\begin{align*}", right: "\\end{align*}", display: true },
          { left: "\\begin{alignat}", right: "\\end{alignat}", display: true },
          { left: "\\begin{alignat*}", right: "\\end{alignat*}", display: true },
          { left: "\\begin{gather}", right: "\\end{gather}", display: true },
          { left: "\\begin{gather*}", right: "\\end{gather*}", display: true },
          { left: "\\begin{CD}", right: "\\end{CD}", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      });
    } catch (_error) {
      /* Keep the raw Markdown if a formula fails. */
    }
  }

  global.MewMarkdown = { esc, renderMarkdown, enhanceMath, extractMath, isolateAutolinks };
})(window);
