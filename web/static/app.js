/* Ocean Cruises onboard assistant — web client. */

const loginView = document.getElementById("login-view");
const chatView = document.getElementById("chat-view");
const loginForm = document.getElementById("login-form");
const loginButton = document.getElementById("login-button");
const loginError = document.getElementById("login-error");
const guestIdInput = document.getElementById("guest-id");
const guestChip = document.getElementById("guest-chip");
const messagesEl = document.getElementById("messages");
const suggestionsEl = document.getElementById("suggestions");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendButton = document.getElementById("send-button");
const newConversationBtn = document.getElementById("new-conversation");
const logoutBtn = document.getElementById("logout-button");
const toast = document.getElementById("toast");

let currentGuestId = null;
let busy = false;

/* ── Helpers ────────────────────────────────────────────────────────── */

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/* Minimal markdown: bold, italics, inline code, bullet lines, line breaks. */
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let inList = false;
  for (const line of lines) {
    const bullet = line.match(/^\s*[-•]\s+(.*)$/);
    if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  return out.join("");

  function inline(s) {
    return s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  }
}

function showToast(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { toast.hidden = true; }, 4200);
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setBusy(state) {
  busy = state;
  chatInput.disabled = state;
  sendButton.disabled = state;
}

function addRow(kind, node) {
  const row = document.createElement("div");
  row.className = `row ${kind}`;
  row.appendChild(node);
  messagesEl.appendChild(row);
  scrollToBottom();
  return row;
}

function addBubble(kind, html) {
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = html;
  return addRow(kind, bubble);
}

function addTyping() {
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  return addRow("assistant", bubble);
}

/* ── Structured result rendering ────────────────────────────────────── */

const MONEY_WORDS = /total|spent|amount|charge|price|spend|cost|fee|revenue|balance/i;
const COUNT_WORDS = /count|number|qty|quantity|num_|_num|rank|id$|_id/i;
const PERCENT_WORDS = /percent|pct|_rate|^rate|ratio/i;

function looksLikePercent(column) {
  return PERCENT_WORDS.test(column);
}

function looksLikeMoney(value, column) {
  if (COUNT_WORDS.test(column) || looksLikePercent(column)) return false;
  if (!Number.isInteger(value)) return true;             // any other fractional value is almost certainly currency
  return MONEY_WORDS.test(column);                        // whole-number money (e.g. "total": 500) still needs a $ sign
}

function fmtCell(value, column, kind) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (kind === "count") return value.toLocaleString("en-US");
    if (kind === "percent") {
      return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "%";
    }
    if (kind === "currency") {
      return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    // No explicit kind from the backend (older result, or a plain passthrough
    // column with no aggregate) — fall back to guessing from the column name.
    if (looksLikePercent(column)) {
      return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "%";
    }
    if (looksLikeMoney(value, column)) {
      return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return value.toLocaleString("en-US");
  }
  return escapeHtml(String(value));
}

function prettyLabel(name) {
  return escapeHtml(String(name).replace(/_/g, " "));
}

function renderDataTable(dt) {
  const card = document.createElement("div");
  card.className = "card";
  const kinds = dt.column_kinds || [];
  const caption = dt.caption || "From your onboard account";
  if (dt.result_type === "scalar" && dt.rows.length === 1 && dt.columns.length === 1) {
    card.innerHTML = `
      <p class="card-caption">${escapeHtml(caption)}</p>
      <div class="scalar-dimension"><span class="scalar-tick"></span><span class="scalar-line"></span><span class="scalar-tick"></span></div>
      <p class="scalar-value">${fmtCell(dt.rows[0][0], dt.columns[0], kinds[0])}</p>
      <p class="scalar-label">${prettyLabel(dt.columns[0])}</p>`;
  } else {
    const head = dt.columns.map((c) => `<th>${prettyLabel(c)}</th>`).join("");
    const body = dt.rows.map((row) =>
      `<tr>${row.map((v, i) => `<td>${fmtCell(v, dt.columns[i], kinds[i])}</td>`).join("")}</tr>`
    ).join("");
    card.innerHTML = `
      <p class="card-caption">${escapeHtml(caption)}</p>
      <table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }
  addRow("assistant", card);
}

function renderReservationCard(res) {
  const card = document.createElement("div");
  card.className = "card reservation-card";
  const status = (res.status || "").toLowerCase();
  const details = [res.reservation_date, res.reservation_time,
    res.party_size ? `party of ${res.party_size}` : null].filter(Boolean).join(" · ");
  card.innerHTML = `
    <p class="card-caption">Dining reservation</p>
    <p class="venue">${escapeHtml(res.venue_name || "")}</p>
    <p class="reservation-meta">${escapeHtml(details)}</p>
    <span class="status-pill ${status}">${escapeHtml(res.status || "")}</span>
    ${res.confirmation_number
      ? `<p class="confirmation-number">Confirmation <strong>${escapeHtml(res.confirmation_number)}</strong></p>`
      : ""}`;
  addRow("assistant", card);
}

function renderPendingAction(action) {
  const card = document.createElement("div");
  card.className = "card pending-card";
  const rows = Object.entries(action.arguments || {}).map(([k, v]) =>
    `<li><span class="k">${prettyLabel(k)}:</span> <strong>${escapeHtml(String(v))}</strong></li>`
  ).join("");
  const verb = { create_reservation: "New reservation",
                 modify_reservation: "Change reservation",
                 cancel_reservation: "Cancel reservation" }[action.tool] || "Action";
  card.innerHTML = `
    <div class="pending-inner">
      <div class="pending-stub" aria-hidden="true">
        <svg viewBox="0 0 48 48" width="34" height="34">
          <circle cx="24" cy="24" r="20" fill="none" stroke="currentColor" stroke-width="2.5"/>
          <path d="M16 24l6 6 11-13" fill="none" stroke="currentColor" stroke-width="3"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="pending-body">
        <p class="pending-title">${escapeHtml(verb)} — awaiting your confirmation</p>
        <ul class="pending-details">${rows}</ul>
        <div class="pending-buttons">
          <button type="button" class="confirm-button">Confirm</button>
          <button type="button" class="cancel-button">Not now</button>
        </div>
      </div>
    </div>`;
  addRow("assistant", card);

  card.querySelector(".confirm-button").addEventListener("click", () =>
    resolveAction(card, action.action_id, "confirm"));
  card.querySelector(".cancel-button").addEventListener("click", () =>
    resolveAction(card, action.action_id, "cancel"));
}

function renderCitations(citations) {
  const wrap = document.createElement("div");
  wrap.className = "citations";
  const seen = new Set();
  for (const c of citations) {
    if (seen.has(c.source)) continue;
    seen.add(c.source);
    const chip = document.createElement("span");
    chip.className = "citation-chip";
    chip.textContent = `📄 ${c.source}`;
    wrap.appendChild(chip);
  }
  if (wrap.children.length) addRow("assistant", wrap);
}

function renderTurn(turn) {
  if (turn.text) addBubble("assistant", renderMarkdown(turn.text));
  if (turn.citations && turn.citations.length) renderCitations(turn.citations);
  if (turn.data_table) renderDataTable(turn.data_table);
  if (turn.reservation_card) renderReservationCard(turn.reservation_card);
  if (turn.pending_action) renderPendingAction(turn.pending_action);
}

/* ── API calls ──────────────────────────────────────────────────────── */

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : "{}",
  });
  if (!response.ok) {
    let detail = "Something went wrong. Please try again.";
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

async function sendMessage(text) {
  if (busy || !text.trim()) return;
  markPendingResolved();
  addBubble("guest", renderMarkdown(text));
  suggestionsEl.hidden = true;
  setBusy(true);
  const typing = addTyping();
  try {
    const turn = await api("/api/chat", { message: text });
    typing.remove();
    renderTurn(turn);
  } catch (err) {
    typing.remove();
    addBubble("assistant", renderMarkdown(
      "I'm terribly sorry — something went wrong on my end. Please try that again, " +
      "or visit Guest Services on Deck 5 for immediate help."));
    showToast(err.message);
  } finally {
    setBusy(false);
    chatInput.focus();
  }
}

async function resolveAction(card, actionId, verb) {
  if (busy) return;
  card.classList.add("resolved");
  setBusy(true);
  const typing = addTyping();
  try {
    const turn = await api(`/api/actions/${actionId}/${verb}`);
    typing.remove();
    renderTurn(turn);
  } catch (err) {
    typing.remove();
    card.classList.remove("resolved");
    showToast(err.message);
  } finally {
    setBusy(false);
  }
}

function markPendingResolved() {
  document.querySelectorAll(".pending-card:not(.resolved)")
    .forEach((el) => el.classList.add("resolved"));
}

/* ── Events ─────────────────────────────────────────────────────────── */

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const guestId = guestIdInput.value.trim();
  if (!guestId) return;
  loginButton.disabled = true;
  loginError.hidden = true;
  try {
    const data = await api("/api/login", { guest_id: guestId });
    currentGuestId = data.guest_id;
    guestChip.textContent = data.loyalty_tier
      ? `${data.guest_name} · ${data.loyalty_tier}`
      : data.guest_name;
    loginView.classList.add("hidden");
    chatView.classList.remove("hidden");
    messagesEl.innerHTML = "";
    suggestionsEl.hidden = false;
    renderTurn(data.turn);
    chatInput.focus();
  } catch (err) {
    loginError.textContent = err.message;
    loginError.hidden = false;
  } finally {
    loginButton.disabled = false;
  }
});

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = chatInput.value;
  chatInput.value = "";
  sendMessage(text);
});

suggestionsEl.addEventListener("click", (event) => {
  if (event.target.classList.contains("chip")) {
    sendMessage(event.target.textContent);
  }
});

newConversationBtn.addEventListener("click", async () => {
  if (!currentGuestId || busy) return;
  setBusy(true);
  try {
    await api("/api/logout");
    const data = await api("/api/login", { guest_id: currentGuestId });
    messagesEl.innerHTML = "";
    suggestionsEl.hidden = false;
    renderTurn(data.turn);
  } catch (err) {
    showToast(err.message);
  } finally {
    setBusy(false);
    chatInput.focus();
  }
});

logoutBtn.addEventListener("click", async () => {
  if (busy) return;
  try {
    await api("/api/logout");
  } catch (_) {
    /* the session may already be expired — reloading to the login view is still correct */
  }
  location.reload();
});
