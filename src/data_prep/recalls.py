"""국내 리콜 데이터 수집·검색 (외부 문서 출처: 한국교통안전공단_차종별 리콜대수).

API는 조건 검색을 지원하지 않고 페이지 조회만 가능하다.
그래서 전체를 한 번 받아 data/raw 에 캐시하고, 검색은 캐시에서 한다.
출처: https://www.data.go.kr/data/15125831/fileData.do (이용허락범위 제한 없음)
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import requests

from src.config import DATA_GO_KR_KEY, RAW_DIR
from src.data_prep import http

API_URL = "https://api.odcloud.kr/api/15125831/v1/uddi:6c285bb3-db42-46c8-8aed-c1249fe30206"
SOURCE_PAGE = "https://www.data.go.kr/data/15125831/fileData.do"
SOURCE_TYPE = "공공 리콜"
CACHE = RAW_DIR / "recalls.json"
PER_PAGE = 1000
REQUEST_INTERVAL = 0.5  # 호출 간격


class RecallError(RuntimeError):
    """화면에 그대로 보여 줄 수 있는 안내 메시지만 담는다."""


# ---------- 수집 ----------
def fetch_all(force: bool = False) -> list[dict]:
    """전체 리콜 데이터를 받아 캐시한다. 캐시가 있으면 다시 호출하지 않는다."""
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text(encoding="utf-8"))["data"]
    if not DATA_GO_KR_KEY:
        raise RecallError("공공데이터포털 인증키(DATA_GO_KR_KEY)가 설정되지 않았습니다. .env 를 확인하세요.")

    rows, page = [], 1
    while True:
        try:
            r = http.get(
                API_URL,
                params={"page": page, "perPage": PER_PAGE, "serviceKey": DATA_GO_KR_KEY},
                timeout=30,
            )
        except requests.RequestException:
            raise RecallError("리콜 데이터 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.")
        if r.status_code in (401, 403):
            raise RecallError("인증키가 유효하지 않거나 아직 활성화되지 않았습니다. 승인 직후라면 잠시 후 다시 시도하세요.")
        if r.status_code != 200:
            raise RecallError(f"리콜 데이터 조회에 실패했습니다. (HTTP {r.status_code})")

        body = r.json()
        rows.extend(body.get("data", []))
        if page * PER_PAGE >= body.get("totalCount", 0):
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "source": SOURCE_PAGE, "data": rows},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return rows


# ---------- 검색 ----------
# 리콜 데이터 차명은 같은 차도 표기가 제각각이다(그랜저/그랜져, 투싼/투산 등).
_CAR_ALIASES = {"그랜져": "그랜저", "소나타": "쏘나타", "투산": "투싼"}

# 차명에 붙은 동력 방식 표기. HEV·PHEV 안의 'EV'는 전기차로 보지 않는다.
_MARKS = {
    "하이브리드": re.compile(r"HEV|하이브리드", re.I),
    "전기차": re.compile(r"(?<![HP])EV|전기|일렉트릭|electri", re.I),
    "수소": re.compile(r"수소|FC", re.I),
    "디젤": re.compile(r"디젤"),
    "휘발유": re.compile(r"가솔린"),
}

# 화면의 제조사 → 리콜 데이터 제작자 (제네시스는 현대자동차로 등록되어 있다)
MAKERS = {"현대": "현대자동차", "제네시스": "현대자동차", "기아": "기아"}

# 신호 분류(signals.csv '범주') → 리콜사유에서 찾을 장치 이름
CATEGORY_KEYWORDS = {
    "전동화": ["고전압", "구동용 배터리", "구동 모터", "ICCU", "통합 충전", "인버터", "하이브리드", "감속기"],
    "안전": ["에어백", "안전벨트", "좌석벨트", "시트벨트"],
    "제동": ["브레이크", "제동", "ABS", "EBD", "EPB", "오토홀드", "AUTO HOLD"],
    "조향": ["조향", "스티어링", "MDPS", "핸들"],
    "연료": ["연료"],
    "엔진": ["엔진", "시동 꺼짐", "점화"],
    "전기 계통": ["발전기", "알터네이터", "12V", "배터리"],
    "타이어": ["타이어", "TPMS", "공기압"],
    "구동": ["4WD", "AWD", "사륜", "트랜스퍼", "구동축"],
    "디젤": ["요소수", "DPF", "매연", "연료필터", "예열", "SCR"],
    "ADAS": ["충돌", "카메라", "레이더", "크루즈", "차로", "ADAS"],
    "조명": ["전조등", "헤드램프", "램프", "등화"],
    "주행 안정": ["ESC", "차체자세", "VDC"],
    "편의": ["도어", "선루프", "후드", "테일게이트", "스마트키"],
    "변속": ["변속", "기어", "SBW", "P단"],
}


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_()]", "", str(s)).lower()


def _car_key(s: str) -> str:
    s = _norm(s)
    for old, new in _CAR_ALIASES.items():
        s = s.replace(old, new)
    return s


def _year(s: str) -> int | None:
    m = re.search(r"(19|20)\d{2}", str(s))
    return int(m.group()) if m else None


def _powertrain_ok(car_name: str, reason: str, powertrain: str, mixed: bool) -> bool:
    """차명 표기로 동력 방식이 다른 리콜을 거른다.

    mixed: 그 차종이 여러 동력 방식으로 팔리는지. 이때 '싼타페(MX5)'처럼 표기 없는 차명은 내연기관이고
    하이브리드·전기차는 '싼타페 하이브리드(MX5 HEV)'처럼 따로 등록되므로, 하이브리드·전기차는 표기가 있어야 한다.
    휘발유·디젤은 차명에 표기가 없는 경우가 많아 리콜사유에 한쪽 엔진만 나오면 그것도 본다
    (예: 투싼(NX4) '디젤 2.0/2.2 엔진 연료공급호스' → 휘발유 차에서 제외).
    """
    if powertrain not in _MARKS:
        return True
    marks = {k for k, p in _MARKS.items() if p.search(car_name)}
    if "수소" in marks:
        return False
    if powertrain in ("하이브리드", "전기차"):
        others = marks - {powertrain}
        return not others and (powertrain in marks or not mixed)
    if marks - {powertrain}:
        return False
    other = "디젤" if powertrain == "휘발유" else "휘발유"
    return not (_MARKS[other].search(reason) and not _MARKS[powertrain].search(reason))


def search(car_name: str = "", keywords: list[str] | None = None, year: int | None = None,
           maker: str = "", limit: int = 30, powertrain: str = "", mixed: bool = False) -> list[dict]:
    """차명·제작자·생산연도·동력 방식·리콜사유 키워드로 후보를 찾는다.

    keywords 는 하나라도 리콜사유에 들어 있으면 후보로 보고, 겹치는 키워드가 많은 순으로 정렬한다.
    year(연식)를 주면 생산기간에 걸치는 리콜만 남긴다. 연식은 생산연도보다 1년 늦을 수 있어
    생산기간 끝 연도 +1 까지 허용한다(예: 2024년 하반기 생산 2025년형).
    """
    rows = fetch_all()
    car, mk = _car_key(car_name), _norm(maker)
    kws = list(dict.fromkeys(_norm(k) for k in (keywords or []) if k))

    hits = []
    for row in rows:
        name = row.get("차명", "")
        if car and car not in _car_key(name):
            continue
        if mk and mk != _norm(row.get("제작자", "")):
            continue
        if not _powertrain_ok(name, row.get("리콜사유", ""), powertrain, mixed):
            continue
        reason = _norm(row.get("리콜사유", ""))
        score = sum(k in reason for k in kws)
        if kws and not score:
            continue
        if year:
            start, end = _year(row.get("생산기간(부터)")), _year(row.get("생산기간(까지)"))
            if start and year < start:
                continue
            if end and year > end + 1:
                continue
        hits.append((score, row))

    hits.sort(key=lambda h: (h[0], str(h[1].get("리콜개시일", ""))), reverse=True)
    return [row for _, row in hits[:limit]]


def signal_keywords(category: str) -> list[str]:
    return CATEGORY_KEYWORDS.get(category, [])


# ---------- 색인용 문서로 변환 ----------
def to_document(row: dict) -> dict:
    """후보 선별(프롬프트 2)과 색인에 넘길 형태. 원문 출처를 함께 담는다."""
    title = f"[리콜] {row.get('제작자', '')} {row.get('차명', '')} ({row.get('리콜개시일', '')})"
    text = (
        f"제작자: {row.get('제작자', '')}\n"
        f"차명: {row.get('차명', '')}\n"
        f"생산기간: {row.get('생산기간(부터)', '')} ~ {row.get('생산기간(까지)', '')}\n"
        f"리콜개시일: {row.get('리콜개시일', '')}\n"
        f"리콜대수: {row.get('리콜대수', '')}\n"
        f"리콜사유: {row.get('리콜사유', '')}"
    )
    doc_id = "recall:" + _norm(f"{row.get('차명', '')}_{row.get('리콜개시일', '')}_{row.get('생산기간(부터)', '')}")
    return {"doc_id": doc_id, "title": title, "text": text, "source": SOURCE_PAGE, "source_type": SOURCE_TYPE,
            "car": row.get("차명", ""), "period": f"{row.get('생산기간(부터)', '')} ~ {row.get('생산기간(까지)', '')}",
            "date": row.get("리콜개시일", ""), "count": row.get("리콜대수", ""), "reason": row.get("리콜사유", "")}


if __name__ == "__main__":
    try:
        data = fetch_all()
    except RecallError as e:
        print("오류:", e)
        raise SystemExit(1)
    print(f"전체 리콜 건수: {len(data)} (캐시: {CACHE})")
    print("예시 필드:", list(data[0].keys()) if data else "-")
    for car, kw in [("아이오닉", ["브레이크", "주차"]), ("투싼", ["에어백"]), ("", ["ABS"])]:
        found = search(car, kw)
        print(f"\n▶ 차명='{car}' 키워드={kw} → {len(found)}건")
        for r in found[:3]:
            print("  -", to_document(r)["title"], "|", str(r.get("리콜사유", ""))[:80])
