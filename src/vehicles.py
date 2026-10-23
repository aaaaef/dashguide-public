"""내 차 선택: 차종 이름이 아니라 '동력 방식'으로 참고할 취급설명서와 적용 신호를 정한다.

같은 동력 방식(휘발유·디젤·하이브리드·전기)이면 계기판 경고등 체계가 대부분 같으므로,
수집한 매뉴얼(투싼·투싼 HEV·아이오닉 5·기아 EV)을 같은 동력 방식 차량의 참고 자료로 쓴다.
"""

NOT_SELECTED = "선택 안 함"
NOT_LISTED = "목록에 없음 / 수입차"

UNKNOWN = "모름"
POWERTRAINS = [UNKNOWN, "휘발유", "디젤", "하이브리드", "전기차"]

# 제조사 → {대표 차종: 선택 가능한 동력 방식}. 차종 목록은 사용자가 '내 차가 없다'고 느끼지 않게 하고
# 동력 방식 선택지를 좁히는 용도다(국내 판매 구성 기준의 대략값이며 연식별로 다를 수 있다).
MODELS = {
    "현대": {
        "아반떼": ["휘발유", "하이브리드"], "쏘나타": ["휘발유", "하이브리드"], "그랜저": ["휘발유", "하이브리드"],
        "투싼": ["휘발유", "디젤", "하이브리드"], "싼타페": ["휘발유", "디젤", "하이브리드"],
        "팰리세이드": ["휘발유", "디젤", "하이브리드"], "코나": ["휘발유", "하이브리드", "전기차"],
        "캐스퍼": ["휘발유", "전기차"], "스타리아": ["휘발유", "디젤", "하이브리드"],
        "아이오닉 5": ["전기차"], "아이오닉 6": ["전기차"], "포터": ["디젤", "전기차"],
    },
    "기아": {
        "K3": ["휘발유"], "K5": ["휘발유", "하이브리드"], "K8": ["휘발유", "하이브리드"],
        "쏘렌토": ["휘발유", "디젤", "하이브리드"], "스포티지": ["휘발유", "디젤", "하이브리드"],
        "카니발": ["휘발유", "디젤", "하이브리드"], "셀토스": ["휘발유", "디젤"], "니로": ["하이브리드", "전기차"],
        "레이": ["휘발유", "전기차"], "EV3": ["전기차"], "EV6": ["전기차"], "EV9": ["전기차"],
        "봉고": ["디젤", "전기차"],
    },
    "제네시스": {
        "G70": ["휘발유", "디젤"], "G80": ["휘발유", "디젤", "전기차"], "G90": ["휘발유"],
        "GV70": ["휘발유", "디젤", "전기차"], "GV80": ["휘발유", "디젤"],
    },
    "기타": {NOT_LISTED: POWERTRAINS[1:]},
}

# 동력 방식 → signals.csv '적용 파워트레인'에서 찾을 표기
_TOKEN = {"휘발유": "가솔린", "디젤": "디젤", "하이브리드": "HEV", "전기차": "EV"}

# 수집한 매뉴얼의 실제 차종 (이 차종·동력 방식이면 '내 차 매뉴얼'이고, 아니면 같은 동력 방식의 대표 매뉴얼이다)
_EXACT = {("투싼", "휘발유"), ("투싼", "디젤"), ("투싼", "하이브리드"), ("아이오닉 5", "전기차"),
          ("팰리세이드", "휘발유"), ("팰리세이드", "디젤")}


def powertrain_options(model: str) -> list[str]:
    """고른 차종이 가질 수 있는 동력 방식. 차종을 안 골랐거나 목록에 없으면 전부."""
    for models in MODELS.values():
        if model in models:
            return [UNKNOWN, *models[model]]
    return POWERTRAINS


def brand_of(model: str) -> str:
    return next((b for b, ms in MODELS.items() if model in ms), "")


def reference_manual(powertrain: str, model: str = "") -> str:
    """retrieve 가 우선할 매뉴얼 차명. 같은 동력 방식의 수집 매뉴얼을 가리킨다."""
    if powertrain == "전기차":
        return "기아 EV" if brand_of(model) == "기아" else "아이오닉5"
    if model == "팰리세이드" and powertrain in ("휘발유", "디젤"):
        return "팰리세이드"  # 팰리세이드 휘발유·디젤은 자기 매뉴얼 (하이브리드 LX3는 미수집)
    return {"휘발유": "투싼", "디젤": "투싼", "하이브리드": "투싼HEV"}.get(powertrain, "")


def is_exact_manual(powertrain: str, model: str = "") -> bool:
    return (model, powertrain) in _EXACT


def describe(model: str, powertrain: str) -> str:
    """LLM 프롬프트에 넘길 실제 차량 설명 (참고 매뉴얼이 아니라 사용자의 차)."""
    parts = [model if model and model != NOT_SELECTED else "", powertrain if powertrain != UNKNOWN else ""]
    return " ".join(p for p in parts if p)


def applies(signal_powertrain: str, powertrain: str) -> bool:
    """신호가 이 동력 방식 차량에 해당하는지. 모름이거나 '공통'이면 해당한다."""
    if powertrain not in _TOKEN or signal_powertrain.startswith("공통"):
        return True
    return _TOKEN[powertrain] in signal_powertrain


def find_recalls(model: str, powertrain: str = UNKNOWN, year: int | None = None,
                 keywords: list[str] | None = None, limit: int = 30) -> list[dict]:
    """내 차(차종·동력 방식·연식)의 국내 리콜. 화면 차종 이름으로 리콜 데이터 차명·제작자를 맞춘다."""
    from src.data_prep import recalls  # 리콜 캐시는 차종을 골랐을 때만 읽는다

    if not model or model in (NOT_SELECTED, NOT_LISTED):
        return []
    mixed = len(powertrain_options(model)) > 2  # 모름 + 동력 방식 2개 이상
    rows = recalls.search(model, keywords, year, recalls.MAKERS.get(brand_of(model), ""), limit,
                          powertrain=powertrain, mixed=mixed)
    return [recalls.to_document(r) for r in rows]
