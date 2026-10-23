"""현대·기아 취급설명서 웹 매뉴얼 수집·검색 (외부 문서 주 출처).

매뉴얼에는 검색 API가 없으므로, 경고등·표시등·경고문 관련 페이지를 미리 내려받아
data/raw/manuals 에 캐시하고, 페이지 안의 소제목 단위로 잘라 '제공 문서 묶음'으로 검색한다.
- 현대: 매뉴얼마다 data/tocData.json(목차)이 있어 제목으로 페이지를 고른다.
- 기아: 목차가 장 단위라 하위 페이지를 찾기 어려워, 신호 확인에 쓴 페이지를 직접 지정한다.
이용 조건: 저작권은 제조사에 있으며 교육 목적 비상업적 사용으로 한정, 출처 URL을 함께 저장한다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser

import requests

from src.config import RAW_DIR
from src.data_prep import http
from src.vectorstore.bm25 import BM25

SOURCE_TYPE = "제조사 매뉴얼"
CACHE_DIR = RAW_DIR / "manuals"
SECTIONS = RAW_DIR / "manual_sections.json"
REQUEST_INTERVAL = 1.0  # 호출 간격
HEADERS = {"User-Agent": "DashGuide/0.1 (warning-light RAG, non-commercial)"}

# 목차 제목이 이 패턴에 맞는 페이지만 받는다. ADAS 경고(충돌·핸즈오프·하차·크루즈)는 기능별 '작동' 페이지에,
# AUTO HOLD·DBC·회생 제동 경고문은 각 기능 페이지에 있다 (투싼HEV searchData.json으로 위치 확인).
PAGE_TITLE_RE = re.compile(
    r"경고등|표시등|경고문|메시지|표시창 정보|계측기|자동 정차 기능|경사로 저속 주행|회생 제동 시스템 사용 제한|승객 구분"
    r"|(충돌방지|차로 유지|차로 이탈|하차|운전자 주의|크루즈 컨트롤|속도 제한 보조|후석 승객 알림).*(작동|이상)"
)


@dataclass
class Manual:
    label: str            # signals.csv '출처 매뉴얼'과 같은 표기
    brand: str
    code: str
    year: str
    pages: list[str] = field(default_factory=list)  # 비어 있으면 목차에서 고른다 (현대)

    @property
    def base(self) -> str:
        site = "hyundai" if self.brand == "현대" else "kia"
        return f"https://ownersmanual.{site}.com/full_webhelp/{self.code}/{self.year}/ko_KR/"

    @property
    def key(self) -> str:
        return f"{self.code}-{self.year}"


MANUALS = [
    Manual("투싼 2025", "현대", "NX4", "2025"),
    Manual("투싼HEV 2026", "현대", "NX4HEV", "2026"),
    # 디젤 보강: 투싼 외에 디젤 신호를 교차 확인할 매뉴얼. 휘발유 3.8·디젤 2.2가 한 권에 있다
    # (스타리아·싼타페 TM·포터는 웹 매뉴얼이 없어 404)
    Manual("팰리세이드 2024", "현대", "LX2", "2024"),
    Manual("아이오닉5 2024", "현대", "NE1", "2024"),
    Manual("아이오닉5 2025", "현대", "NE1", "2025"),
    Manual("기아 EV SV1 2025", "기아", "SV1", "2025", pages=["topics/t00067.html", "topics/t00068.html"]),
]


class ManualError(RuntimeError):
    """화면에 그대로 보여 줄 수 있는 안내 메시지만 담는다."""


# ---------- 수집 ----------
def _get(url: str) -> str:
    try:
        r = http.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException:
        raise ManualError("취급설명서 사이트에 연결하지 못했습니다. 잠시 후 다시 시도하세요.")
    if r.status_code != 200:
        raise ManualError(f"취급설명서 페이지를 가져오지 못했습니다. (HTTP {r.status_code})")
    r.encoding = "utf-8"
    return r.text


def _cached(manual: Manual, path: str) -> str:
    """페이지를 한 번만 내려받는다. 캐시 파일 이름은 경로의 '/'를 '_'로 바꾼 것."""
    file = CACHE_DIR / manual.key / path.replace("/", "_")
    if file.exists():
        return file.read_text(encoding="utf-8")
    text = _get(manual.base + path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    time.sleep(REQUEST_INTERVAL)
    return text


def _toc_pages(manual: Manual) -> list[str]:
    raw = _cached(manual, "data/tocData.json")
    toc = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])  # "var tocData = {...}" 형태
    pages: list[str] = []

    def walk(node: dict):
        for title, href, children in node.values():
            href = href.split("#")[0]
            if PAGE_TITLE_RE.search(title) and href not in pages:
                pages.append(href)
            if isinstance(children, dict):
                walk(children)

    walk(toc)
    return pages


class _SectionParser(HTMLParser):
    """본문(현대 #content-hyundai, 기아 <article>)만 읽어 소제목 단위로 나눈다.
    머리글(thead)이 있는 표는 신호 목록 표(예: 아이오닉5 2025)로 보고 행마다 문서로 나눈다."""
    BLOCK = {"p", "li", "div", "section", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
    SKIP = {"script", "style", "nav", "userinput"}  # userinput = 현대 아이콘 글리프 문자
    VOID = {"br", "img", "hr", "col", "input", "meta", "link", "wbr"}

    def __init__(self):
        super().__init__()
        self.depth = 0          # 본문 안에서의 태그 깊이 (0 = 본문 밖)
        self.skip = 0
        self.mode = ""          # "page" | "section" | "head" | "row" | ""
        self.page_title = ""
        self.sections: list[list] = [["", []]]   # [소제목, 문장들]
        self.buf = ""
        self.headers: list[str] = []
        self.in_head = self.in_body = False
        self.col = 0

    def _flush(self):
        text = re.sub(r"\s+", " ", self.buf).strip()
        self.buf = ""
        if not text:
            return
        if self.mode == "page":
            self.page_title = (self.page_title + " " + text).strip()
        elif self.mode == "section":
            self.sections.append([text, []])
        elif self.in_head:
            self.headers.append(text)
        elif self.mode == "row":        # 표 행의 첫 문장 = 신호 이름
            self.sections.append([text, []])
            self.mode = ""
        else:
            self.sections[-1][1].append(text)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.depth:
            if a.get("id") == "content-hyundai" or tag == "article":
                self.depth = 1
            return
        if tag not in self.VOID:
            self.depth += 1
        if self.skip or tag in self.SKIP:
            self.skip += tag in self.SKIP
            return
        if tag in self.BLOCK:
            self._flush()
        cls = (a.get("class") or "").split()
        if tag == "h3" and not self.page_title:
            self.mode = "page"
        elif "title" in cls and (tag == "div" or "sectiontitle" in cls):
            self.mode = "section"
        elif tag == "thead":
            self.in_head, self.headers = True, []
        elif tag == "tbody" and self.headers:
            self.in_body = True
        elif tag == "tr" and self.in_body:
            self.mode, self.col = "row", 0
        elif tag in ("td", "th") and self.in_body:
            self.col += 1
            if 1 < self.col <= len(self.headers):
                self.sections[-1][1].append(f"[{self.headers[self.col - 1]}]")

    def handle_endtag(self, tag):
        if not self.depth or tag in self.VOID:
            return
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif not self.skip:
            if tag in self.BLOCK or self.mode in ("page", "section"):
                self._flush()
                if self.mode in ("page", "section"):
                    self.mode = ""
            if tag == "thead":
                self.in_head = False
            elif tag == "table" and self.in_body:
                self.in_body, self.headers = False, []
                self.sections.append(["", []])  # 표 뒤 문단은 페이지 제목 아래로
        self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.buf += data


# 그림 번호·이미지 라벨 (예: WPSE001, OSVQ014108, 2C_ShiftPosition)
_FIGURE_CODE = re.compile(r"^(?=.*[\d_])[A-Za-z0-9_.-]{5,}$")


def parse_sections(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _SectionParser()
    p.feed(html)
    p._flush()
    sections = []
    for title, lines in p.sections:
        lines = [ln for ln in lines if not _FIGURE_CODE.match(ln)]
        if lines:
            sections.append((title or p.page_title, "\n".join(lines)))
    return p.page_title, sections


def collect(force: bool = False) -> list[dict]:
    """모든 매뉴얼의 대상 페이지를 받아 소제목 단위 문서로 저장한다.
    MANUALS에 캐시에 없는 매뉴얼이 생기면 다시 만든다. 이미 받은 페이지는 페이지 캐시에서 읽는다."""
    if SECTIONS.exists() and not force:
        docs = json.loads(SECTIONS.read_text(encoding="utf-8"))["data"]
        if {m.label for m in MANUALS} <= {d["manual"] for d in docs}:
            return docs

    docs = []
    for m in MANUALS:
        for path in (m.pages or _toc_pages(m)):
            page_title, sections = parse_sections(_cached(m, path))
            for n, (title, text) in enumerate(sections):
                docs.append({
                    "doc_id": f"{m.key}:{path.rsplit('/', 1)[-1].removesuffix('.html')}#{n}",
                    "title": title,
                    "page_title": page_title,
                    "text": text,
                    "manual": m.label,
                    "brand": m.brand,
                    "source": m.base + path,
                    "source_type": SOURCE_TYPE,
                })

    SECTIONS.parent.mkdir(parents=True, exist_ok=True)
    SECTIONS.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "data": docs}, ensure_ascii=False),
        encoding="utf-8",
    )
    return docs


# ---------- 검색 ----------
@lru_cache(maxsize=1)
def _index() -> tuple[BM25, dict[str, dict]]:
    docs = collect()
    # 제목 검색이 기본이라 제목을 두 번 넣어 본문보다 가중치를 준다.
    texts = [f"{d['title']} {d['title']} {d['page_title']} {d['text'][:300]}" for d in docs]
    return BM25([d["doc_id"] for d in docs], texts), {d["doc_id"]: d for d in docs}


def _norm(s: str) -> str:
    return re.sub(r"[^\w가-힣]", "", s).lower()


def match_tier(doc: dict, query: str) -> int:
    """질의 문구와 문서의 일치 등급. 0 제목 같음, 1 제목에 포함, 2 본문에 포함,
    3 본문이 질의 글자(2-gram)의 85% 이상 포함 (예: 'AUTO HOLD의 작동 조건' 같은 조사 차이), 4 그 외.
    경고문은 소제목이 아니라 본문 줄로 들어 있는 경우가 많아 본문 일치도 본다."""
    q, title, text = _norm(query), _norm(doc["title"]), _norm(doc["text"])
    if not q:
        return 4
    if title == q:
        return 0
    if q in title:
        return 1
    if q in text:
        return 2
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    if len(grams) >= 4 and sum(g in text for g in grams) / len(grams) >= 0.85:
        return 3
    return 4


def search(query: str, manual: str = "", k: int = 5) -> list[dict]:
    """키워드 검색. 질의 문구와 일치하는 문서(match_tier)를 먼저 올리고, 같은 등급 안에서는 BM25 순서.
    manual 을 주면 '출처 매뉴얼' 표기가 그 문자열을 포함하는 것만 남긴다."""
    index, by_id = _index()
    hits = [by_id[doc_id] for doc_id, _ in index.search(query, k=len(by_id))]
    hits.sort(key=lambda d: match_tier(d, query))  # 안정 정렬이라 등급 안에서는 BM25 순서 유지
    if manual:
        hits = [d for d in hits if manual in d["manual"]]
    return hits[:k]


if __name__ == "__main__":
    try:
        docs = collect()
    except ManualError as e:
        print("오류:", e)
        raise SystemExit(1)
    print(f"전체 소제목 문서: {len(docs)}건 (캐시: {SECTIONS})")
    for m in MANUALS:
        mine = [d for d in docs if d["manual"] == m.label]
        print(f"  - {m.label}: 페이지 {len({d['source'] for d in mine})}개, 문서 {len(mine)}건")
    for q in ["파워다운 표시등", "브레이크 경고등", "변속기 과열"]:
        print(f"\n▶ {q}")
        for d in search(q, k=3):
            print(f"  - [{d['manual']}] {d['title']} | {d['text'][:60]}")
