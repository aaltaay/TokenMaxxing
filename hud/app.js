/* Token HUD front-end. Renders /api/state. No billing or reset math here. */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function $(sel, root) {
  return (root || document).querySelector(sel);
}

function $all(sel, root) {
  return Array.from((root || document).querySelectorAll(sel));
}

function setText(sel, value) {
  const el = $(sel);
  if (el) el.textContent = value == null ? "" : String(value);
}

function setFill(sel, pct, color) {
  const el = $(sel);
  if (!el) return;
  const width = Math.max(0, Math.min(100, Number(pct) || 0));
  el.style.width = `${width}%`;
  if (color) el.style.background = color;
}

function currentTab() {
  const q = new URLSearchParams(location.search).get("tab");
  if (q === "chat" || q === "resets" || q === "cycle") return q;
  const on = $(".segments button.on");
  return on ? on.getAttribute("data-tab") : "cycle";
}

function showTab(name) {
  $all(".segments button").forEach((btn) => {
    const on = btn.getAttribute("data-tab") === name;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  $all("[data-tab-panel]").forEach((panel) => {
    panel.hidden = panel.getAttribute("data-tab-panel") !== name;
  });
}

function rowsHtml(rows, keys, emptyCols) {
  if (!rows || !rows.length) {
    return `<tr><td class="empty" colspan="${emptyCols}">No rows yet</td></tr>`;
  }
  return rows
    .map((row) => {
      const cells = keys
        .map((key, i) => `<td${i === keys.length - 1 ? ' class="wide"' : ""}>${esc(row[key])}</td>`)
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
}

function renderClock(prefix, clock) {
  const root = $(`[data-clock="${prefix}"]`);
  if (!root || !clock) return;
  setText(`[data-clock="${prefix}"] [data-left]`, clock.left || "--");
  const leftEl = $(`[data-clock="${prefix}"] [data-left]`);
  if (leftEl && clock.color) leftEl.style.color = clock.color;
  setText(`[data-clock="${prefix}"] [data-next]`, clock.next || "");
  setText(`[data-clock="${prefix}"] [data-meta]`, clock.meta || "");
  const fill = $("[data-fill]", root);
  if (fill) {
    fill.style.width = `${Math.max(0, Math.min(100, Number(clock.remain_pct) || 0))}%`;
    if (clock.color) fill.style.background = clock.color;
  }
}

function render(state) {
  const banner = $("#banner");
  if (state.banner && state.banner.visible && state.banner.text) {
    banner.hidden = false;
    setText("#banner-text", state.banner.text);
  } else {
    banner.hidden = true;
    setText("#banner-text", "");
  }

  const header = state.header || {};
  setText("#plan", header.plan || "Loading cycle...");
  setText("#header-status", header.status || "");
  setText("#api-pct", header.api_pct_text || "--%");
  setText("#auto-pct", header.auto_pct_text || "--%");
  if (header.api_color) $("#api-pct").style.color = header.api_color;
  if (header.auto_color) $("#auto-pct").style.color = header.auto_color;
  setFill("#api-fill", header.api_pct, header.api_color);
  setFill("#auto-fill", header.auto_pct, header.auto_color);
  setText("#header-note", header.note || "");

  const cycle = state.cycle || {};
  setText("#cycle-status", cycle.status || "");
  setText("#spend-big", cycle.spend || "$--");
  setText("#kv-included", cycle.included || "");
  setText("#kv-ondemand", cycle.ondemand || "");
  setText("#kv-cloud", cycle.cloud || "");
  setText("#kv-interactive", cycle.interactive || "");
  setText("#kv-tokens", cycle.tokens || "");

  $("#chats-body").innerHTML = rowsHtml(cycle.chats, ["dollars", "other", "n", "hl", "name"], 5);
  $("#models-body").innerHTML = rowsHtml(cycle.models, ["dollars", "pool", "in", "out", "model"], 5);
  $("#days-body").innerHTML = rowsHtml(cycle.days, ["date", "dollars", "n"], 3);

  const findings = cycle.findings || [];
  $("#findings").innerHTML = findings.length
    ? findings.map((line) => `<p>${esc(line)}</p>`).join("")
    : `<p class="meta">No findings yet.</p>`;

  const chat = state.chat || {};
  setText("#follow", chat.follow || "Following Cursor selection");
  setText("#chat-model", chat.model || "");
  setText("#chat-pct", chat.pct_text || "--%");
  if (chat.pct_color) $("#chat-pct").style.color = chat.pct_color;
  setFill("#chat-fill", chat.pct, chat.pct_color);
  setText("#chat-input", chat.input || "");
  setText("#chat-billed", chat.billed || "");
  setText("#chat-body", chat.body || "");

  const list = $("#chat-list");
  const recent = chat.recent || [];
  list.innerHTML = recent.length
    ? recent
        .map(
          (row) =>
            `<button type="button" class="chat-row${row.current ? " current" : ""}" data-id="${esc(row.id)}">` +
            `<span>${esc(row.name)}</span>` +
            `${row.current ? '<span class="mark">Now</span>' : ""}` +
            `</button>`
        )
        .join("")
    : `<p class="meta" style="padding:12px">No recent chats.</p>`;

  const resets = state.resets || {};
  ["claude", "codex"].forEach((key) => {
    const card = resets[key] || {};
    const pill = $(`[data-pill="${key}"]`);
    if (pill) {
      pill.textContent = card.pill || "OK";
      pill.className = `pill ${card.pill_kind || "ok"}`;
    }
    renderClock(`${key}-session`, card.session);
    renderClock(`${key}-weekly`, card.weekly);
  });
  setText("#reset-status", resets.status || "");
  setText("#reset-help", resets.help || "");

  const toggles = resets.toggles || {};
  $all("[data-toggle]").forEach((input) => {
    const key = input.getAttribute("data-toggle");
    if (key in toggles) input.checked = !!toggles[key];
    const label = input.closest(".toggle");
    if (label) label.classList.toggle("on", input.checked);
  });
}

async function poll() {
  const res = await fetch("/api/state", { cache: "no-store" });
  if (!res.ok) return;
  render(await res.json());
}

async function act(payload) {
  const res = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) return;
  render(await res.json());
}

function bind() {
  $all(".segments button").forEach((btn) => {
    btn.addEventListener("click", () => showTab(btn.getAttribute("data-tab")));
  });
  $("#btn-refresh").addEventListener("click", () => act({ op: "refresh" }));
  $("#btn-follow").addEventListener("click", () => act({ op: "follow" }));
  $("#btn-buzz").addEventListener("click", () => act({ op: "test_buzz" }));
  $all("[data-mark]").forEach((btn) => {
    btn.addEventListener("click", () => act({ op: "mark_session", provider: btn.getAttribute("data-mark") }));
  });
  $all("[data-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      act({ op: "toggle", key: input.getAttribute("data-toggle"), value: input.checked });
    });
  });
  $("#chat-list").addEventListener("click", (evt) => {
    const row = evt.target.closest("[data-id]");
    if (row) act({ op: "pin", chat_id: row.getAttribute("data-id") });
  });
}

showTab(currentTab());
bind();
poll();
setInterval(() => {
  poll().catch(() => {});
}, 800);
