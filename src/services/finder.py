"""증상(색·모양 설명)으로 신호 후보 찾기. 이름을 몰라도 찾을 수 있게 하이브리드 검색에 색·유형 필터를 더한다."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.data_prep.signals import Signal, load_signals
from src.llm import get_judge_llm
from src.rag.prompts import RELEVANCE_PROMPT
from src.vectorstore.signal_meta import normalize_colors, search_signals_checked

COLORS = ["빨강", "노랑", "초록", "파랑", "흰색"]          # 노랑 = 노랑·주황(황색)
KINDS = ["경고등", "표시등", "표시창 경고문", "표시창 정보"]
MIN_RATIO = 0.65   # 1위 점수의 65% 미만은 화면에 보이지 않는다
JUDGE_TIMEOUT = 6  # 입력 검사(프롬프트 4)를 기다리는 최대 초. 넘으면 검사 없이 후보를 보여 준다
# LLM API가 요청 하나만 무작위로 17~19초씩 걸릴 때가 있어(평가 한 번에 시간 초과 7건) 같은 검사를 동시에 2번 보내고 먼저 온 답을 쓴다
JUDGE_COPIES = 2

log = logging.getLogger(__name__)
_judge_pool = ThreadPoolExecutor(max_workers=8)
_judged: dict[str, bool] = {}   # 입력 → 신호를 찾으려는 말인지 (판정에 성공한 것만)


def _ask_judge(text: str, candidates: str) -> bool:
    """프롬프트 4: 검색창에 쓴 말이 자동차 계기판 신호를 찾으려는 말인지. 명시적으로 false 라고 할 때만 False."""
    llm = get_judge_llm(JUDGE_TIMEOUT).bind(response_format={"type": "json_object"})
    reply = llm.invoke(RELEVANCE_PROMPT.format_messages(text=text, candidates=candidates)).content
    return json.loads(reply).get("related") is not False


def _is_signal_search(text: str, found: list[Signal]) -> bool:
    """입력 검사 결과. 같은 요청을 JUDGE_COPIES 번 보내 먼저 성공한 답을 쓰고,
    JUDGE_TIMEOUT 안에 하나도 성공하지 못하면 예외. 후보는 입력으로 정해지므로 입력만으로 기억해 둔다."""
    if text not in _judged:
        # 이름만 주면 "커피잔 모양"을 커피잔으로 읽어 걸렀다. 모양·색을 함께 주면 계기판 그림 설명인지 알아본다.
        candidates = ", ".join(f"{s.name}({s.color}, 모양: {s.shape})" for s in found[:3]) or "(없음)"
        futures = [_judge_pool.submit(_ask_judge, text, candidates) for _ in range(JUDGE_COPIES)]
        for f in as_completed(futures, timeout=JUDGE_TIMEOUT):
            if f.exception() is None:
                if len(_judged) > 1000:
                    _judged.clear()
                _judged[text] = f.result()
                break
        else:
            raise futures[0].exception()
    return _judged[text]


def _matches(s: Signal, colors: list[str], kinds: list[str]) -> bool:
    if kinds and s.kind not in kinds:
        return False
    if colors:
        # '빨강/노랑'처럼 여러 색을 가진 신호도 있어서 하나라도 겹치면 통과
        return any(c in normalize_colors(s.color) for c in colors)
    return True


def find_signals_split(description: str = "", colors: list[str] | None = None, kinds: list[str] | None = None,
                       k: int = 5, more: int = 3) -> tuple[list[Signal], list[Signal], bool]:
    """(주요 후보, 다른 후보, 상관없는 설명 여부). 1위 점수의 MIN_RATIO 이상만 주요 후보로 보여 주고,
    그 아래는 1위가 틀렸을 때 고를 수 있게 '다른 후보'로 따로 돌려준다.
    설명이 계기판 신호와 상관없어 보이면("페이커", "후드티") 주요 후보 없이 '다른 후보'만 준다. 판정이 틀렸을 때도 고를 수 있게."""
    colors, kinds = colors or [], kinds or []
    text = description.strip()
    if not text:
        return [s for s in load_signals() if _matches(s, colors, kinds)][:k], [], False
    # 필터로 걸러질 것을 생각해 넉넉히 받은 뒤 자른다.
    found, relevance = search_signals_checked(text, k=30)
    off_topic = relevance.off_topic
    # 검색 점수가 애매하면(엉뚱한 말과 정상 모양 설명이 섞이는 구간) 찾은 후보와 함께 LLM 에 묻는다.
    # 색·모양 낱말이 있으면 묻지 않던 때는 "빨간 후드티"가 통과했다.
    if not off_topic and not relevance.confident:
        try:
            off_topic = not _is_signal_search(text, [s for s, _ in found])
        except Exception as e:  # noqa: BLE001  시간 초과·API 오류: 검사 없이 후보를 보여 준다
            log.warning("입력 검사 실패, 후보를 그대로 표시: %r", e)
    hits = [(s, sc) for s, sc in found if _matches(s, colors, kinds)]
    if not hits:
        return [], [], off_topic
    if off_topic:
        return [], [s for s, _ in hits][:more], True
    cut = hits[0][1] * MIN_RATIO
    main = [s for s, sc in hits if sc >= cut][:k]
    rest = [s for s, sc in hits if s not in main][:more]
    return main, rest, False


def find_signals(description: str = "", colors: list[str] | None = None, kinds: list[str] | None = None,
                 k: int = 5) -> list[Signal]:
    return find_signals_split(description, colors, kinds, k)[0]
