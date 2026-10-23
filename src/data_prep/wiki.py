"""위키백과 수집 (외부 문서 출처: 백과사전, 보조 출처 — 장치 원리·용어).

signals.csv '관련 위키 문서'에 적은 제목을 한국어 위키백과에서 받고, 한국어 본문이 짧으면
영어판 도입부를 함께 받는다. 결과는 data/raw/wiki.json 에 캐시해 다시 호출하지 않는다.
API 호출은 User-Agent에 연락처(WIKI_CONTACT)를 넣고 간격을 둔다.
이용 조건: CC BY-SA 4.0, 출처(문서 URL) 표기.
"""
from __future__ import annotations

import json
import re
import threading
import time

import requests

from src.config import RAW_DIR, WIKI_CONTACT
from src.data_prep import http
from src.data_prep.signals import load_signals

SOURCE_TYPE = "백과사전"
CACHE = RAW_DIR / "wiki.json"
TERM_CACHE = RAW_DIR / "wiki_terms.json"   # 질문하기에서 물은 용어 (lookup_term)
_cache_lock = threading.Lock()
REQUEST_INTERVAL = 1.0
KO_MIN_CHARS = 800      # 한국어 본문이 이보다 짧으면 영어판 도입부로 보완
KO_MAX_CHARS = 2500     # 원리 설명만 쓰므로 앞부분만 (긴 문서가 매뉴얼 근거를 밀어내지 않게)
EN_MAX_CHARS = 1500

# CSV 제목이 동음이의어 문서로 가는 경우의 정식 제목
TITLE_FIX = {"OBD": "온보드 진단기", "배터리": "자동차 배터리"}
# 여러 신호(10건 이상)에 걸린 넓은 주제 문서는 본문이 운용비·시장 등 경고등과 먼 내용이라 도입부만 쓴다
INTRO_ONLY = {"전기 자동차", "하이브리드 차량", "선진 운전자 지원 시스템"}
# 원리·용어와 관계없는 단락은 뺀다
_SKIP_SECTIONS = re.compile(r"역사|같이 보기|각주|외부 링크|참고 문헌|참고 자료|출처|각국|시장|판매|논란|"
                            r"History|See also|References|External links|Further reading|Notes|Sales|Market")


class WikiError(RuntimeError):
    """화면·콘솔에 그대로 보여 줄 수 있는 안내 메시지만 담는다."""


def _headers() -> dict:
    if not WIKI_CONTACT:
        raise WikiError("위키백과 API 호출에 필요한 연락처(WIKI_CONTACT)가 설정되지 않았습니다. .env 를 확인하세요.")
    return {"User-Agent": f"DashGuide/0.1 (warning-light RAG, non-commercial; {WIKI_CONTACT})"}


_call_lock = threading.Lock()
_last_call = 0.0


def _query(lang: str, title: str, intro_only: bool = False) -> dict:
    params = {"action": "query", "prop": "extracts|langlinks|info|pageprops", "explaintext": 1, "redirects": 1,
              "titles": title, "lllang": "en", "inprop": "url", "format": "json", "formatversion": 2}
    if intro_only:
        params["exintro"] = 1
    global _last_call
    with _call_lock:  # 호출 사이 간격 (질문 때 한 번만 부를 때는 기다리지 않도록 뒤가 아니라 앞에서 맞춘다)
        time.sleep(max(0.0, REQUEST_INTERVAL - (time.time() - _last_call)))
        _last_call = time.time()
    try:
        r = http.get(f"https://{lang}.wikipedia.org/w/api.php", params=params, headers=_headers(), timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        raise WikiError("위키백과 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.")
    return r.json()["query"]["pages"][0]


def _clean(text: str, limit: int) -> str:
    """'== 제목 ==' 단락 중 원리와 관계없는 단락을 빼고 앞에서부터 limit 자까지."""
    parts = re.split(r"\n(={2,}[^=\n]+={2,})\n", text)
    kept, skipping = [parts[0].strip()], False
    for p in parts[1:]:
        if re.fullmatch(r"={2,}[^=\n]+={2,}", p):
            level = len(p) - len(p.lstrip("="))
            if level == 2:
                skipping = bool(_SKIP_SECTIONS.search(p))
            if not skipping:
                kept.append(p.strip("= ").strip())
        elif not skipping and p.strip():
            kept.append(p.strip())
    out = "\n".join(k for k in kept if k)
    return out[:limit].rsplit("\n", 1)[0] if len(out) > limit else out


def _doc(lang: str, page: dict, text: str) -> dict:
    label = "한국어" if lang == "ko" else "영어"
    return {"doc_id": f"wiki:{lang}:{page['title']}", "title": page["title"], "text": text,
            "manual": f"위키백과({label})", "source": page["fullurl"], "source_type": SOURCE_TYPE}


def fetch_term(term: str) -> list[dict]:
    """용어 하나 → 문서 0~2건 (한국어, 필요하면 영어 도입부)."""
    ko = _query("ko", TITLE_FIX.get(term, term), intro_only=term in INTRO_ONLY)
    if ko.get("missing") or "disambiguation" in ko.get("pageprops", {}):
        return []
    docs = []
    ko_text = _clean(ko.get("extract", ""), KO_MAX_CHARS)
    if ko_text:
        docs.append(_doc("ko", ko, ko_text))
    en_title = next((l["title"] for l in ko.get("langlinks", []) if l["lang"] == "en"), "")
    if len(ko_text) < KO_MIN_CHARS and en_title:
        en = _query("en", en_title, intro_only=True)
        en_text = _clean(en.get("extract", ""), EN_MAX_CHARS)
        if en_text and not en.get("missing"):
            docs.append(_doc("en", en, en_text))
    return docs


def collect(force: bool = False) -> dict[str, list[dict]]:
    """신호에 적힌 위키 용어 전체를 받는다. 캐시에 있는 용어는 다시 받지 않는다. 반환: 용어 → 문서."""
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() and not force else {}
    terms = sorted({w for s in load_signals() for w in s.wiki})
    new = [t for t in terms if t not in cache]
    for t in new:
        cache[t] = fetch_term(t)
    if new:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def lookup_term(term: str) -> list[dict]:
    """질문하기에서 뜻을 물은 용어("액추에이터가 뭐야?") → 위키 문서. 신호에 연결한 용어만 미리 받아 두어서
    그 밖의 용어는 질문 때 받는다. 받은 결과(문서가 없는 것도)는 TERM_CACHE 에 남겨 다시 부르지 않는다.
    '용어 (자동차)' 문서가 있으면 그쪽을 먼저 쓴다 (예: 같은 이름의 다른 분야 문서를 피하려고)."""
    term = re.sub(r"\s+", " ", term).strip()
    if not term or len(term) > 40:
        return []
    known = docs_for_signal([term])
    if known:
        return known
    with _cache_lock:
        cache = json.loads(TERM_CACHE.read_text(encoding="utf-8")) if TERM_CACHE.exists() else {}
    if term not in cache:
        docs = fetch_term(f"{term} (자동차)") or fetch_term(term)
        with _cache_lock:
            cache = json.loads(TERM_CACHE.read_text(encoding="utf-8")) if TERM_CACHE.exists() else {}
            cache[term] = docs
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            TERM_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache[term]


def docs_for_signal(wiki_terms: list[str]) -> list[dict]:
    """신호의 위키 용어 → 문서 (캐시에서만 읽는다. 없으면 python -m src.data_prep.wiki 로 먼저 수집)."""
    if not CACHE.exists():
        return []
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    return [d for t in wiki_terms for d in cache.get(t, [])]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = collect()
    except WikiError as e:
        print("오류:", e)
        raise SystemExit(1)
    for term, docs in data.items():
        desc = ", ".join(f"{d['manual']} '{d['title']}' {len(d['text'])}자" for d in docs) or "문서 없음"
        print(f"- {term}: {desc}")
    print(f"\n용어 {len(data)}개, 문서 {sum(len(d) for d in data.values())}건 (캐시: {CACHE})")
