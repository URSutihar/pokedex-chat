/* ---------------------------------------------------------------------------
   Pokedex chat client.

   Streaming markdown is the fiddly part.  What we do, and why:

   1. Accumulate raw text; re-render on a ~60 ms debounce, not per token
      (16 ms is wasted work — the eye can't read that fast anyway).
   2. Before parsing, *repair* the tail: an unclosed ``` fence, ** run, `code`
      span, $math$ or half-typed [link]( makes the parser emit garbage that
      flips back to formatted a token later.  Closing it optimistically keeps
      the layout stable.
   3. Split into block tokens with marked's lexer and keep one DOM node per
      block.  Blocks 0..N-1 are unchanged on every tick, so we only re-render
      the last one.  No layout thrash, and already-streamed text stays
      selectable.
   4. Sanitize with DOMPurify; if it *removes* anything, the model emitted
      active content — stop rendering the message rather than patch it up.
   5. Expensive block types (mermaid, charts) render only once their closing
      fence has arrived; until then they show a placeholder.
   --------------------------------------------------------------------------- */

import { store } from "/static/history.js";

/* ------------------------------- theme ---------------------------------- */
const THEME_KEY = "pokedex-theme";
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem(THEME_KEY, t);
  initMermaid();
  rerenderAll();
}
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "dark";
}
// applied for real in boot(); set the attribute now so the first paint is correct
document.documentElement.setAttribute("data-theme", localStorage.getItem(THEME_KEY) || "dark");

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ------------------------------ mermaid --------------------------------- */
let mermaidReady = false;
function initMermaid() {
  if (typeof mermaid === "undefined") return;
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    fontFamily: cssVar("--sans") || "sans-serif",
    // pure-SVG labels: foreignObject/HTML would be stripped by the sanitiser
    htmlLabels: false,
    flowchart: { htmlLabels: false, curve: "basis", useMaxWidth: true },
    sequence: { useMaxWidth: true },
    themeVariables: {
      background: cssVar("--surface-1"),
      primaryColor: cssVar("--surface-2"),
      primaryTextColor: cssVar("--text"),
      primaryBorderColor: cssVar("--border-strong"),
      secondaryColor: cssVar("--surface-3"),
      tertiaryColor: cssVar("--surface-2"),
      lineColor: cssVar("--text-3"),
      textColor: cssVar("--text"),
      mainBkg: cssVar("--surface-2"),
      nodeBorder: cssVar("--border-strong"),
      clusterBkg: cssVar("--surface-1"),
      edgeLabelBackground: cssVar("--surface-1"),
    },
  });
  mermaidReady = true;
}
initMermaid();

/* ------------------------------- marked --------------------------------- */
marked.setOptions({ gfm: true, breaks: false, mangle: false, headerIds: false });

/* ----------------------- incomplete-markdown repair --------------------- */
function repairMarkdown(src) {
  let s = src;

  // 1. unbalanced fenced code block
  const fences = (s.match(/^```/gm) || []).length;
  if (fences % 2 === 1) s += "\n```";

  // Everything below must ignore text inside fenced blocks.
  const outside = s.replace(/```[\s\S]*?```/g, "");

  // 2. dangling link / image: ![alt]( or [text](  with no closing paren
  s = s.replace(/!?\[[^\]\n]*\]\([^)\n]*$/, "");
  // 3. dangling link label: [text  with no closing bracket
  s = s.replace(/!?\[[^\]\n]*$/, "");

  // 4. inline code
  const ticks = (outside.match(/(?<!`)`(?!`)/g) || []).length;
  if (ticks % 2 === 1) s += "`";

  // 5. bold / italic
  if ((outside.match(/\*\*/g) || []).length % 2 === 1) s += "**";

  // 6. math
  const display = (outside.match(/\$\$/g) || []).length;
  if (display % 2 === 1) s += "$$";
  else {
    const inline = (outside.replace(/\$\$/g, "").match(/\$/g) || []).length;
    if (inline % 2 === 1) s += "$";
  }
  return s;
}

/* --------------------------- chart rendering ---------------------------- */
const SERIES = i => `var(--series-${(i % 8) + 1})`;

function svgEl(tag, attrs = {}, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}
function roundedTop(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
}
function roundedRight(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, h / 2, w));
  return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`;
}
function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / (mag / 2)) * (mag / 2);
}

/** Returns null when the spec is renderable, else a short reason. */
function chartSpecProblem(spec) {
  if (!spec || typeof spec !== "object") return "the chart block was not valid JSON";
  if (!Array.isArray(spec.labels) || !spec.labels.length) return "no labels";
  if (!Array.isArray(spec.series) || !spec.series.length) return "no series";
  for (const s of spec.series) {
    if (!s || !Array.isArray(s.data) || !s.data.length) return "a series had no data";
    if (s.data.some(v => !Number.isFinite(Number(v)))) return "a series had non-numeric data";
  }
  return null;
}

/** Same numbers, as a table, when the chart spec cannot be drawn. */
function chartFallback(spec, reason) {
  const wrap = document.createElement("div");
  wrap.className = "figure";
  const note = document.createElement("div");
  note.className = "chart-title";
  note.textContent = spec?.title ? `${spec.title} (shown as a table — ${reason})` : `Chart unavailable — ${reason}`;
  wrap.appendChild(note);

  const labels = Array.isArray(spec?.labels) ? spec.labels : [];
  const series = Array.isArray(spec?.series) ? spec.series.filter(s => Array.isArray(s?.data)) : [];
  if (!labels.length || !series.length) return wrap;

  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  const t = document.createElement("table");
  const hr = document.createElement("tr");
  hr.appendChild(document.createElement("th"));
  series.forEach((s, i) => {
    const th = document.createElement("th");
    th.className = "num";
    th.textContent = s.name || `series ${i + 1}`;
    hr.appendChild(th);
  });
  const thead = document.createElement("thead");
  thead.appendChild(hr);
  const tb = document.createElement("tbody");
  labels.forEach((lab, li) => {
    const tr = document.createElement("tr");
    const th = document.createElement("td");
    th.textContent = String(lab);
    tr.appendChild(th);
    series.forEach(s => {
      const td = document.createElement("td");
      td.className = "num";
      td.textContent = s.data[li] ?? "—";
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.append(thead, tb);
  scroll.appendChild(t);
  wrap.appendChild(scroll);
  return wrap;
}

function renderChart(spec) {
  const wrap = document.createElement("div");
  wrap.className = "figure";
  if (spec.title) {
    const t = document.createElement("div");
    t.className = "chart-title";
    t.textContent = spec.title;
    wrap.appendChild(t);
  }
  const labels = (spec.labels || []).slice(0, 12);
  const series = (spec.series || []).slice(0, 8).map(s => ({
    name: s.name || "",
    data: (s.data || []).slice(0, labels.length).map(Number),
  }));
  if (!labels.length || !series.length) {
    wrap.appendChild(document.createTextNode("empty chart"));
    return wrap;
  }
  const type = (spec.type || "bar").toLowerCase();
  const svg = type === "radar" ? radarChart(labels, series, spec)
            : type === "hbar" ? hbarChart(labels, series, spec)
            : type === "line" ? lineChart(labels, series, spec)
            : barChart(labels, series, spec);
  wrap.appendChild(svg);

  if (series.length > 1) {
    const lg = document.createElement("div");
    lg.className = "chart-legend";
    series.forEach((s, i) => {
      const item = document.createElement("span");
      const sw = document.createElement("i");
      sw.className = "chart-swatch";
      sw.style.background = SERIES(i);
      item.appendChild(sw);
      item.appendChild(document.createTextNode(s.name || `series ${i + 1}`));
      lg.appendChild(item);
    });
    wrap.appendChild(lg);
  }
  return wrap;
}

function gridAndAxis(svg, x0, y0, w, h, max, ticks = 4) {
  for (let i = 0; i <= ticks; i++) {
    const v = (max / ticks) * i;
    const y = y0 + h - (h * i) / ticks;
    svg.appendChild(svgEl("line", {
      x1: x0, x2: x0 + w, y1: y, y2: y,
      stroke: "var(--grid)", "stroke-width": 1,
    }));
    svg.appendChild(svgEl("text", {
      x: x0 - 8, y: y + 4, "text-anchor": "end",
      "font-size": 10, fill: "var(--text-3)",
    }, String(Math.round(v))));
  }
}

function barChart(labels, series, spec) {
  const W = 680, H = 300, pad = { t: 12, r: 12, b: 46, l: 40 };
  const w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  const max = spec.max || niceMax(Math.max(...series.flatMap(s => s.data), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  gridAndAxis(svg, pad.l, pad.t, w, h, max);

  const groupW = w / labels.length;
  const gap = 2;
  const barW = Math.max(3, (groupW * 0.72) / series.length - gap);
  labels.forEach((lab, li) => {
    const gx = pad.l + groupW * li + groupW * 0.14;
    series.forEach((s, si) => {
      const v = s.data[li] || 0;
      const bh = Math.max(1, (v / max) * h);
      const x = gx + si * (barW + gap);
      const y = pad.t + h - bh;
      const p = svgEl("path", { d: roundedTop(x, y, barW, bh, 4), fill: SERIES(si) });
      p.appendChild(svgEl("title", {}, `${lab} · ${s.name || ""} ${v}`));
      svg.appendChild(p);
      if (series.length === 1 && labels.length <= 12) {
        svg.appendChild(svgEl("text", {
          x: x + barW / 2, y: y - 5, "text-anchor": "middle",
          "font-size": 10.5, fill: "var(--text-2)",
        }, String(v)));
      }
    });
    svg.appendChild(svgEl("text", {
      x: pad.l + groupW * li + groupW / 2, y: pad.t + h + 16,
      "text-anchor": "middle", "font-size": 10.5, fill: "var(--text-2)",
    }, String(lab).length > 12 ? String(lab).slice(0, 11) + "…" : String(lab)));
  });
  return svg;
}

function hbarChart(labels, series, spec) {
  const rowH = 26, W = 680;
  const pad = { t: 10, r: 44, b: 18, l: 130 };
  const H = pad.t + rowH * labels.length + 6 + pad.b;
  const w = W - pad.l - pad.r;
  const max = spec.max || niceMax(Math.max(...series.flatMap(s => s.data), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const barH = Math.max(4, (rowH * 0.68) / series.length - 2);
  labels.forEach((lab, li) => {
    const y0 = pad.t + li * rowH + (rowH - barH * series.length - 2 * (series.length - 1)) / 2;
    series.forEach((s, si) => {
      const v = s.data[li] || 0;
      const bw = Math.max(1, (v / max) * w);
      const y = y0 + si * (barH + 2);
      const p = svgEl("path", { d: roundedRight(pad.l, y, bw, barH, 4), fill: SERIES(si) });
      p.appendChild(svgEl("title", {}, `${lab} · ${s.name || ""} ${v}`));
      svg.appendChild(p);
      if (series.length === 1) {
        svg.appendChild(svgEl("text", {
          x: pad.l + bw + 7, y: y + barH - 1,
          "font-size": 10.5, fill: "var(--text-2)",
        }, String(v)));
      }
    });
    svg.appendChild(svgEl("text", {
      x: pad.l - 9, y: pad.t + li * rowH + rowH / 2 + 4, "text-anchor": "end",
      "font-size": 11, fill: "var(--text-2)",
    }, String(lab)));
  });
  return svg;
}

function lineChart(labels, series, spec) {
  const W = 680, H = 300, pad = { t: 12, r: 14, b: 42, l: 42 };
  const w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  const max = spec.max || niceMax(Math.max(...series.flatMap(s => s.data), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  gridAndAxis(svg, pad.l, pad.t, w, h, max);
  const step = labels.length > 1 ? w / (labels.length - 1) : 0;
  series.forEach((s, si) => {
    const pts = s.data.map((v, i) => [pad.l + step * i, pad.t + h - (v / max) * h]);
    svg.appendChild(svgEl("path", {
      d: pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" "),
      fill: "none", stroke: SERIES(si), "stroke-width": 2,
      "stroke-linecap": "round", "stroke-linejoin": "round",
    }));
    pts.forEach((p, i) => {
      const c = svgEl("circle", {
        cx: p[0], cy: p[1], r: 4, fill: SERIES(si),
        stroke: "var(--surface-1)", "stroke-width": 2,
      });
      c.appendChild(svgEl("title", {}, `${labels[i]} · ${s.name || ""} ${s.data[i]}`));
      svg.appendChild(c);
    });
  });
  labels.forEach((lab, i) => {
    svg.appendChild(svgEl("text", {
      x: pad.l + step * i, y: pad.t + h + 16, "text-anchor": "middle",
      "font-size": 10.5, fill: "var(--text-2)",
    }, String(lab)));
  });
  return svg;
}

function radarChart(labels, series, spec) {
  const W = 460, H = 380, cx = W / 2, cy = H / 2 + 4, R = 132;
  const max = spec.max || niceMax(Math.max(...series.flatMap(s => s.data), 0));
  const n = labels.length;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const pt = (i, r) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  };
  for (let ring = 1; ring <= 4; ring++) {
    const r = (R * ring) / 4;
    svg.appendChild(svgEl("polygon", {
      points: labels.map((_, i) => pt(i, r).map(v => v.toFixed(1)).join(",")).join(" "),
      fill: "none", stroke: "var(--grid)", "stroke-width": 1,
    }));
  }
  labels.forEach((_, i) => {
    const [x, y] = pt(i, R);
    svg.appendChild(svgEl("line", { x1: cx, y1: cy, x2: x, y2: y, stroke: "var(--grid)", "stroke-width": 1 }));
  });
  series.forEach((s, si) => {
    const pts = s.data.map((v, i) => pt(i, (Math.max(0, v) / max) * R));
    svg.appendChild(svgEl("polygon", {
      points: pts.map(p => p.map(v => v.toFixed(1)).join(",")).join(" "),
      // overlapping translucent fills turn to mud — outline only past one series
      fill: series.length > 1 ? "none" : SERIES(si), "fill-opacity": 0.22,
      stroke: SERIES(si), "stroke-width": 2, "stroke-linejoin": "round",
    }));
    pts.forEach((p, i) => {
      const c = svgEl("circle", { cx: p[0], cy: p[1], r: 3.5, fill: SERIES(si),
        stroke: "var(--surface-1)", "stroke-width": 2 });
      c.appendChild(svgEl("title", {}, `${labels[i]} ${s.data[i]}`));
      svg.appendChild(c);
    });
  });
  labels.forEach((lab, i) => {
    const [x, y] = pt(i, R + 22);
    svg.appendChild(svgEl("text", {
      x, y: y + 4, "text-anchor": Math.abs(x - cx) < 6 ? "middle" : x > cx ? "start" : "end",
      "font-size": 11, fill: "var(--text-2)",
    }, String(lab)));
    const [vx, vy] = pt(i, R + 22);
    if (series.length === 1) {
      svg.appendChild(svgEl("text", {
        x: vx, y: vy + 17, "text-anchor": Math.abs(vx - cx) < 6 ? "middle" : vx > cx ? "start" : "end",
        "font-size": 10.5, fill: "var(--text-3)",
      }, String(series[0].data[i])));
    }
  });
  return svg;
}

/* --------------------- streaming markdown renderer ---------------------- */
const PURIFY_CFG = {
  ADD_ATTR: ["target", "rel"],
  FORBID_TAGS: ["style", "form", "input", "iframe", "object", "embed", "script"],
  FORBID_ATTR: ["onerror", "onload", "onclick", "srcset"],
};

class MarkdownStream {
  constructor(el) {
    this.el = el;
    this.el.classList.add("md");
    this.raw = "";
    this.blocks = [];      // { raw, node, final }
    this.done = false;
    this.unsafe = false;
    this._timer = null;
  }
  push(text) {
    if (this.unsafe) return;
    this.raw += text;
    if (this._timer) return;
    this._timer = setTimeout(() => { this._timer = null; this.render(); }, 60);
  }
  end() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    this.done = true;
    this.render();
  }
  setRaw(text) { this.raw = text; this.done = true; this.blocks = []; this.el.replaceChildren(); this.render(); }

  render() {
    if (this.unsafe) return;
    const src = this.done ? this.raw : repairMarkdown(this.raw);
    let tokens;
    try { tokens = marked.lexer(src); } catch { return; }

    tokens.forEach((tok, i) => {
      const isLast = i === tokens.length - 1;
      const final = this.done || !isLast;
      const prev = this.blocks[i];
      if (prev && prev.raw === tok.raw && prev.final === final) return;

      const arr = [tok];
      arr.links = tokens.links || {};
      let html;
      try { html = marked.parser(arr); } catch { return; }

      const clean = DOMPurify.sanitize(html, PURIFY_CFG);
      if (DOMPurify.removed && DOMPurify.removed.length) {
        this.unsafe = true;
        const warn = document.createElement("div");
        warn.className = "error-box";
        warn.textContent =
          "Rendering stopped: the response contained active HTML that was stripped for safety.";
        this.el.appendChild(warn);
        return;
      }

      const holder = document.createElement("div");
      holder.innerHTML = clean;
      const base = holder.childElementCount === 1 ? holder.firstElementChild : holder;
      const node = decorate(base, tok, final);

      if (prev) this.el.replaceChild(node, prev.node);
      else this.el.appendChild(node);
      this.blocks[i] = { raw: tok.raw, node, final };
    });

    // model rewrote history (rare) — drop stale trailing nodes
    while (this.blocks.length > tokens.length) {
      const extra = this.blocks.pop();
      if (extra.node.parentNode === this.el) this.el.removeChild(extra.node);
    }
  }
}

const NUMERIC = /^-?[\d.,%×x+/\s-]+$/;

/** Turn one parsed block into its final DOM node. Returns the node to insert. */
function decorate(node, tok, final) {
  if (tok.type === "code") {
    const lang = (tok.lang || "").trim().split(/\s+/)[0].toLowerCase();

    // deferred renderers: nothing is drawn until the closing fence arrives
    if (lang === "mermaid" || lang === "chart") {
      const fig = document.createElement("div");
      fig.className = "figure pending";
      if (!final) {
        fig.textContent = lang === "chart" ? "building chart…" : "drawing diagram…";
        return fig;
      }
      if (lang === "chart") {
        // Malformed JSON is the model's fault but the user's problem, so degrade
        // to the same numbers as a table rather than printing a parser error.
        let spec = null;
        try { spec = JSON.parse(tok.text); } catch { /* handled below */ }
        const bad = chartSpecProblem(spec);
        if (!bad) return renderChart(spec);
        return chartFallback(spec, bad);
      }
      fig.classList.remove("pending");
      if (mermaidReady) {
        const id = "m" + Math.random().toString(36).slice(2);
        mermaid.render(id, tok.text)
          .then(({ svg }) => {
            fig.innerHTML = DOMPurify.sanitize(svg, {
              USE_PROFILES: { svg: true, svgFilters: true, html: true },
            });
          })
          .catch(e => { fig.classList.add("pending"); fig.textContent = "diagram error: " + e.message; });
      }
      return fig;
    }

    // ordinary code block: highlight + language tag
    const pre = node.tagName === "PRE" ? node : node.querySelector("pre");
    if (pre) {
      const code = pre.querySelector("code");
      if (code && lang && hljs.getLanguage(lang)) {
        try {
          code.innerHTML = hljs.highlight(code.textContent, { language: lang }).value;
          code.classList.add("hljs");
        } catch { /* leave plain */ }
      }
      const wrap = document.createElement("div");
      wrap.className = "code-wrap";
      wrap.appendChild(pre);
      const tag = document.createElement("span");
      tag.className = "code-lang";
      tag.textContent = lang || "text";
      wrap.appendChild(tag);
      return wrap;
    }
  }

  // tables: horizontal scroll container + right-aligned numeric columns
  if (tok.type === "table" && node.tagName === "TABLE") {
    const heads = [...node.querySelectorAll("thead th")];
    const rows = [...node.querySelectorAll("tbody tr")];
    heads.forEach((th, c) => {
      const vals = rows.map(r => (r.children[c]?.textContent || "").trim()).filter(Boolean);
      if (vals.length && vals.every(v => NUMERIC.test(v))) {
        th.classList.add("num");
        rows.forEach(r => r.children[c]?.classList.add("num"));
      }
    });
    finish(node);
    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    scroll.appendChild(node);
    return scroll;
  }

  finish(node);
  return node;
}

/** External links + math, applied in place. */
function finish(node) {
  node.querySelectorAll?.("a[href^='http']").forEach(a => {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  });
  if (typeof renderMathInElement === "function") {
    try {
      renderMathInElement(node, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
      });
    } catch { /* ignore */ }
  }
}

/* ----------------------------- credentials ------------------------------
   Two ways in, both optional depending on how the server was started:
     · the server's own key, unlocked by a shared password (APP_PASSWORD)
     · the visitor's own OpenRouter key, held in this browser only
   Nothing is stored server-side either way.
   ------------------------------------------------------------------------ */
const KEY_STORE = "pokedex-own-key";
const PW_STORE = "pokedex-password";
const MODE_STORE = "pokedex-auth-choice";

const creds = {
  get mode() { return localStorage.getItem(MODE_STORE) || "shared"; },
  set mode(v) { localStorage.setItem(MODE_STORE, v); },
  get key() { return localStorage.getItem(KEY_STORE) || ""; },
  set key(v) { v ? localStorage.setItem(KEY_STORE, v) : localStorage.removeItem(KEY_STORE); },
  get password() { return localStorage.getItem(PW_STORE) || ""; },
  set password(v) { v ? localStorage.setItem(PW_STORE, v) : localStorage.removeItem(PW_STORE); },
  clear() {
    localStorage.removeItem(KEY_STORE);
    localStorage.removeItem(PW_STORE);
    localStorage.removeItem(MODE_STORE);
  },
};

/** Headers carrying whichever credential applies. */
function authHeaders() {
  const h = {};
  if (creds.mode === "own" && creds.key) h["X-Api-Key"] = creds.key;
  else if (creds.password) h["X-App-Password"] = creds.password;
  return h;
}

let authMode = "open";   // what the server told us it needs

const dlg = document.getElementById("settings-dialog");
const pwInput = document.getElementById("app-password");
const keyInput = document.getElementById("own-key");
const fieldPw = document.getElementById("field-password");
const fieldKey = document.getElementById("field-key");
const statusEl = document.getElementById("settings-status");
const modeNote = document.getElementById("mode-note");
const segBtns = [...document.querySelectorAll(".seg-btn")];

function paintSettings(mode) {
  segBtns.forEach(b => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  fieldPw.hidden = mode !== "shared";
  fieldKey.hidden = mode !== "own";
}
segBtns.forEach(b => b.addEventListener("click", () => {
  statusEl.textContent = "";
  statusEl.className = "dialog-status";
  paintSettings(b.dataset.mode);
}));

const NO_SHARED = new Set(["byok", "locked"]);  // nothing a password could unlock

const MODE_NOTE = {
  open: "This server spends its own key with no password (ALLOW_OPEN is on). You can still point it at your own key instead.",
  password: "This server's key is password-protected. Enter the password, or use your own key instead.",
  locked: "This server has a key but it is locked — no password was configured, so it will not be spent. Use your own OpenRouter key.",
  byok: "This server has no key of its own. Add your own OpenRouter key to use it.",
};

function openSettings() {
  const sharedUseless = NO_SHARED.has(authMode);
  const mode = sharedUseless ? "own" : creds.mode;
  paintSettings(mode);
  segBtns.forEach(b => {
    if (b.dataset.mode === "shared") b.disabled = sharedUseless;
  });
  pwInput.value = creds.password;
  keyInput.value = creds.key;
  statusEl.textContent = "";
  statusEl.className = "dialog-status";
  modeNote.textContent = MODE_NOTE[authMode] || MODE_NOTE.byok;
  if (!dlg.open) dlg.showModal();
}
document.getElementById("settings").addEventListener("click", openSettings);
document.getElementById("settings-cancel").addEventListener("click", () => dlg.close());
document.getElementById("settings-clear").addEventListener("click", () => {
  creds.clear();
  pwInput.value = "";
  keyInput.value = "";
  statusEl.className = "dialog-status ok";
  statusEl.textContent = "Forgotten. Nothing is stored in this browser any more.";
});

document.getElementById("settings-save").addEventListener("click", async () => {
  const btn = document.getElementById("settings-save");
  const chosen = segBtns.find(b => b.getAttribute("aria-selected") === "true").dataset.mode;
  const headers = chosen === "own"
    ? { "X-Api-Key": keyInput.value.trim() }
    : { "X-App-Password": pwInput.value };
  if (chosen === "own" && !headers["X-Api-Key"]) {
    statusEl.className = "dialog-status err";
    statusEl.textContent = "Paste a key first.";
    return;
  }
  btn.disabled = true;
  statusEl.className = "dialog-status";
  statusEl.textContent = "Checking…";
  try {
    const r = await fetch("/api/verify", { method: "POST", headers });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    creds.mode = chosen;
    if (chosen === "own") { creds.key = keyInput.value.trim(); }
    else { creds.password = pwInput.value; }
    statusEl.className = "dialog-status ok";
    statusEl.textContent = `Working — using the ${body.using}.`;
    await loadModels();
    setTimeout(() => dlg.close(), 700);
  } catch (e) {
    statusEl.className = "dialog-status err";
    statusEl.textContent = String(e.message || e);
  } finally {
    btn.disabled = false;
  }
});


/* ------------------------------ chat state ------------------------------ */
const thread = document.getElementById("thread");
const welcome = document.getElementById("welcome");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const modelSel = document.getElementById("model");
const convList = document.getElementById("conv-list");
const sidebar = document.getElementById("sidebar");
const scrim = document.getElementById("scrim");
const hint = document.getElementById("hint");

let conv = null;           // the active conversation record from `store`
let rendered = [];         // [{role, content, mdEl}] for re-render on theme change
let busy = false;
let inflight = null;       // AbortController for the current request

/** Messages sent upstream. Derived from the conversation, never a parallel copy. */
const historyOf = c => (c?.messages ?? []).map(m => ({ role: m.role, content: m.content }));

const SUGGESTIONS = [
  "Which starter final evolution has the highest Attack + Special Attack summed?",
  "Which dual typing can hit the most types super-effectively with STAB — and does a Pokemon with it exist?",
  "Compare Garchomp, Dragapult and Baxcalibur's base stats, and tell me which is the better Gen 9 sweeper.",
  "Show Eevee's full evolution line with every evolution requirement.",
  "What is the strongest physical Fire move that has 100% accuracy and no drawback?",
];
const sugBox = document.getElementById("suggestions");
SUGGESTIONS.forEach(s => {
  const b = document.createElement("button");
  b.className = "suggestion";
  b.type = "button";
  b.textContent = s;
  b.onclick = () => { input.value = s; autosize(); send(); };
  sugBox.appendChild(b);
});

/* Composer auto-grow.
   `scrollHeight` is not usable here: the textarea is a flex item, and once it is
   collapsed to measure it, some engines report the *document's* scroll height
   instead of the content's — which pins the composer open at its 200px maximum
   for the rest of the session. So measure a hidden mirror div that carries the
   same font and width, and set the height from that. Deterministic everywhere. */
const MAX_COMPOSER_H = 200;
let mirror = null;

function measureText(text) {
  if (!mirror) {
    mirror = document.createElement("div");
    mirror.setAttribute("aria-hidden", "true");
    mirror.style.cssText =
      "position:absolute;visibility:hidden;pointer-events:none;left:-9999px;top:0;" +
      "white-space:pre-wrap;overflow-wrap:break-word;";
    document.body.appendChild(mirror);
  }
  const cs = getComputedStyle(input);
  for (const p of ["fontFamily", "fontSize", "fontWeight", "lineHeight",
                   "letterSpacing", "paddingTop", "paddingBottom"]) {
    mirror.style[p] = cs[p];
  }
  mirror.style.width = input.clientWidth + "px";
  // a trailing newline needs a line of its own
  mirror.textContent = text.endsWith("\n") ? text + " " : text || " ";
  return mirror.getBoundingClientRect().height;
}

function autosize() {
  const h = Math.min(measureText(input.value), MAX_COMPOSER_H);
  input.style.height = h + "px";
  input.style.overflowY = h >= MAX_COMPOSER_H ? "auto" : "hidden";
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
document.getElementById("composer").addEventListener("submit", e => { e.preventDefault(); send(); });

let stick = true;
thread.addEventListener("scroll", () => {
  stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 90;
});
function scrollDown() { if (stick) thread.scrollTop = thread.scrollHeight; }

function rerenderAll() {
  if (typeof marked === "undefined") return;
  for (const r of rendered) {
    if (r.role !== "assistant" || !r.mdEl) continue;
    const ms = new MarkdownStream(r.mdEl);
    r.mdEl.replaceChildren();
    ms.setRaw(r.content);
  }
}

/* --------------------------- conversation list --------------------------- */
function closeSidebar() {
  sidebar.classList.remove("open");
  scrim.hidden = true;
}
document.getElementById("menu").addEventListener("click", () => {
  sidebar.classList.toggle("open");
  scrim.hidden = !sidebar.classList.contains("open");
});
scrim.addEventListener("click", closeSidebar);

function paintConvList() {
  const list = store.all();
  convList.replaceChildren();
  if (!list.length) {
    const empty = document.createElement("p");
    empty.className = "conv-empty";
    empty.textContent = "No saved chats yet.";
    convList.appendChild(empty);
    return;
  }
  for (const c of list) {
    const row = document.createElement("div");
    row.className = "conv" + (c.id === conv?.id ? " active" : "");

    const open = document.createElement("button");
    open.type = "button";
    open.className = "conv-open";
    open.title = c.title;
    const t = document.createElement("span");
    t.className = "conv-title";
    t.textContent = c.title;
    const meta = document.createElement("span");
    meta.className = "conv-meta";
    meta.textContent = `${c.messages.length >> 1 || 0} turns · ${relTime(c.updated)}`;
    open.append(t, meta);
    open.onclick = () => { openConversation(c.id); closeSidebar(); };

    const del = document.createElement("button");
    del.type = "button";
    del.className = "conv-del";
    del.title = "Delete";
    del.textContent = "×";
    del.onclick = e => {
      e.stopPropagation();
      store.remove(c.id);
      if (conv?.id === c.id) startNewChat();
      else paintConvList();
    };

    row.append(open, del);
    convList.appendChild(row);
  }
}

function relTime(ms) {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function clearThread() {
  thread.querySelectorAll(".msg").forEach(n => n.remove());
  rendered = [];
}

function startNewChat() {
  if (busy) stopGeneration();
  conv = null;
  store.setActive("");
  clearThread();
  welcome.style.display = "";
  paintConvList();
  input.focus();
}
document.getElementById("newchat").addEventListener("click", () => { startNewChat(); closeSidebar(); });

function openConversation(id) {
  if (busy) stopGeneration();
  const c = store.get(id);
  if (!c) return startNewChat();
  conv = c;
  store.setActive(id);
  clearThread();
  welcome.style.display = "none";
  for (const m of c.messages) {
    if (m.role === "user") {
      renderUserBubble(m.content);
    } else {
      const { mdEl } = renderAssistantShell();
      const ms = new MarkdownStream(mdEl);
      ms.setRaw(m.content);
      rendered.push({ role: "assistant", content: m.content, mdEl });
    }
  }
  // a reopened chat keeps the model it last used, unless you have since changed it
  if (c.model && [...modelSel.options].some(o => o.value === c.model)) modelSel.value = c.model;
  paintConvList();
  stick = true;
  scrollDown();
}

function renderUserBubble(text) {
  const el = document.createElement("div");
  el.className = "msg msg-user";
  const inner = document.createElement("div");
  inner.textContent = text;
  el.appendChild(inner);
  thread.appendChild(el);
  rendered.push({ role: "user", content: text });
  return el;
}

function renderAssistantShell() {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant";
  const toolsBox = document.createElement("div");
  toolsBox.className = "tools";
  const reasonBox = document.createElement("div");
  reasonBox.className = "reasoning";
  reasonBox.style.display = "none";
  const mdEl = document.createElement("div");
  wrap.append(toolsBox, reasonBox, mdEl);
  thread.appendChild(wrap);
  return { wrap, toolsBox, reasonBox, mdEl };
}

document.getElementById("export").addEventListener("click", () => {
  const blob = new Blob([store.exportAll()], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `pokedex-chats-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
});

document.getElementById("wipe").addEventListener("click", () => {
  if (!confirm("Delete every saved conversation in this browser? This cannot be undone.")) return;
  store.clearAll();
  startNewChat();
});

/* ------------------------------ tool cards ------------------------------ */
function toolCard(container, ev) {
  const card = document.createElement("div");
  card.className = "tool running";
  card.dataset.id = ev.id;
  const head = document.createElement("div");
  head.className = "tool-head";
  const spin = document.createElement("i"); spin.className = "spin";
  const kind = document.createElement("span"); kind.className = "tool-kind";
  kind.textContent = ev.name === "sql_query" ? "sql" : ev.name === "web_search" ? "web" : "art";
  const label = document.createElement("span"); label.className = "tool-label"; label.textContent = ev.label;
  const meta = document.createElement("span"); meta.className = "tool-meta";
  head.append(spin, kind, label, meta);
  const body = document.createElement("div"); body.className = "tool-body";
  head.onclick = () => card.classList.toggle("open");
  card.append(head, body);
  container.appendChild(card);
  return card;
}

function fillToolBody(card, ev) {
  const body = card.querySelector(".tool-body");
  const res = ev.result || {};
  body.replaceChildren();

  if (ev.name === "sql_query") {
    const args = card._args || {};
    if (args.sql) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.className = "hljs";
      try { code.innerHTML = hljs.highlight(args.sql, { language: "sql" }).value; }
      catch { code.textContent = args.sql; }
      pre.appendChild(code);
      body.appendChild(pre);
    }
    if (res.error) { body.appendChild(errLine(res.error)); return; }
    if (res.columns) body.appendChild(resultTable(res));
  } else if (ev.name === "web_search") {
    if (res.error) { body.appendChild(errLine(res.error)); return; }
    const p = document.createElement("div");
    p.className = "md";
    p.innerHTML = DOMPurify.sanitize(marked.parse(res.summary || ""), PURIFY_CFG);
    p.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    body.appendChild(p);
    if (res.sources?.length) {
      const ul = document.createElement("ul");
      res.sources.forEach(s => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = s.url; a.textContent = s.title || s.url;
        a.target = "_blank"; a.rel = "noopener noreferrer";
        li.appendChild(a); ul.appendChild(li);
      });
      body.appendChild(ul);
    }
  } else {
    if (res.image_url) {
      const img = document.createElement("img");
      img.src = res.image_url; img.alt = res.species_name || res.item_name || "";
      img.style.maxWidth = "120px";
      body.appendChild(img);
    } else {
      body.appendChild(errLine(res.error || "not found"));
    }
  }
}

function errLine(msg) {
  const d = document.createElement("div");
  d.className = "error-box";
  d.textContent = msg;
  return d;
}

function resultTable(res) {
  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  const t = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  res.columns.forEach(c => { const th = document.createElement("th"); th.textContent = c; hr.appendChild(th); });
  thead.appendChild(hr);
  const tb = document.createElement("tbody");
  res.rows.slice(0, 60).forEach(r => {
    const tr = document.createElement("tr");
    r.forEach(v => {
      const td = document.createElement("td");
      td.textContent = v === null ? "—" : String(v);
      if (typeof v === "number") td.className = "num";
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.append(thead, tb);
  scroll.appendChild(t);
  if (res.rows.length > 60 || res.truncated) {
    const note = document.createElement("div");
    note.style.cssText = "font-size:11px;color:var(--text-3);padding:6px 10px";
    note.textContent = `showing 60 of ${res.row_count}${res.truncated ? "+" : ""} rows`;
    scroll.appendChild(note);
  }
  return scroll;
}

/* -------------------------------- send ----------------------------------
   The conversation is committed only when an exchange completes. A failed send
   leaves it untouched and marks the message retryable — otherwise one 401 wedges
   a dangling `user` turn into the history, every later request re-sends it, and
   providers either reject the two-in-a-row roles or answer the wrong question.
   ------------------------------------------------------------------------- */
function retryBar(onRetry) {
  const bar = document.createElement("div");
  bar.className = "retry-bar";
  const label = document.createElement("span");
  label.textContent = "Not sent.";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "retry-btn";
  btn.textContent = "Retry";
  btn.onclick = onRetry;
  bar.append(label, btn);
  return bar;
}

function setBusy(on) {
  busy = on;
  sendBtn.hidden = on;
  stopBtn.hidden = !on;
  hint.textContent = on
    ? "Generating… press Esc or Stop to cancel"
    : "Enter to send · Shift+Enter for a new line";
}

function stopGeneration() {
  if (inflight) inflight.abort();
}
stopBtn.addEventListener("click", stopGeneration);
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && busy) stopGeneration();
});

async function send(retryText = null) {
  const text = retryText ?? input.value.trim();
  if (!text || busy) return;
  const demo = text === "/demo";
  setBusy(true);
  if (!retryText) { input.value = ""; input.style.height = ""; autosize(); }
  welcome.style.display = "none";

  // The model is read here, at send time — so switching it mid-conversation
  // takes effect from this message on, and earlier turns keep their own answers.
  const chosenModel =
    modelSel.value && modelSel.value !== SHOW_ALL ? modelSel.value : undefined;

  const userEl = renderUserBubble(text);
  const renderedUser = rendered[rendered.length - 1];

  // the turn we are *proposing*; only merged into the conversation on success
  const outbound = [...historyOf(conv), { role: "user", content: text }];
  stick = true; scrollDown();

  const { wrap, toolsBox, reasonBox, mdEl } = renderAssistantShell();
  const stream = new MarkdownStream(mdEl);
  const record = { role: "assistant", content: "", mdEl };
  rendered.push(record);

  const cards = new Map();
  let reachedStream = false;
  let aborted = false;
  inflight = new AbortController();

  try {
    // adjacent same-role turns are malformed for every provider; catch it here
    // rather than paying for a confused answer
    for (let i = 1; i < outbound.length; i++) {
      if (outbound[i].role === outbound[i - 1].role) {
        console.error("conversation invariant broken: adjacent", outbound[i].role, outbound);
        break;
      }
    }
    const resp = demo
      ? await fetch("/api/demo", { signal: inflight.signal })
      : await fetch("/api/chat", {
          method: "POST",
          signal: inflight.signal,
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ messages: outbound, model: chosenModel }),
        });
    if (!resp.ok || !resp.body) {
      const body = await resp.json().catch(() => null);
      const msg = body?.detail || `server ${resp.status}`;
      if (resp.status === 401) setTimeout(openSettings, 200);
      throw new Error(msg);
    }
    reachedStream = true;

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        // SSE allows several `data:` lines per event; concatenate them with \n
        // rather than taking the first and silently dropping the rest.
        const data = part
          .split("\n")
          .filter(l => l.startsWith("data:"))
          .map(l => l.slice(5).replace(/^ /, ""))
          .join("\n");
        if (!data) continue;
        let ev;
        try { ev = JSON.parse(data); } catch { continue; }

        if (ev.type === "delta") {
          record.content += ev.text;
          stream.push(ev.text);
          scrollDown();
        } else if (ev.type === "reasoning") {
          reasonBox.style.display = "";
          reasonBox.textContent += ev.text;
          reasonBox.scrollTop = reasonBox.scrollHeight;
          scrollDown();
        } else if (ev.type === "tool_start") {
          const card = toolCard(toolsBox, ev);
          card._args = ev.args || {};
          cards.set(ev.id, card);
          scrollDown();
        } else if (ev.type === "tool_end") {
          const card = cards.get(ev.id);
          if (card) {
            card.classList.remove("running");
            if (String(ev.summary).startsWith("error")) card.classList.add("failed");
            card.querySelector(".tool-meta").textContent = `${ev.summary} · ${ev.ms} ms`;
            fillToolBody(card, ev);
          }
          scrollDown();
        } else if (ev.type === "error") {
          wrap.insertBefore(errLine(ev.message), mdEl);
          scrollDown();
        }
      }
    }
    stream.end();
    reasonBox.style.display = "none";
    toolsBox.querySelectorAll(".tool.running").forEach(c => c.classList.remove("running"));

    if (demo) return;                       // canned stream: never enters the history
    if (!record.content.trim()) throw new Error("The model returned an empty response.");

    // commit both turns, or neither
    if (!conv) conv = store.create(chosenModel || modelSel.value);
    const messages = [...conv.messages,
      { role: "user", content: text },
      { role: "assistant", content: record.content }];
    const title = conv.messages.length ? conv.title : store.titleFor(text);
    conv = store.update(conv.id, { messages, title, model: chosenModel || conv.model });
    paintConvList();
  } catch (e) {
    aborted = e.name === "AbortError";
    if (aborted) {
      // Keep whatever streamed. A stopped answer is a real answer; it just ends early.
      stream.end();
      reasonBox.style.display = "none";
      toolsBox.querySelectorAll(".tool.running").forEach(c => c.classList.remove("running"));
      const note = document.createElement("div");
      note.className = "stopped-note";
      note.textContent = "Stopped.";
      wrap.appendChild(note);
      if (!demo && record.content.trim()) {
        if (!conv) conv = store.create(chosenModel || modelSel.value);
        const messages = [...conv.messages,
          { role: "user", content: text },
          { role: "assistant", content: record.content }];
        const title = conv.messages.length ? conv.title : store.titleFor(text);
        conv = store.update(conv.id, { messages, title, model: chosenModel || conv.model });
        paintConvList();
        return;
      }
    } else {
      wrap.appendChild(errLine(String(e.message || e)));
    }
    // roll the proposed turn back out of the re-render list, and offer a retry
    const i = rendered.indexOf(renderedUser);
    if (i !== -1) rendered.splice(i, 2);
    userEl.classList.add("failed");
    if (!reachedStream || aborted) wrap.remove();
    userEl.appendChild(retryBar(() => {
      userEl.remove();
      if (reachedStream && !aborted) wrap.remove();
      send(text);
    }));
  } finally {
    inflight = null;
    setBusy(false);
    autosize();
    input.focus();
    scrollDown();
  }
}

/* ------------------------------ boot ------------------------------------ */
const SHOW_ALL = "__all__";
let showingAll = false;

const money = v => `$${v.toFixed(2)}`;

async function loadModels(all = showingAll) {
  try {
    const url = all ? "/api/models?all=true" : "/api/models";
    const resp = await fetch(url, { headers: authHeaders() });
    if (resp.status === 401) {
      // The catalogue is behind the same credential as everything else, so before
      // you unlock there is nothing to list. Say that, don't cry "unavailable".
      modelSel.replaceChildren();
      const o = document.createElement("option");
      o.textContent = "unlock to choose a model";
      modelSel.appendChild(o);
      modelSel.classList.add("degraded");
      modelSel.title = "Open Settings and enter the password, or add your own API key.";
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const m = await resp.json();
    showingAll = !m.curated;
    const keep = modelSel.value && modelSel.value !== SHOW_ALL ? modelSel.value : "";
    modelSel.replaceChildren();

    for (const mo of m.models) {
      const o = document.createElement("option");
      o.value = mo.id;
      // "DeepSeek V3.2 · $0.27/$0.40" — in/out dollars per million tokens
      const price = mo.price_out ? ` · ${money(mo.price_in)}/${money(mo.price_out)}` : "";
      o.textContent = showingAll ? mo.id : `${mo.label}${price}`;
      if (mo.note) o.title = `${mo.id} — ${mo.note}\nper million tokens: in ${money(mo.price_in)}, out ${money(mo.price_out)}`;
      if (mo.id === (keep || conv?.model || m.current)) o.selected = true;
      modelSel.appendChild(o);
    }
    if (!modelSel.value && modelSel.options.length) modelSel.selectedIndex = 0;

    const toggle = document.createElement("option");
    toggle.value = SHOW_ALL;
    toggle.textContent = showingAll ? "← back to the shortlist" : "all models on OpenRouter…";
    modelSel.appendChild(toggle);
    modelSel.classList.remove("degraded");
  } catch (e) {
    // Say why, and let the user retry, rather than showing a dead select box.
    modelSel.replaceChildren();
    const o = document.createElement("option");
    o.textContent = "models unavailable — click to retry";
    modelSel.appendChild(o);
    modelSel.classList.add("degraded");
    modelSel.title = String(e.message || e);
  }
}

let lastModel = "";
modelSel.addEventListener("change", async () => {
  if (modelSel.classList.contains("degraded")) { await loadModels(); return; }
  if (modelSel.value !== SHOW_ALL) {
    lastModel = modelSel.value;
    // remember the switch on the open conversation, but do not touch messages:
    // it applies from the next one
    if (conv) conv = store.update(conv.id, { model: lastModel });
    return;
  }
  await loadModels(!showingAll);
  if (lastModel && [...modelSel.options].some(o => o.value === lastModel)) {
    modelSel.value = lastModel;
  }
});

(async function boot() {
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  document.getElementById("theme").addEventListener("click", () =>
    applyTheme(currentTheme() === "dark" ? "light" : "dark")
  );

  try {
    const resp = await fetch("/api/config");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const h = await resp.json();
    const d = h.db;
    authMode = h.auth_mode || "open";
    document.getElementById("dbstat").textContent =
      `${d.species} species · ${d.forms} forms · ${d.moves} moves · gen ${d.latest_generation}`;
  } catch (e) {
    // A server that is up but broken must not look like a healthy one.
    authMode = "byok";
    document.getElementById("dbstat").textContent = "server unreachable";
    thread.prepend(errLine(
      `Could not reach the server (${e.message || e}). Reload once it is running.`
    ));
  }

  await loadModels();

  const active = store.activeId();
  if (active && store.get(active)) openConversation(active);
  else paintConvList();

  // nothing usable yet → ask up front rather than failing on the first question
  const haveCred = NO_SHARED.has(authMode)
    ? Boolean(creds.key)
    : (creds.mode === "own" && creds.key) || creds.password;
  if (authMode !== "open" && !haveCred) openSettings();

  input.focus();
})();
