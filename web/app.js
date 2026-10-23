// 계기판 신호 해설 — 웹 화면. 서버 API: src/api/main.py
"use strict";

// ---------- 작은 도우미 ----------
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const clip = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);
// 브라우저 저장소는 편의 기능(내 차·대화 기억)에만 쓰고, 막혀 있어도 화면은 그대로 동작한다.
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 저장 못 해도 무시 */ } },
};

const UNKNOWN = "모름";
const S = {
  meta: null,
  signals: {},                                   // id → 신호
  car: store.get("car", { brand: "현대", model: "", powertrain: UNKNOWN, year: null }),
  carCtx: { manual: "", note: "", recalls: [] },
  page: "find",
  cardSignal: "",
  askSignal: "",
  chats: store.get("chats", {}),                 // 신호 ID("" = 자유 질문) → 대화 기록
  cards: {},                                     // 카드 결과 캐시
  find: { colors: [], kinds: [] },
  busy: false,
};

const COLOR_HEX = { 빨강: "#d92d20", 주황: "#f08c00", 노랑: "#f5b301", 초록: "#22a447", 파랑: "#2563eb", 흰색: "#ffffff", 회색: "#9ca3af" };
const SRC = { "신호 메타데이터": ["src-meta", "🏷️"], "제조사 매뉴얼": ["src-manual", "📘"], "백과사전": ["src-wiki", "📗"], "공공 리콜": ["src-recall", "🔔"] };
const URG = {
  "즉시 정차": ["u-stop", "🛑", "안전한 곳에 정차한 뒤 점검이 필요한 단계예요"],
  "즉시 조치": ["u-act", "🚨", "고장이 아니라 지금 운전 조작이 필요한 경고예요"],
  "빠른 점검": ["u-check", "🔧", "가능한 한 빨리 점검을 받아야 하는 단계예요"],
  "주의": ["u-caution", "⚠️", "운전자가 직접 확인·조치할 수 있는 단계예요"],
  "정보": ["u-info", "ℹ️", "상태나 작동을 알려 주는 표시예요"],
  "확인되지 않음": ["u-unknown", "❔", "근거 문서로 판단할 수 없어요"],
};
const NEXT_STEP = {
  start: "질문을 분석하는 중…",
  extract: "취급설명서에서 근거를 찾는 중…",
  retrieve: "근거를 바탕으로 작성하는 중…",
};

const dots = (color) => {
  const names = Object.keys(COLOR_HEX).filter((c) => (color || "").includes(c));
  return names.length
    ? names.map((c) => `<span class="dot" style="background:${COLOR_HEX[c]};box-shadow:0 0 9px ${COLOR_HEX[c]}" title="${c}"></span>`).join("")
    : `<span title="표시창 문구">💬</span>`;
};
const srcBadge = (t) => { const [c, i] = SRC[t] || ["src-manual", "📄"]; return `<span class="src ${c}">${i} ${esc(t)}</span>`; };
const genCode = (car) => (car.match(/\(\s*([A-Z0-9]+)/) || [])[1] || "";

// ---------- 서버 호출 ----------
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

async function errorText(r) {
  const fallback = "요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요.";
  try { const d = (await r.json()).detail; return typeof d === "string" && d ? d : fallback; } catch { return fallback; }
}

// 한 줄에 JSON 하나씩 오는 스트림: step(진행) … result | error
async function apiStream(path, body, onStep) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(await errorText(r));
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "", result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      const ev = JSON.parse(line);
      if (ev.type === "step") onStep(ev);
      else if (ev.type === "error") throw new Error(ev.message);
      else if (ev.type === "result") result = ev;
    }
  }
  if (!result) throw new Error("응답을 받지 못했습니다. 잠시 후 다시 시도하세요.");
  return result;
}

// 진행 상태: 끝난 단계는 ✓, 진행 중인 단계는 돌아가는 표시
function progress() {
  const el = h(`<ul class="progress"><li class="pending">${NEXT_STEP.start}</li></ul>`);
  return {
    el,
    step(ev) {
      const pending = $(".pending", el);
      pending.before(h(`<li>${esc(ev.label)}</li>`));
      if (NEXT_STEP[ev.node]) pending.textContent = NEXT_STEP[ev.node];
      else pending.remove();
    },
  };
}

// ---------- 근거·리콜 표시 ----------
function richText(text, uid) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (_, g) =>
      g.split(",").map((n) => `<a class="cite" data-uid="${uid}" data-n="${n.trim()}" title="근거 ${n.trim()} 보기">${n.trim()}</a>`).join(""))
    .replace(/\n/g, "<br>");
}

// 실제로 인용한 근거만 보여 주고, 하나도 없으면 그 사실을 알린다
function evidenceBlock(evidence, cited, note, uid) {
  const shown = cited ? evidence.filter((e) => cited.includes(e.n)) : evidence;
  if (!shown.length) return h(`<p class="muted small">ℹ️ 이 답변에 인용된 근거 문서가 없습니다. 근거 문서에서 확인되지 않은 내용일 수 있어요.</p>`);
  return h(`<details class="evidence" data-uid="${uid}">
    <summary>근거 ${shown.length}건 보기</summary>
    <div class="ev-list">
      ${note ? `<p class="note small">ℹ️ ${esc(note)}</p>` : ""}
      ${shown.map((e) => `<div class="ev" id="ev-${uid}-${e.n}">
        <div class="ev-head"><span class="ev-n">[${e.n}]</span><span class="ev-title">${esc(e.title)}</span></div>
        <div class="ev-meta">${srcBadge(e.source_type)}<span>${esc(e.manual)}</span>
          ${e.source ? `<a href="${esc(e.source)}" target="_blank" rel="noopener">원문 보기 ↗</a>` : ""}</div>
        <div class="ev-text">${esc(clip(e.text, 320))}</div></div>`).join("")}
    </div></details>`);
}

// 본문의 [n]을 누르면 그 근거를 펼쳐 보여 준다
document.addEventListener("click", (e) => {
  const c = e.target.closest(".cite");
  if (!c) return;
  const box = document.querySelector(`details.evidence[data-uid="${c.dataset.uid}"]`);
  const ev = document.getElementById(`ev-${c.dataset.uid}-${c.dataset.n}`);
  if (!box || !ev) return;
  box.open = true;
  ev.scrollIntoView({ behavior: "smooth", block: "center" });
  ev.classList.remove("flash"); void ev.offsetWidth; ev.classList.add("flash");
});

function recallItem(r) {
  const gen = genCode(r.car);
  const el = h(`<div class="recall">
    <div class="recall-head"><span class="recall-car">${esc(r.car)} ${gen ? `<span class="gen-chip">${esc(gen)} 세대</span>` : ""}</span>
      <span class="recall-date">리콜 개시 ${esc(r.date)}</span></div>
    <div class="recall-meta">생산기간 ${esc(r.period)} · ${Number(r.count || 0).toLocaleString()}대 · ${srcBadge(r.source_type)}
      · <a href="${esc(r.source)}" target="_blank" rel="noopener">원문 ↗</a></div>
    <div class="recall-reason clamp">${esc(r.reason)}</div>
    ${r.reason.length > 120 ? `<button class="link-btn" type="button">자세히 보기</button>` : ""}</div>`);
  const more = $(".link-btn", el);
  if (more) more.onclick = () => { const t = $(".recall-reason", el); t.classList.toggle("clamp"); more.textContent = t.classList.contains("clamp") ? "자세히 보기" : "접기"; };
  return el;
}

function recallNotes(recalls) {
  const gens = [...new Set(recalls.map((r) => genCode(r.car)).filter(Boolean))];
  const box = document.createElement("div");
  if (gens.length > 1) box.append(h(`<p class="note small">ℹ️ 여러 세대(${gens.map(esc).join(", ")}) 기록이 섞여 있어요. 차명 괄호 안 코드로 내 차 세대를 확인하세요.</p>`));
  box.append(h(`<p class="muted small">내 차가 실제 리콜 대상인지는 <a href="https://www.car.go.kr" target="_blank" rel="noopener">자동차리콜센터</a>에서 차대번호로 확인하세요.</p>`));
  return box;
}

// ---------- 신호 고르기 (이름·모양으로 검색되는 목록) ----------
function makePicker(host, { placeholder, allowFree = false, onPick }) {
  const el = h(`<div class="picker">
    <input type="text" placeholder="${esc(placeholder)}" aria-label="신호 선택" autocomplete="off">
    <button class="picker-clear" type="button" aria-label="선택 지우기" hidden>✕</button>
    <div class="picker-list" role="listbox" hidden></div></div>`);
  host.replaceChildren(el);
  const input = $("input", el), list = $(".picker-list", el), clear = $(".picker-clear", el);
  let items = [], active = 0, value = "";

  function render(q) {
    const key = q.replace(/\s/g, "").toLowerCase();
    items = Object.values(S.signals)
      .filter((s) => !key || (s.name + s.shape + s.kind + s.color).replace(/\s/g, "").toLowerCase().includes(key))
      .slice(0, 60);
    if (allowFree && !key) items = [{ id: "", name: "자유 질문 (신호 선택 안 함)", kind: "", color: "" }, ...items];
    list.innerHTML = items.length
      ? items.map((s, i) => `<div class="picker-item ${i === active ? "active" : ""}" data-i="${i}" role="option">
          <span>${s.id ? dots(s.color) : "💬"}</span><span>${esc(s.name)}</span><span class="kind">${esc(s.kind)}</span></div>`).join("")
      : `<div class="picker-item muted">맞는 신호가 없어요</div>`;
    list.hidden = false;
  }
  function set(id, silent = false) {
    value = id;
    const s = S.signals[id];
    input.value = s ? s.name : "";
    clear.hidden = !s;
    list.hidden = true;
    if (!silent) onPick(id);
  }
  input.addEventListener("focus", () => { active = 0; render(""); input.select(); });
  input.addEventListener("input", () => { active = 0; render(input.value); });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(input.value); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(input.value); e.preventDefault(); }
    else if (e.key === "Enter") { if (items[active]) { set(items[active].id); input.blur(); } e.preventDefault(); }
    else if (e.key === "Escape") { set(value, true); input.blur(); }
  });
  list.addEventListener("mousedown", (e) => {
    const it = e.target.closest("[data-i]");
    if (!it) return;
    e.preventDefault();
    set(items[+it.dataset.i].id);
    input.blur();
  });
  input.addEventListener("blur", () => setTimeout(() => set(value, true), 150));
  clear.onclick = () => set("");
  return { set };
}

// ---------- 화면 전환 ----------
const FEATURES = ["home", "find", "card", "ask", "recall"];
const carReady = () => Boolean(S.car.model);

function setPage(page) {
  // 내 차를 설정하기 전에는 첫 화면(내 차 설정)만 보여 준다
  if (page !== "onboard" && !carReady()) page = "onboard";
  S.page = page;
  $("#nav").hidden = page === "onboard";
  $("#car-chip").hidden = page === "onboard";
  document.querySelectorAll(".nav-btn").forEach((t) => t.classList.toggle("active", t.dataset.page === page));
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
  if (page !== "onboard" && location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  if (page === "home") renderHome();
  if (page === "recall") renderRecallPage();
  if (page === "ask") renderAsk();
  window.scrollTo({ top: 0 });
}

// ---------- 내 차 ----------
function carLabel() {
  const { model, powertrain, year } = S.car;
  const parts = [model, powertrain !== UNKNOWN ? powertrain : "", year || ""].filter(Boolean);
  return parts.join(" ");
}

async function refreshCar() {
  $("#car-chip-text").textContent = carLabel();
  try {
    S.carCtx = await api("/api/car", S.car);
  } catch (e) {
    S.carCtx = { manual: "", note: "", recalls: [], error: e.message };
  }
  const n = S.carCtx.recalls.length;
  $("#recall-badge").hidden = !n;
  $("#recall-badge").textContent = n;
  S.cards = {};  // 내 차가 바뀌면 카드 근거·리콜이 달라진다
  if (S.page === "home") renderHome();
  if (S.page === "card" && S.cardSignal) loadCard();
  if (S.page === "recall") renderRecallPage();
}

// 첫 화면: 제조사 → 차종 → 동력 방식 → 연식. 차종을 골라야 [시동 걸기]가 켜진다.
// editing = 이미 설정한 차를 바꾸는 중 (취소하면 원래 화면으로)
function openOnboard(editing = false) {
  const V = S.meta.vehicles, back = S.page;
  let draft = { ...S.car };
  if (!V[draft.brand]) draft.brand = Object.keys(V)[0];
  const brandBox = $("#ob-brand"), modelBox = $("#ob-model"), ptBox = $("#ob-powertrain"), yearIn = $("#ob-year");

  const chips = (values, current, attr) => values.map((v) =>
    `<button type="button" class="chip" aria-pressed="${v === current}" data-${attr}="${esc(v)}">${esc(v)}</button>`).join("");
  const draw = () => {
    brandBox.innerHTML = chips(Object.keys(V), draft.brand, "brand");
    modelBox.innerHTML = chips(Object.keys(V[draft.brand] || {}), draft.model, "model");
    const opts = draft.model && V[draft.brand]?.[draft.model] ? [UNKNOWN, ...V[draft.brand][draft.model]] : S.meta.powertrains;
    if (!opts.includes(draft.powertrain)) draft.powertrain = UNKNOWN;
    ptBox.innerHTML = chips(opts, draft.powertrain, "pt");
    $("#ob-start").disabled = !draft.model;
    $("#ob-hint").textContent = draft.model ? "동력 방식을 모르면 '모름'으로 두어도 돼요." : "차종을 고르면 시작할 수 있어요.";
  };
  brandBox.onclick = (e) => { const b = e.target.closest("[data-brand]"); if (b) { draft.brand = b.dataset.brand; draft.model = ""; draw(); } };
  modelBox.onclick = (e) => { const b = e.target.closest("[data-model]"); if (b) { draft.model = b.dataset.model; draw(); } };
  ptBox.onclick = (e) => { const b = e.target.closest("[data-pt]"); if (b) { draft.powertrain = b.dataset.pt; draw(); } };
  yearIn.value = draft.year || "";
  // 연식: − / + 단추와 위아래 화살표 키로 한 해씩. 비어 있으면 올해부터 시작한다.
  const maxYear = new Date().getFullYear() + 1;
  const stepYear = (d) => {
    const y = parseInt(yearIn.value, 10);
    yearIn.value = Math.min(maxYear, Math.max(MIN_YEAR, Number.isFinite(y) ? y + d : maxYear - 1));
  };
  document.querySelectorAll(".ys-btn").forEach((b) => { b.onclick = () => stepYear(Number(b.dataset.step)); });
  yearIn.oninput = () => { yearIn.value = yearIn.value.replace(/\D/g, "").slice(0, 4); };
  yearIn.onkeydown = (e) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); stepYear(e.key === "ArrowUp" ? 1 : -1); }
  };
  $("#ob-cancel").hidden = !editing;
  $("#ob-cancel").onclick = () => setPage(back === "onboard" ? "home" : back);
  $("#ob-start").onclick = () => {
    const y = parseInt(yearIn.value, 10);
    S.car = { ...draft, year: y >= MIN_YEAR && y <= maxYear ? y : null };
    store.set("car", S.car);
    refreshCar();
    setPage(editing && back !== "onboard" ? back : "home");
  };
  draw();
  setPage("onboard");
}

// ---------- 홈: 기능 선택 ----------
function renderHome() {
  $("#home-car").innerHTML = `MY CAR <span class="ko">· ${esc(carLabel())}</span>`;
  const n = S.carCtx.recalls.length;
  $("#ro-recalls").textContent = S.carCtx.error ? "-" : n;
  $("#tile-recall-desc").textContent = n ? `내 차종 국내 리콜 기록 ${n}건` : "내 차종의 국내 리콜 기록";
}

// ---------- 경고등 찾기 ----------
function buildFindFilters() {
  const mk = (box, values, key) => {
    box.innerHTML = values.map((v) => `<button type="button" class="chip" aria-pressed="false" data-v="${esc(v)}">
      ${key === "colors" ? `<span class="dot" style="background:${COLOR_HEX[v] || "#ccc"}"></span>` : ""}${esc(v)}</button>`).join("");
    box.onclick = (e) => {
      const b = e.target.closest("[data-v]");
      if (!b) return;
      const on = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", on);
      S.find[key] = on ? [...S.find[key], b.dataset.v] : S.find[key].filter((x) => x !== b.dataset.v);
      runFind();
    };
  };
  mk($("#color-chips"), S.meta.colors, "colors");
  mk($("#kind-chips"), S.meta.kinds, "kinds");
  $("#find-form").onsubmit = (e) => { e.preventDefault(); runFind(); };
}

function signalRow(s, top) {
  const el = h(`<div class="panel signal-row ${top ? "rank-1" : ""}">
    <div class="signal-info"><div class="signal-icon">${dots(s.color)}</div>
      <div><div class="signal-name">${esc(s.name)}</div>
        <div class="signal-sub">${esc(s.kind)} · ${esc(s.color)} · 모양: ${esc(s.shape)} · 적용: ${esc(s.powertrain)}</div></div></div>
    <div class="signal-actions">
      <button class="btn small" type="button" data-act="card">🩺 진단 카드</button>
      <button class="btn small primary" type="button" data-act="ask">💬 질문하기</button></div></div>`);
  $("[data-act=card]", el).onclick = () => openCard(s.id);
  $("[data-act=ask]", el).onclick = () => openAsk(s.id);
  return el;
}

const MIN_YEAR = 1990;
const FIND_EXAMPLES =["주황색 타이어 안에 느낌표", "거북이 모양", "빨간 주전자 모양", "변속기 과열"];
let findSeq = 0;
async function runFind() {
  const out = $("#find-results");
  const description = $("#find-input").value.trim();
  if (!description && !S.find.colors.length && !S.find.kinds.length) { out.replaceChildren(); return; }
  const seq = ++findSeq;
  out.replaceChildren(h(`<p class="muted">찾는 중…</p>`));
  try {
    const r = await api("/api/find", { description, colors: S.find.colors, kinds: S.find.kinds });
    if (seq !== findSeq) return;  // 더 새 검색이 있으면 버린다
    if (r.off_topic) {
      // 계기판과 상관없는 설명: 후보 대신 설명 방법을 안내하고, 예시를 누르면 바로 다시 찾는다.
      const tip = h(`<div class="empty off-topic"><p><b>‘${esc(description)}’</b>은(는) 계기판 경고등·표시와 관련된 설명이 아닌 것 같아요.</p>
        <p>보이는 <b>색·모양</b>이나 <b>화면 문구</b>로 설명해 주세요.</p><div class="chips"></div></div>`);
      FIND_EXAMPLES.forEach((ex) => {
        const b = h(`<button type="button" class="chip">${esc(ex)}</button>`);
        b.onclick = () => { $("#find-input").value = ex; runFind(); };
        $(".chips", tip).append(b);
      });
      out.replaceChildren(tip);
    } else if (!r.main.length) { out.replaceChildren(h(`<div class="empty">조건에 맞는 신호를 찾지 못했어요. 설명을 바꾸거나 필터를 줄여 보세요.</div>`)); return; }
    else {
      const list = h(`<div class="result-list"></div>`);
      r.main.forEach((s, i) => list.append(signalRow(s, i === 0 && description)));
      out.replaceChildren(list);
    }
    if (r.others.length) {
      const label = r.off_topic ? `그래도 후보 ${r.others.length}개 보기` : `찾는 신호가 없나요? 다른 후보 ${r.others.length}개 보기`;
      const more = h(`<details class="more"><summary>${label}</summary><div class="result-list"></div></details>`);
      r.others.forEach((s) => $(".result-list", more).append(signalRow(s, false)));
      out.append(more);
    }
  } catch (e) {
    out.replaceChildren(h(`<p class="note error">${esc(e.message)}</p>`));
  }
}

// ---------- 진단 카드 ----------
let cardPicker;
function openCard(id) {
  S.cardSignal = id;
  cardPicker.set(id, true);
  setPage("card");
  loadCard();
}

async function loadCard() {
  const body = $("#card-body"), id = S.cardSignal;
  if (!id) { body.replaceChildren(h(`<div class="empty">위에서 신호를 고르거나, 경고등 찾기에서 후보를 눌러 보세요.</div>`)); return; }
  const key = JSON.stringify([id, S.car]);
  if (!S.cards[key]) {
    const p = progress();
    body.replaceChildren(h(`<div class="panel"></div>`));
    $(".panel", body).append(h(`<p><b>${esc(S.signals[id].name)}</b> 진단 카드를 만드는 중…</p>`), p.el);
    try {
      S.cards[key] = await apiStream("/api/card", { signal_id: id, car: S.car }, p.step);
    } catch (e) {
      body.replaceChildren(h(`<p class="note error">${esc(e.message)}</p>`));
      return;
    }
    if (S.cardSignal !== id) return;
  }
  renderCard(S.cards[key]);
}

function renderCard(r) {
  const c = r.card, [cls, icon, desc] = URG[c.urgency] || URG["확인되지 않음"], uid = `card-${r.signal.id}`;
  const list = (items, cls2) => `<ul class="${cls2}">${items.map((x) => `<li><span>${richText(x, uid)}</span></li>`).join("")}</ul>`;
  const diag = h(`<div class="panel diag">
    <div class="urgency ${cls}"><span class="urgency-icon">${icon}</span>
      <div><div class="urgency-level">${esc(c.urgency)}</div><div class="urgency-desc">${desc}</div></div></div>
    <div class="diag-body">
      <div class="diag-title"><span class="signal-icon">${dots(r.signal.color)}</span>
        <div><h3>${esc(r.signal.name)}</h3><div class="signal-sub">${esc(r.signal.kind)} · 적용: ${esc(r.signal.powertrain)}</div></div></div>
      <div class="diag-section"><h4>의미</h4><p>${richText(c.meaning, uid)}</p></div>
      ${c.do_now.length ? `<div class="diag-section"><h4>지금 할 일</h4>${list(c.do_now, "do-list")}</div>` : ""}
      ${c.dont.length ? `<div class="diag-section"><h4>하지 말 것</h4>${list(c.dont, "dont-list")}</div>` : ""}
      ${!c.do_now.length && !c.dont.length ? `<p class="muted small">근거 문서에서 확인된 행동 안내가 없습니다.</p>` : ""}
      <div class="ev-slot"></div>
      <div class="diag-actions"><button class="btn primary" type="button" data-act="ask">💬 이 신호에 대해 더 물어보기</button></div>
    </div></div>`);
  $(".ev-slot", diag).append(evidenceBlock(r.evidence, c.evidence, r.note, uid));
  $("[data-act=ask]", diag).onclick = () => openAsk(r.signal.id);

  const side = h(`<aside class="panel side-panel"><h3>🔔 관련 내 차 리콜</h3><div class="side-body"></div></aside>`);
  const sb = $(".side-body", side);
  if (!S.car.model) {
    sb.append(h(`<div class="empty small">내 차를 설정하면 이 장치와 관련된 리콜 기록을 함께 보여 드려요.<br><button class="btn small" type="button">내 차 설정</button></div>`));
    $("button", sb).onclick = () => openOnboard(true);
  } else if (!r.recalls.length) {
    sb.append(h(`<p class="muted small">${esc(carLabel())}에서 이 신호의 장치와 관련된 리콜 기록은 찾지 못했어요.</p>`));
    if (S.carCtx.recalls.length) {
      const b = h(`<button class="link-btn" type="button">이 차종 리콜 전체 ${S.carCtx.recalls.length}건 보기 →</button>`);
      b.onclick = () => setPage("recall");
      sb.append(b);
    }
  } else {
    sb.append(h(`<p class="muted small">리콜 사유에 이 신호 장치 이름이 들어 있는 기록이에요.</p>`));
    const l = h(`<div class="recall-list"></div>`);
    r.recalls.forEach((x) => l.append(recallItem(x)));
    sb.append(l, recallNotes(r.recalls));
  }

  const wrap = document.createElement("div");
  if (r.warning) wrap.append(h(`<p class="note warn">${esc(r.warning)}</p>`));
  const grid = h(`<div class="card-layout"></div>`);
  grid.append(diag, side);
  wrap.append(grid);
  $("#card-body").replaceChildren(wrap);
}

// ---------- 질문하기 ----------
let askPicker;
const EXAMPLES_FREE = ["엔진 경고등이 켜진 것과 깜빡이는 것은 다른가요?", "전기차 거북이 표시가 뜨면 무엇이 제한되나요?", "요소수가 뭐고 경고등이 뜨면 어떻게 해야 해?"];
const EXAMPLES_SIGNAL = ["이거 켜지면 계속 달려도 돼?", "왜 켜지는 거야?", "어떻게 하면 꺼져?"];

function openAsk(id) {
  S.askSignal = id;
  askPicker.set(id, true);
  setPage("ask");
  $("#ask-input").focus();
}

function chatOf(id) { return (S.chats[id] = S.chats[id] || []); }
function saveChats() { store.set("chats", S.chats); }

function renderChatList() {
  const box = $("#chat-list");
  const ids = Object.keys(S.chats).filter((id) => S.chats[id].length && (id === "" || S.signals[id]));
  if (!ids.length) { box.replaceChildren(); return; }
  box.innerHTML = `<span class="muted small" style="align-self:center">대화 기록</span>` + ids.map((id) => {
    const name = id ? S.signals[id].name : "자유 질문";
    return `<button type="button" class="chip" aria-pressed="${id === S.askSignal}" data-id="${esc(id)}">${esc(clip(name, 18))} · ${Math.ceil(S.chats[id].length / 2)}</button>`;
  }).join("");
  box.onclick = (e) => { const b = e.target.closest("[data-id]"); if (b) { S.askSignal = b.dataset.id; askPicker.set(S.askSignal, true); renderAsk(); } };
}

function botBubble(m, uid) {
  const el = h(`<div class="msg bot"><div class="bubble"><div class="answer">${richText(m.content, uid)}</div></div></div>`);
  const b = $(".bubble", el);
  // 인사·범위 밖 질문은 근거 검색 없이 답한 것이라 '인용된 근거 없음' 안내를 붙이지 않는다
  if (!m.chat) b.append(evidenceBlock(m.evidence || [], m.cited || [], m.note, uid));
  // 자유 질문에서 설명이 여러 신호에 맞을 수 있으면 후보를 함께 보여 준다 (답변이 한 신호로 단정해도 확인할 수 있게)
  if (m.free && m.signals && m.signals.length > 1) {
    const alt = h(`<div class="alt-signals"><span class="muted small">설명에 맞을 수 있는 신호 — 내 계기판과 같은지 확인해 보세요</span>
      <div class="chips">${m.signals.map((s) => `<button type="button" class="chip" data-id="${esc(s.id)}">${dots(s.color)} ${esc(s.name)}</button>`).join("")}</div></div>`);
    alt.onclick = (e) => { const c = e.target.closest("[data-id]"); if (c) openCard(c.dataset.id); };
    b.append(alt);
  }
  if (m.recalls && m.recalls.length) {
    const d = h(`<details class="evidence"><summary>🔔 관련 내 차 리콜 ${m.recalls.length}건</summary><div class="ev-list"><div class="recall-list"></div></div></details>`);
    m.recalls.forEach((r) => $(".recall-list", d).append(recallItem(r)));
    $(".ev-list", d).append(recallNotes(m.recalls));
    b.append(d);
  }
  return el;
}

function renderChatTools(id, hasMessages) {
  const tools = $("#chat-tools");
  tools.replaceChildren();
  if (id) { const b = h(`<button class="btn small" type="button">🩺 진단 카드 보기</button>`); b.onclick = () => openCard(id); tools.append(b); }
  if (hasMessages) {
    const clear = h(`<button class="btn small ghost" type="button">이 대화 지우기</button>`);
    clear.onclick = () => { if (S.busy) return; S.chats[id] = []; saveChats(); renderAsk(); };
    tools.append(clear);
  }
}

function renderAsk() {
  renderChatList();
  const id = S.askSignal, thread = $("#chat-thread"), msgs = chatOf(id);
  renderChatTools(id, msgs.length > 0);
  $("#ask-input").placeholder = id ? `'${S.signals[id].name}'에 대해 물어보세요` : "계기판 신호에 대해 물어보세요";
  thread.replaceChildren();
  if (!msgs.length) {
    const ex = id ? EXAMPLES_SIGNAL : EXAMPLES_FREE;
    const empty = h(`<div class="empty">
      <p>${id ? `<b>${esc(S.signals[id].name)}</b>에 대해 궁금한 점을 물어보세요.` : "신호를 고르면 그 신호 문서를 먼저 근거로 써요. 고르지 않고 자유롭게 물어도 돼요."}</p>
      <div class="examples">${ex.map((q) => `<button type="button" class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>`);
    empty.onclick = (e) => {
      const q = e.target.closest("[data-q]");
      if (q) { $("#ask-input").value = q.dataset.q; sendQuestion(); }
    };
    thread.append(empty);
    return;
  }
  msgs.forEach((m, i) => thread.append(m.role === "user"
    ? h(`<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>`)
    : botBubble(m, `${id || "free"}-${i}`)));
}

async function sendQuestion() {
  const input = $("#ask-input"), question = input.value.trim();
  if (!question || S.busy) return;
  const id = S.askSignal, msgs = chatOf(id);
  const history = msgs.map((m) => [m.role, m.content]);
  msgs.push({ role: "user", content: question });
  input.value = ""; autoGrow();
  renderAsk();
  const p = progress();
  const pending = h(`<div class="msg bot"><div class="bubble"></div></div>`);
  $(".bubble", pending).append(p.el);
  $("#chat-thread").append(pending);
  pending.scrollIntoView({ behavior: "smooth", block: "end" });
  S.busy = true; $("#ask-send").disabled = true;
  try {
    const r = await apiStream("/api/ask", { question, history, signal_id: id, car: S.car }, p.step);
    msgs.push({ role: "assistant", content: r.answer, evidence: r.evidence, cited: r.cited, recalls: r.recalls, note: r.note,
                signals: r.signals, free: !id, chat: r.chat });
    saveChats();
    if (S.page === "ask" && S.askSignal === id) renderAsk();
  } catch (e) {
    msgs.pop();  // 실패한 질문은 기록에서 빼서 다시 물을 수 있게
    pending.replaceWith(h(`<p class="note error">${esc(e.message)}</p>`));
    input.value = question;
  } finally {
    S.busy = false; $("#ask-send").disabled = false;
  }
  const last = $("#chat-thread").lastElementChild;
  if (last) last.scrollIntoView({ behavior: "smooth", block: "end" });
}

function autoGrow() { const t = $("#ask-input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; }

// ---------- 내 차 리콜 ----------
function renderRecallPage() {
  const body = $("#recall-body");
  if (!S.car.model) {
    body.replaceChildren(h(`<div class="panel empty">내 차종을 설정하면 국내 리콜 기록을 보여 드려요.<br><button class="btn primary" type="button">내 차 설정</button></div>`));
    $("button", body).onclick = () => openOnboard(true);
    return;
  }
  if (S.carCtx.error) { body.replaceChildren(h(`<p class="note error">${esc(S.carCtx.error)}</p>`)); return; }
  const all = S.carCtx.recalls;
  const head = h(`<div class="panel" style="margin-bottom:12px">
    <p><b>${esc(carLabel())}</b> · 국내 리콜 기록 <b>${all.length}</b>건</p>
    ${S.carCtx.note ? `<p class="muted small">${esc(S.carCtx.note)}</p>` : ""}</div>`);
  head.append(recallNotes(all));
  if (!all.length) { body.replaceChildren(head, h(`<div class="empty">이 조건에 맞는 국내 리콜 기록을 찾지 못했어요. 연식이나 동력 방식을 바꿔 보세요.</div>`)); return; }
  const tools = h(`<div class="recall-tools"><input type="search" placeholder="리콜 사유 검색 (예: 브레이크, 배터리, 에어백)" aria-label="리콜 사유 검색"><span class="muted small"></span></div>`);
  const list = h(`<div class="recall-list"></div>`);
  const draw = () => {
    const q = $("input", tools).value.replace(/\s/g, "").toLowerCase();
    const shown = all.filter((r) => !q || (r.reason + r.car).replace(/\s/g, "").toLowerCase().includes(q));
    $("span", tools).textContent = q ? `${shown.length}건` : "";
    list.replaceChildren(...shown.map(recallItem));
    if (!shown.length) list.append(h(`<p class="muted">검색어와 맞는 리콜이 없어요.</p>`));
  };
  $("input", tools).oninput = draw;
  draw();
  body.replaceChildren(head, tools, list);
}

// ---------- 시작 ----------
function showBlocker(st) {
  // 키 누락·색인 없음이면 기능 화면 대신 안내만 보여 준다
  const b = $("#blocker");
  if (!st.key_ok) {
    b.innerHTML = `<p class="note error">API 키(OPENAI_API_KEY)가 설정되지 않았습니다. <code>.env.example</code>을 <code>.env</code>로 복사해 키를 넣은 뒤 서버를 다시 실행하세요.</p>`;
  } else if (!st.meta_count || !st.doc_count) {
    b.innerHTML = `<p class="note error">검색 색인이 없습니다. 아래 명령으로 데이터를 준비·색인한 뒤 서버를 다시 실행하세요.</p>
      <pre>python -m src.data_prep.manuals\npython -m src.data_prep.wiki\npython -m src.vectorstore.signal_meta\npython -m src.rag.collect</pre>`;
  } else return false;
  b.hidden = false;
  document.querySelectorAll(".page, #nav, #car-chip").forEach((p) => { p.hidden = true; });
  return true;
}

async function init() {
  try {
    S.meta = await api("/api/meta");
  } catch (e) {
    $("#blocker").hidden = false;
    $("#blocker").innerHTML = `<p class="note error">서버에 연결하지 못했습니다. 서버를 실행했는지 확인하세요. (${esc(e.message)})</p>`;
    return;
  }
  const st = S.meta.status;
  $("#disclaimer-text").textContent = S.meta.disclaimer;
  $("#index-status").textContent = `🏷️ 신호 메타데이터 색인 ${st.meta_count}건 · 📚 문서 청크 ${st.doc_count}개`;
  if (showBlocker(st)) return;
  S.meta.signals.forEach((s) => { S.signals[s.id] = s; });
  $("#ro-signals").textContent = S.meta.signals.length;
  $("#ro-docs").textContent = st.doc_count;

  document.querySelectorAll(".nav-btn").forEach((t) => { t.onclick = () => setPage(t.dataset.page); });
  document.querySelectorAll(".tile[data-go]").forEach((t) => { t.onclick = () => setPage(t.dataset.go); });
  $("#car-chip").onclick = () => openOnboard(true);
  window.addEventListener("hashchange", () => {
    const p = location.hash.slice(1);
    if (FEATURES.includes(p) && p !== S.page) setPage(p);
  });
  buildFindFilters();
  cardPicker = makePicker($("#card-picker"), { placeholder: "신호 이름이나 모양으로 검색 (예: 타이어, 거북이, 브레이크)", onPick: (id) => { S.cardSignal = id; loadCard(); } });
  askPicker = makePicker($("#ask-picker"), { placeholder: "질문할 신호 고르기 (안 고르면 자유 질문)", allowFree: true, onPick: (id) => { S.askSignal = id; renderAsk(); } });
  $("#ask-form").onsubmit = (e) => { e.preventDefault(); sendQuestion(); };
  $("#ask-input").addEventListener("input", autoGrow);
  $("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendQuestion(); } });
  loadCard();

  if (!carReady()) { openOnboard(false); return; }
  const page = location.hash.slice(1);
  setPage(FEATURES.includes(page) ? page : "home");
  refreshCar();
}

init();
