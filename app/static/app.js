// Chat client: streams /api/chat over fetch, renders answers as DOM nodes (never innerHTML).
"use strict";

const MAX_HISTORY = 12; // 6 turns, matching the server's trim
const $ = (sel, root = document) => root.querySelector(sel);

const els = {
  welcome: $("#welcome"),
  thread: $("#thread"),
  form: $("#composer"),
  field: $("#question"),
  send: $("#send"),
  help: $("#composer-help"),
  helpText: $("#help-text"),
  count: $("#count"),
  newChat: $("#new-chat"),
  passRow: $("#passcode-row"),
  pass: $("#passcode"),
  phrase: $("#snapshot-phrase"),
};
const HELP = els.helpText.textContent;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let history = [];
let controller = null;
let turnSeq = 0;

// ---------- Safe markdown: a small subset, built as nodes ----------

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[(?:\d+(?:\s*,\s*\d+)*)\]|\[[^\]]+\]\([^)]+\))/g;

function inline(text, parent, ctx) {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) {
      const b = document.createElement("strong");
      b.textContent = tok.slice(2, -2);
      parent.append(b);
    } else if (tok.startsWith("`")) {
      const c = document.createElement("code");
      c.textContent = tok.slice(1, -1);
      parent.append(c);
    } else if (/^\[\d/.test(tok)) {
      for (const n of tok.slice(1, -1).split(",").map((s) => Number(s.trim()))) {
        parent.append(n >= 1 && n <= ctx.sources.length ? citeChip(n, ctx) : `[${n}]`);
      }
    } else {
      // A link the model wrote: show its text only. Real links come from the source list.
      parent.append(tok.slice(1, tok.indexOf("](")));
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function render(markdown, target, ctx) {
  const frag = document.createDocumentFragment();
  let list = null;
  for (const raw of markdown.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const kind = bullet ? "UL" : "OL";
      if (!list || list.tagName !== kind) {
        list = document.createElement(kind.toLowerCase());
        frag.append(list);
      }
      const li = document.createElement("li");
      inline((bullet || numbered)[1], li, ctx);
      list.append(li);
      continue;
    }
    list = null;
    if (!line) continue;
    const p = document.createElement("p");
    const heading = line.match(/^#{1,6}\s+(.*)/);
    if (heading) {
      const b = document.createElement("strong");
      inline(heading[1], b, ctx);
      p.append(b);
    } else {
      inline(line, p, ctx);
    }
    frag.append(p);
  }
  target.replaceChildren(frag);
}

function citeChip(n, ctx) {
  const a = document.createElement("a");
  a.className = "cite";
  a.href = `#src-${ctx.id}-${n}`;
  a.textContent = String(n);
  a.setAttribute("aria-label", `Source ${n}: ${ctx.sources[n - 1].title}`);
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const row = document.getElementById(`src-${ctx.id}-${n}`);
    if (!row) return;
    document.querySelectorAll(".source.is-current").forEach((r) => r.classList.remove("is-current"));
    row.classList.add("is-current");
    row.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    row.querySelector("a").focus({ preventScroll: true });
  });
  return a;
}

// ---------- Turns ----------

function addUserTurn(text) {
  const node = $("#tpl-user").content.firstElementChild.cloneNode(true);
  $(".bubble", node).textContent = text;
  els.thread.append(node);
}

function addAnswerTurn() {
  const node = $("#tpl-answer").content.firstElementChild.cloneNode(true);
  els.thread.append(node);
  return {
    id: ++turnSeq,
    node,
    status: $(".status", node),
    answer: $(".answer", node),
    error: $(".error", node),
    errorText: $(".error__text", node),
    retry: $(".retry", node),
    sourcesBox: $(".sources", node),
    sourcesList: $(".sources__list", node),
    more: $(".sources__more", node),
    rest: $(".sources__rest", node),
    sources: [],
    text: "",
  };
}

const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const SITE = { darglobal: "Dar Global", wasalt: "Wasalt" };

function renderSources(ctx) {
  const rows = ctx.sources.map((s) => {
    const row = $("#tpl-source").content.firstElementChild.cloneNode(true);
    row.id = `src-${ctx.id}-${s.n}`;
    $(".source__n", row).textContent = String(s.n);
    const link = $(".source__title", row);
    link.textContent = s.title || s.url;
    if (/^https:\/\//.test(s.url)) link.href = s.url; // index URLs are https; anything else stays text
    $(".source__site", row).textContent = SITE[s.site] || s.site;
    $(".source__kind", row).textContent = s.kind === "listing" ? "listing" : "page";
    const time = $(".source__date", row);
    time.dateTime = s.snapshot_date;
    const d = new Date(`${s.snapshot_date}T00:00:00Z`);
    time.textContent = Number.isNaN(d.getTime()) ? s.snapshot_date : dateFmt.format(d);
    return row;
  });
  ctx.sourcesList.replaceChildren(...rows);
  ctx.sourcesBox.hidden = rows.length === 0;
}

// Once the answer is in, keep cited sources in the list and fold the rest away.
function splitSources(ctx) {
  const cited = new Set();
  for (const m of ctx.text.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    m[1].split(",").forEach((n) => cited.add(Number(n.trim())));
  }
  if (!cited.size) return; // nothing cited: leave every source in view
  const rows = [...ctx.sourcesList.children];
  const uncited = rows.filter((row) => !cited.has(Number(row.id.split("-").pop())));
  if (!uncited.length || uncited.length === rows.length) return;
  ctx.rest.replaceChildren(...uncited);
  $("summary", ctx.more).textContent = `Also searched, not cited (${uncited.length})`;
  ctx.more.hidden = false;
}

function showError(ctx, message, { retry = true } = {}) {
  ctx.status.textContent = "";
  ctx.errorText.textContent = message;
  ctx.retry.hidden = !retry;
  ctx.error.hidden = false;
  ctx.node.setAttribute("aria-busy", "false");
}

// ---------- Scrolling ----------

function nearBottom() {
  return innerHeight + scrollY >= document.documentElement.scrollHeight - 160;
}
function toBottom() {
  scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
}

// ---------- Sending ----------

function setBusy(busy) {
  els.send.textContent = busy ? "Stop" : "Send";
  els.send.type = busy ? "button" : "submit";
  els.send.setAttribute("aria-busy", String(busy));
  document.querySelectorAll(".example").forEach((b) => (b.disabled = busy));
}

function helpError(message) {
  els.helpText.textContent = message;
  els.help.classList.toggle("is-error", Boolean(message !== HELP));
}

async function ask(question) {
  helpError(HELP);
  els.welcome.hidden = true;
  els.newChat.hidden = false;
  addUserTurn(question);
  const ctx = addAnswerTurn();
  ctx.retry.addEventListener("click", () => {
    ctx.node.previousElementSibling?.remove();
    ctx.node.remove();
    ask(question);
  });
  toBottom();

  const messages = [...history.slice(-MAX_HISTORY), { role: "user", content: question }];
  const headers = { "Content-Type": "application/json" };
  const code = sessionStorage.getItem("access-code");
  if (code) headers["X-Access-Code"] = code;

  controller = new AbortController();
  setBusy(true);
  let finished = false;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({ messages }),
      signal: controller.signal,
    });
    if (!res.ok) {
      await handleHttpError(res, ctx);
      return;
    }
    finished = await readStream(res, ctx);
  } catch (err) {
    if (err.name === "AbortError") {
      ctx.status.textContent = ctx.text ? "Stopped." : "";
      if (!ctx.text) showError(ctx, "Stopped before an answer arrived.");
      ctx.node.setAttribute("aria-busy", "false");
    } else {
      showError(ctx, "The connection dropped. Check your network and try again.");
    }
  } finally {
    setBusy(false);
    controller = null;
    if (finished) {
      history.push({ role: "user", content: question }, { role: "assistant", content: ctx.text });
      history = history.slice(-MAX_HISTORY);
    }
    els.field.focus();
  }
}

async function handleHttpError(res, ctx) {
  let detail = "";
  try {
    const body = await res.json();
    detail = typeof body.detail === "string" ? body.detail : "";
  } catch {
    /* no JSON body */
  }
  if (res.status === 401) {
    sessionStorage.removeItem("access-code");
    els.passRow.hidden = false;
    els.pass.setAttribute("aria-invalid", "true");
    els.pass.focus();
    showError(ctx, "This demo needs an access code. Enter it above, then try again.");
  } else if (res.status === 429) {
    const wait = Number(res.headers.get("Retry-After"));
    const when = wait > 0 && wait < 120 ? ` Try again in ${Math.ceil(wait)} seconds.` : "";
    showError(ctx, `${detail || "Too many messages."}${when}`);
  } else if (res.status === 422) {
    showError(ctx, "That message could not be sent. Remove unusual characters or shorten it.", {
      retry: false,
    });
  } else {
    showError(ctx, "The assistant is unavailable right now. Please try again shortly.");
  }
}

async function readStream(res, ctx) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let queued = false;
  const paint = () => {
    queued = false;
    const stick = nearBottom();
    render(ctx.text, ctx.answer, ctx);
    if (stick) toBottom();
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      const payload = JSON.parse(data);
      if (event === "sources") {
        ctx.sources = payload.sources || [];
        ctx.status.textContent = "Writing the answer…";
        renderSources(ctx);
      } else if (event === "token") {
        ctx.text += payload.text;
        if (!queued) {
          queued = true;
          requestAnimationFrame(paint);
        }
      } else if (event === "done") {
        paint();
        splitSources(ctx);
        ctx.status.textContent = "";
        ctx.node.setAttribute("aria-busy", "false");
        return true;
      } else if (event === "error") {
        paint();
        showError(ctx, payload.message || "Something went wrong. Please try again.");
        return false;
      }
    }
  }
  showError(ctx, "The answer stopped early. Please try again.");
  return false;
}

// ---------- Composer ----------

function grow() {
  els.field.rows = 1;
  while (els.field.scrollHeight > els.field.clientHeight && els.field.rows < 6) els.field.rows += 1;
  const n = els.field.value.length;
  els.count.textContent = n > 1500 ? `${n.toLocaleString("en")} / 2,000` : "";
}

els.field.addEventListener("input", grow);
els.field.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    els.form.requestSubmit();
  }
});

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (controller) return;
  const question = els.field.value.trim();
  if (!question) {
    helpError("Type a question first.");
    els.field.focus();
    return;
  }
  if (!els.passRow.hidden) {
    const code = els.pass.value.trim();
    if (!code) {
      els.pass.setAttribute("aria-invalid", "true");
      els.pass.focus();
      return;
    }
    sessionStorage.setItem("access-code", code);
    els.pass.removeAttribute("aria-invalid");
    els.passRow.hidden = true;
  }
  els.field.value = "";
  grow();
  ask(question);
});

els.send.addEventListener("click", () => {
  if (controller) controller.abort();
});

document.querySelectorAll(".example").forEach((btn) =>
  btn.addEventListener("click", () => {
    els.field.value = btn.textContent;
    els.form.requestSubmit();
  }),
);

els.newChat.addEventListener("click", () => {
  if (controller) controller.abort();
  history = [];
  els.thread.replaceChildren();
  els.welcome.hidden = false;
  els.newChat.hidden = true;
  helpError(HELP);
  els.field.focus();
});

// The snapshot dates come from the index manifest, so the copy never drifts from the data.
fetch("/healthz")
  .then((r) => (r.ok ? r.json() : null))
  .then((h) => {
    const dates = (h?.snapshot_dates || []).map((d) => {
      const t = new Date(`${d}T00:00:00Z`);
      return Number.isNaN(t.getTime())
        ? d
        : new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric" }).format(t);
    });
    if (dates.length) {
      els.phrase.textContent = `a snapshot of the two companies’ public websites taken on ${dates.join(" and ")}`;
    }
  })
  .catch(() => {});
