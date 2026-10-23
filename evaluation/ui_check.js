// 웹 화면 점검: 실제 Chrome으로 조작하고 결과와 화면 캡처를 남긴다.
// 준비: 서버 실행(uvicorn src.api.main:app --port 8000) 후, 루트에서
//   npm install --no-save playwright-core@1.49.1
//   node evaluation/ui_check.js        (CHROME_PATH 로 브라우저 경로 변경 가능, LLM 약 4회)
// 결과: docs/화면_점검결과.md, 캡처: docs/screenshots/
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const BASE = process.env.APP_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const ROOT = path.join(__dirname, "..");
const SHOTS = path.join(ROOT, "docs", "screenshots");
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (item, ok, detail, shot = "") => { results.push({ item, ok, detail, shot }); console.log(`[${ok ? "통과" : "실패"}] ${item} — ${detail}`); };

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const jsErrors = [];
  page.on("pageerror", (e) => jsErrors.push(e.message));
  const shot = async (name, full = true) => { await page.waitForTimeout(400); await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: full }); return `screenshots/${name}.png`; };
  const answers = () => page.locator("#chat-thread .msg.bot").count();

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle" });
  check("면책 안내가 화면 상단·하단에 표시", await page.locator(".disclaimer").isVisible() && (await page.locator(".footer").innerText()).includes("대신하지 않습니다"),
    "상단 고정 안내 + 하단 안내", await shot("01_시작화면"));

  // 첫 화면은 내 차 설정. 설정 전에는 기능 메뉴·기능 화면이 보이지 않고 [시동 걸기]가 꺼져 있다
  const lockedBefore = await page.locator("#page-onboard").isVisible() && !(await page.locator("#nav").isVisible())
    && await page.locator("#ob-start").isDisabled();
  await page.goto(BASE + "/#ask", { waitUntil: "networkidle" });  // 주소로 기능 화면에 바로 가려 해도
  const lockedDirect = await page.locator("#page-onboard").isVisible() && !(await page.locator("#page-ask").isVisible());
  check("첫 화면은 내 차 설정, 설정 전에는 기능을 쓸 수 없음", lockedBefore && lockedDirect,
    `첫 화면 온보딩·메뉴 숨김·시동 버튼 꺼짐=${lockedBefore}, #ask 주소로 들어가도 온보딩=${lockedDirect}`);

  await page.click('#ob-brand [data-brand="현대"]');
  await page.click('#ob-model [data-model="아이오닉 5"]');
  await page.click('#ob-powertrain [data-pt="전기차"]');
  // 연식은 기본 숫자 화살표 대신 − / + 단추 (한 해씩)
  await page.fill("#ob-year", "2021");
  await page.click('.ys-btn[data-step="1"]');
  await page.click('.ys-btn[data-step="1"]');
  await page.click('.ys-btn[data-step="-1"]');
  const year = await page.inputValue("#ob-year");
  check("첫 화면 연식을 − / + 단추로 한 해씩 바꿈", year === "2022", `2021 입력 → + + − → ${year}`);
  const ob = await shot("02_내차설정");
  await page.click("#ob-start");
  await page.waitForSelector("#page-home:not([hidden])");
  await page.waitForFunction(() => !document.querySelector("#recall-badge").hidden, null, { timeout: 30000 });
  const tiles = await page.locator(".tile:visible").count();
  check("내 차 설정 후 홈에서 기능 선택 (화면 중앙 타일 4개)", tiles === 4 && await page.locator("#nav").isVisible(),
    `내 차: ${await page.locator("#car-chip-text").innerText()}, 리콜 ${await page.locator("#recall-badge").innerText()}건, 기능 타일 ${tiles}개`, ob);
  await shot("14_홈");
  await page.click('.tile[data-go="find"]');

  // 조건 기반 탐색: 색 필터를 켜면 그 색이 아닌 항목이 없어야 한다
  await page.fill("#find-input", "느낌표 있는 경고등");
  await page.click('#color-chips [data-v="빨강"]');
  await page.waitForSelector(".signal-row");
  // '다른 후보'는 접힌 상자 안이라 innerText가 비므로 textContent로 읽는다
  const subs = await page.locator("#find-results .signal-row .signal-sub").allTextContents();
  check("자연어 조건 + 색 필터 결과에 조건과 맞지 않는 항목 없음", subs.length > 0 && subs.every((t) => t.includes("빨강")),
    `조건: '느낌표 있는 경고등' + 빨강 → ${subs.length}건`, await shot("03_경고등찾기"));
  await page.click('#color-chips [data-v="빨강"]');

  // 계기판과 상관없는 설명("페이커")에는 후보 대신 설명 방법을 안내하고, 예시를 누르면 바로 다시 찾는다
  await page.fill("#find-input", "페이커");
  await page.click('#find-form button[type="submit"]');
  await page.waitForSelector("#find-results .off-topic");
  const offMain = await page.locator("#find-results > .result-list .signal-row").count();
  const offShot = await shot("15_엉뚱한설명");
  await page.locator("#find-results .off-topic .chip").nth(1).click();
  await page.waitForSelector("#find-results .signal-row.rank-1");
  const exTop = await page.locator("#find-results .signal-row.rank-1 .signal-name").innerText();
  // 자동차 낱말과 글자만 겹치는 말('후드티'의 '후드')도 안내 (검색 점수가 애매해 입력 검사 LLM 이 판정)
  await page.fill("#find-input", "후드티");
  await page.click('#find-form button[type="submit"]');
  await page.waitForSelector("#find-results .off-topic", { timeout: 30000 });
  const hoodMain = await page.locator("#find-results > .result-list .signal-row").count();
  check("계기판과 상관없는 설명에는 후보 대신 안내, 예시로 바로 다시 찾기",
    offMain === 0 && hoodMain === 0 && exTop.includes("파워다운"),
    `'페이커' → 주요 후보 ${offMain}건 + 설명 방법 안내(후보는 '그래도 후보 보기'로 접음), 예시 '거북이 모양' → 1위 '${exTop}', `
    + `'후드티'(글자만 겹침) → 주요 후보 ${hoodMain}건 + 안내`, offShot);

  // 경고등 찾기 → 질문하기: 후보에서 바로 그 신호로 질문 이어가기
  await page.fill("#find-input", "주황색인데 타이어 안에 느낌표 있는 거");
  await page.click('#find-form button[type="submit"]');
  await page.waitForFunction(() => document.querySelector(".signal-row .signal-name")?.innerText.includes("타이어"));
  const tpms = await page.locator(".signal-row .signal-name").first().innerText();
  await page.locator('.signal-row [data-act="ask"]').first().click();
  const picked = await page.locator("#ask-picker input").inputValue();
  check("제시된 항목을 고르면 그 항목으로 질문을 이어감", picked === tpms, `찾기 1위 '${tpms}' → 질문하기 신호 '${picked}'`);

  // 진행 상태 → 근거 번호와 근거 목록
  await page.fill("#ask-input", "이거 켜지면 계속 달려도 돼?");
  await page.press("#ask-input", "Enter");
  await page.waitForSelector("#chat-thread .progress");
  const prog = await shot("04_진행상태", false);
  check("답변 생성 중 진행 상태 표시", true, "단계별 '✓' + 진행 중 표시", prog);
  await page.waitForFunction(() => document.querySelectorAll("#chat-thread .msg.bot .answer").length === 1, null, { timeout: 180000 });
  const cites = await page.locator("#chat-thread .answer .cite").allInnerTexts();
  await page.locator("#chat-thread details.evidence summary").first().click();
  const evNums = (await page.locator("#chat-thread .ev-n").allInnerTexts()).map((t) => t.replace(/\D/g, ""));
  const same = [...new Set(cites)].sort().join() === [...new Set(evNums)].sort().join();
  check("답변 근거 번호와 화면 근거 목록 일치 (인용한 근거만 표시)", cites.length > 0 && same,
    `본문 번호 ${[...new Set(cites)]} / 근거 목록 ${evNums}`, await shot("05_질문답변_근거"));
  const badges = await page.locator("#chat-thread .ev .src").allInnerTexts();
  check("근거에 출처 종류 구분 표시", badges.length > 0, `근거 출처 배지: ${[...new Set(badges)].join(", ")}`);

  // 신호별 대화 분리 + 인용 없음 안내
  await page.click("#ask-picker .picker-clear");
  const freeEmpty = (await answers()) === 0;
  await page.fill("#ask-input", "와이퍼 고무는 얼마마다 갈아야 해?");
  await page.press("#ask-input", "Enter");
  await page.waitForSelector("#chat-thread .msg.bot .answer", { timeout: 180000 });
  const noCite = await page.locator("#chat-thread .msg.bot").innerText();
  check("인용된 근거가 없으면 화면에 안내", noCite.includes("인용된 근거 문서가 없습니다"), `와이퍼 질문 → ${noCite.split("\n")[0]}`, await shot("06_인용없음안내"));
  // 설명이 여러 신호에 맞을 수 있는 자유 질문 → 후보 신호를 함께 표시 (#7 개선)
  await page.fill("#ask-input", "핸들 그림이 빨갛게 뜨면서 경고음이 울리면 차를 세우고 정비소에 가야 해?");
  await page.press("#ask-input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll("#chat-thread .msg.bot .answer").length === 2, null, { timeout: 180000 });
  const lastBot = page.locator("#chat-thread .msg.bot").last();
  const alts = await lastBot.locator(".alt-signals .chip").allInnerTexts();
  const wiperExtras = await page.locator("#chat-thread .msg.bot").first().locator(".alt-signals, details.evidence").count();
  await page.evaluate(() => { document.querySelector("#ask-form").style.visibility = "hidden"; });  // 고정 입력창이 답을 가리지 않게
  await lastBot.screenshot({ path: path.join(SHOTS, "13_후보신호.png") });
  await page.evaluate(() => { document.querySelector("#ask-form").style.visibility = ""; });
  check("자유 질문의 설명에 맞는 신호 후보를 답변과 함께 표시", alts.length > 1,
    `핸들 빨간 그림 질문 → 후보 ${alts.map((t) => t.trim()).join(", ")}`, "screenshots/13_후보신호.png");
  check("근거가 없는 답(와이퍼)에는 후보 신호·리콜을 붙이지 않음", wiperExtras === 0, `와이퍼 답변의 후보·리콜 블록 ${wiperExtras}개`);
  // 인사는 근거 검색 없이 대화로 답하고, '인용된 근거 없음' 안내를 붙이지 않는다
  await page.fill("#ask-input", "고마워");
  await page.press("#ask-input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll("#chat-thread .msg.bot .answer").length === 3, null, { timeout: 180000 });
  const thanks = page.locator("#chat-thread .msg.bot").last();
  const thanksText = (await thanks.locator(".answer").innerText()).trim();
  const thanksNote = (await thanks.innerText()).includes("인용된 근거 문서가 없습니다");
  check("인사('고마워')는 근거 검색 없이 대화로 답하고 근거 안내를 붙이지 않음", !thanksNote && !thanksText.includes("확인되지 않"),
    `'고마워' → ${thanksText.split("\n")[0]} / 근거 안내 ${thanksNote ? "있음" : "없음"}`);
  await page.locator("#chat-list [data-id]").filter({ hasText: "저압 타이어" }).click();
  const tpmsMsgs = await page.locator("#chat-thread .msg").count();
  const hasWiper = (await page.locator("#chat-thread").innerText()).includes("와이퍼");
  check("항목을 바꿔도 이전 대화가 섞이지 않음", freeEmpty && tpmsMsgs === 2 && !hasWiper,
    `자유 질문으로 바꿨을 때 빈 대화=${freeEmpty}, TPMS로 돌아왔을 때 메시지 ${tpmsMsgs}개, 와이퍼 대화 섞임=${hasWiper}`, await shot("07_신호별대화"));

  // 진단 카드 + 리콜 패널
  await page.click('.nav-btn[data-page="card"]');
  await page.click("#card-picker input");
  await page.keyboard.type("12V");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".diag", { timeout: 180000 });
  await page.click("#card-body details.evidence summary");
  const urg = await page.locator(".urgency-level").innerText();
  const cardEv = await page.locator("#card-body .ev").count();
  const sideRecalls = await page.locator(".side-panel .recall").count();
  check("도메인 특화 기능(진단 카드)에 근거 제시", cardEv > 0, `긴급도 ${urg}, 인용 근거 ${cardEv}건, 관련 리콜 ${sideRecalls}건`, await shot("08_진단카드"));

  await page.click('.nav-btn[data-page="recall"]');
  await page.waitForSelector("#recall-body .recall");
  check("내 차 리콜 화면", true, `${await page.locator("#recall-body .recall").count()}건 표시`, await shot("09_내차리콜"));

  // 키 누락·색인 없음: 서버 상태 응답을 바꿔 화면 안내 확인
  const meta = await (await page.request.get(`${BASE}/api/meta`)).json();
  for (const [name, status, expect] of [["10_키없음안내", { ...meta.status, key_ok: false }, "API 키"], ["11_색인없음안내", { ...meta.status, meta_count: 0, doc_count: 0 }, "검색 색인이 없습니다"]]) {
    await page.route("**/api/meta", (r) => r.fulfill({ json: { ...meta, status } }));
    await page.goto(`${BASE}/?check=${name}#find`, { waitUntil: "networkidle" });  // 주소가 같으면 다시 로드되지 않아 쿼리를 바꾼다
    const text = await page.locator("#blocker").innerText();
    check(`${expect.includes("키") ? "API 키 누락" : "색인 없음"} 안내`, text.includes(expect) && !(await page.locator("#page-find").isVisible()),
      text.split("\n")[0], await shot(name, false));
    await page.unroute("**/api/meta");
  }

  // 모바일 폭
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + "/#card", { waitUntil: "networkidle" });
  check("좁은 화면(390px)에서 가로 스크롤 없음", await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    "모바일 폭 레이아웃", await shot("12_모바일", false));

  check("화면 JS 오류 없음", jsErrors.length === 0, jsErrors.join(" / ") || "없음");
  await browser.close();

  const md = ["# 화면 점검 결과 (자동 생성)", "", `\`node evaluation/ui_check.js\` 실행 결과 · ${new Date().toLocaleString("ko-KR")}`, "",
    "| 항목 | 결과 | 확인 내용 | 캡처 |", "|---|---|---|---|",
    ...results.map((r) => `| ${r.item} | ${r.ok ? "✅ 통과" : "❌ 실패"} | ${r.detail.replace(/\|/g, "/")} | ${r.shot ? `[보기](${r.shot})` : ""} |`)];
  fs.writeFileSync(path.join(ROOT, "docs", "화면_점검결과.md"), md.join("\n") + "\n");
  console.log("저장: docs/화면_점검결과.md, 실패:", results.filter((r) => !r.ok).length);
})().catch((e) => { console.error("점검 실패:", e.message); process.exit(1); });
