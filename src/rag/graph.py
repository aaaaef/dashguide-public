"""LangGraph 질의응답. extract(프롬프트 1) → retrieve → generate(프롬프트 3).
retrieve 는 signal_meta 로 신호를 특정한 뒤 그 신호들의 signal_docs 에서 근거를 찾고,
같은 문단이 여러 매뉴얼·신호에 중복 색인된 것을 합쳐 근거 번호 [n]을 붙인다.
내 차를 알면 신호 장치와 관련된 국내 리콜도 찾는다(recalls). 리콜은 신호의 뜻·대처 근거가 아니므로
질문이 리콜·결함을 물을 때만 답변 근거에 넣고, 그 밖에는 화면에 따로 보여 준다.
인사·감사("고마워")와 자동차와 상관없는 질문은 extract 가 유형으로 가려 근거 검색 없이 chat 으로 답한다.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal, TypedDict

import numpy as np
from langchain_core.documents import Document
from langchain_core.vectorstores.utils import maximal_marginal_relevance
from langgraph.graph import END, START, StateGraph

from src.config import MMR_FETCH_K, MMR_LAMBDA, SEARCH_TYPE, TOP_K
from src.data_prep.recalls import signal_keywords
from src.data_prep.signals import load_signals
from src.data_prep.wiki import SOURCE_TYPE as WIKI_SOURCE
from src.data_prep.wiki import lookup_term
from src.llm import get_embeddings, get_llm
from src.rag.prompts import (ANSWER_PROMPT, CHAT_PROMPT, DIAGNOSIS_CARD_PROMPT, NOT_FOUND, OUT_OF_SCOPE,
                             QUERY_EXTRACT_PROMPT, URGENCY_LEVELS)
from src.vectorstore.signal_docs import get_docs_store
from src.vectorstore.signal_meta import get_meta_chunk, search_signals
from src.vehicles import find_recalls

SIGNAL_CANDIDATES = 3   # 근거를 찾을 신호 수
HISTORY_TURNS = 4       # 프롬프트에 넣을 직전 대화 수
RECALL_K = 5            # 화면에 보여 줄 관련 리콜 수
RECALL_CONTEXT_K = 3    # 리콜을 묻는 질문에서 답변 근거로 넣을 리콜 수
WIKI_K = 1              # 답변 근거에 넣을 위키 문서 수 (보조 출처라 매뉴얼을 밀어내지 않게)
META_SOURCE = "신호 메타데이터"  # 근거 출처 유형: 항목 메타데이터 (외부 문서와 구분)
QUESTION_KINDS = ("질문", "대화", "범위 밖")   # 프롬프트 1의 유형. 대화·범위 밖은 근거 검색 없이 답한다

log = logging.getLogger(__name__)


class QAState(TypedDict, total=False):
    # 입력
    question: str
    history: list[tuple[str, str]]      # [("user"|"assistant", 내용), ...]
    selected_signal: str                # 화면에서 고른 신호 ID (없으면 "")
    selected_car: str
    selected_year: int | None
    vehicle: str
    model: str                          # 화면에서 고른 실제 차종 (리콜 검색용, 없으면 "")
    powertrain: str
    mode: Literal["answer", "card"]
    # 중간·출력
    query: dict
    signals: list[dict]                 # 특정한 신호 [{id, name, urgency}]
    evidence: list[dict]                # [{n, title, manual, source, source_type, signal_id, text}]
    answer: str
    cited: list[int]                    # 답변에서 실제로 인용한 근거 번호
    chat: bool                          # 근거 검색 없이 답했는지 (인사·범위 밖, 화면이 '인용 근거 없음' 안내를 하지 않음)
    card: dict
    recalls: list[dict]                 # 내 차 관련 리콜 (recalls.to_document 형식)


_RECALL_WORDS = re.compile(r"리콜|결함|무상\s*수리|시정\s*조치")
# 질문이 계기판 신호 이야기인지 (용어 질문에서 신호 문서를 뺄지 정할 때)
_SIGNAL_WORDS = re.compile(r"경고등|표시등|표시|경고|문구|메시지|램프|불이|불 |떴|떠요|뜨면|뜨는|켜졌|켜져|켜지|깜빡|들어왔|들어와")
_PRINCIPLE_WORDS = re.compile(r"원리|작동\s*(방식|원리)|어떻게\s*작동|어떤\s*장치|무슨\s*장치|뭐야|뭐고|뭔가요|뭐예요|뭐에요|"
                              r"무엇인|이란|역할|왜\s|용어|어떤\s*거")
# 추출 키워드 중 리콜사유 어디에나 나와 리콜을 임의로 거르게 되는 낱말
_GENERIC_WORDS = {"안전", "안전성", "문제", "점검", "고장", "이상", "증상", "경고등", "표시등", "경고", "차량", "자동차",
                  "전기차", "하이브리드", "휘발유", "디젤", "계기판", "표시", "신호", "경고 표시"}
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")  # [1], [1, 3] 둘 다 인식
_EMPTY_VALUE = re.compile(r"\s*\(?\s*(없음|모름|알 수 없음|null|none|n/?a|-)?\s*\)?\s*", re.I)  # LLM이 빈 값 대신 적는 표기


def _cited_numbers(*texts: str) -> set[int]:
    return {int(n) for t in texts for group in _CITE.findall(t) for n in group.split(",")}


def _history_text(history: list[tuple[str, str]] | None) -> str:
    turns = (history or [])[-HISTORY_TURNS:]
    return "\n".join(f"{'사용자' if r == 'user' else '안내'}: {t}" for r, t in turns) or "(없음)"


def _json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group()) if m else {}


def _car_rank(d: Document, car: str) -> bool:
    """고른 차종 매뉴얼이면 False(앞으로)."""
    return car not in re.sub(r"\s", "", d.metadata.get("manual", ""))


def _paragraph_key(body: str) -> str:
    """같은 문단 판정: 기호를 뺀 본문 앞 200자 (같은 문단이 여러 매뉴얼에 그대로 실려 있다)."""
    return re.sub(r"\W", "", body)[:200]


def _mmr_hits(text: str, where: dict, car: str) -> list[Document]:
    """MMR: 후보를 MMR_FETCH_K건 넉넉히 가져와 같은 문단을 합친 뒤(내 차 매뉴얼 쪽을 남김),
    질문과의 관련성과 이미 고른 청크와의 차이를 함께 따져 순서를 정한다.
    같은 문단을 먼저 합치는 이유: 글자까지 같은 문단을 MMR에 그대로 넣으면 내 차 매뉴얼 쪽이 '겹침'으로 밀려난다."""
    q = get_embeddings().embed_query(text)
    res = get_docs_store()._collection.query(query_embeddings=[q], n_results=MMR_FETCH_K, where=where,
                                             include=["documents", "metadatas", "embeddings"])
    cands = [(Document(page_content=t, metadata=m), e)
             for t, m, e in zip(res["documents"][0], res["metadatas"][0], res["embeddings"][0])]
    if car:
        cands.sort(key=lambda c: _car_rank(c[0], car))
    unique, seen = [], set()
    for d, e in cands:
        key = _paragraph_key(d.page_content.split("\n", 1)[-1])
        if key not in seen:
            seen.add(key)
            unique.append((d, e))
    order = maximal_marginal_relevance(np.array(q), [e for _, e in unique], lambda_mult=MMR_LAMBDA, k=len(unique))
    return [unique[i][0] for i in order]


# ---------- 노드 ----------
def extract(state: QAState) -> QAState:
    """프롬프트 1: 검색어 JSON. 실패하면 질문 자체를 신호 검색어로 쓴다."""
    llm = get_llm().bind(response_format={"type": "json_object"})
    reply = llm.invoke(QUERY_EXTRACT_PROMPT.format_messages(
        history=_history_text(state.get("history")),
        selected_signal=state.get("selected_signal") or "(없음)",
        selected_car=state.get("vehicle") or state.get("selected_car") or "(없음)",
        selected_year=state.get("selected_year") or "(없음)",
        question=state["question"],
    )).content
    try:
        query = _json(reply)
    except json.JSONDecodeError:
        query = {}
    # 화면 선택값이 '(없음)'으로 들어가서 LLM이 빈 값 대신 "없음"을 적을 때가 있다.
    # 그대로 두면 "없음"으로 신호를 검색해 엉뚱한 신호 3개가 후보 자리를 채웠다(요소수 질문 → 파워 스티어링).
    for k in ("신호", "차명", "용어"):
        if not isinstance(query.get(k), str) or _EMPTY_VALUE.fullmatch(query[k]):
            query[k] = ""
    query.setdefault("키워드", [])
    if query.get("유형") not in QUESTION_KINDS:
        query["유형"] = "질문"  # 모르면 근거로 답하는 쪽으로
    return {"query": query}


def route(state: QAState) -> str:
    """인사·감사·범위 밖 질문은 근거 검색 없이 답한다. 진단 카드는 늘 근거로."""
    if state.get("mode") == "card":
        return "retrieve"
    return "chat" if state.get("query", {}).get("유형") in ("대화", "범위 밖") else "retrieve"


def chat(state: QAState) -> QAState:
    """근거 없이 답하는 경우. 범위 밖 질문은 정해 둔 안내만, 인사·감사는 짧은 대화(자동차 정보는 말하지 않음)."""
    base = {"chat": True, "evidence": [], "cited": [], "signals": [], "recalls": []}
    if state["query"]["유형"] == "범위 밖":
        return {**base, "answer": OUT_OF_SCOPE}
    reply = get_llm().invoke(CHAT_PROMPT.format_messages(history=_history_text(state.get("history")),
                                                         question=state["question"])).content
    return {**base, "answer": reply.strip()}


def retrieve(state: QAState) -> QAState:
    question, query = state["question"], state.get("query", {})
    by_id = {s.id: s for s in load_signals()}

    # 1) 신호 특정: 화면 선택 > 추출한 신호명 > 원 질문 (원 질문도 함께 검색해 추출 실패를 보완)
    ids: list[str] = []
    if state.get("selected_signal") in by_id:
        ids.append(state["selected_signal"])
    for q in (query.get("신호"), question):
        if q:
            ids += [s.id for s, _ in search_signals(q, k=SIGNAL_CANDIDATES)]
    ids = list(dict.fromkeys(ids))[:SIGNAL_CANDIDATES]
    if state.get("mode") == "card" and state.get("selected_signal") in by_id:
        ids = [state["selected_signal"]]  # 진단 카드는 고른 신호 하나에 대한 것이라 다른 신호 문서를 섞지 않는다

    # 2) 그 신호들의 문서에서 근거 검색
    search_text = " ".join([question, query.get("신호", ""), *query.get("키워드", [])]).strip()
    where = {"signal_id": {"$in": ids}}
    # 원리 설명(백과사전)은 장치 원리·용어를 물을 때만 쓴다. 카드는 뜻·대처만 다루고, 다른 질문에 상관없는
    # 위키가 섞이면 매뉴얼에 없는 내용을 지어내는 일이 늘었다(엔진 경고등 켜짐/깜빡임 차이 질문).
    term = query.get("용어", "") if state.get("mode") != "card" else ""
    if state.get("mode") == "card" or not (term or _PRINCIPLE_WORDS.search(question)):
        where = {"$and": [where, {"source_type": {"$ne": WIKI_SOURCE}}]}
    car = re.sub(r"\s", "", state.get("selected_car") or query.get("차명") or "")
    if SEARCH_TYPE == "mmr":
        hits = _mmr_hits(search_text, where, car)
    else:
        hits = get_docs_store().similarity_search(search_text, k=TOP_K * 4, filter=where)

    # 3) 고른 차종 매뉴얼 우선. 같은 문단은 하나로 합치고(청크 첫 줄 '[매뉴얼] 제목'은 비교에서 뺀다),
    #    같은 문서의 청크는 근거 번호 하나로 묶는다. 위키는 보조 출처라 WIKI_K건까지만, 번호는 매뉴얼 뒤로.
    if car:
        hits.sort(key=lambda d: _car_rank(d, car))
    docs: dict[str, dict] = {}
    seen = set()
    for d in hits:
        body = d.page_content.split("\n", 1)[-1]
        key = _paragraph_key(body)
        m = d.metadata
        # TOP_K는 매뉴얼 문서 수, 위키는 그와 따로 WIKI_K건 (같이 세면 매뉴얼이 자리를 다 채워 원리 질문에도 위키가 빠졌다)
        is_wiki = m["source_type"] == WIKI_SOURCE
        if key in seen or (m["doc_id"] not in docs
                           and sum((x["source_type"] == WIKI_SOURCE) == is_wiki for x in docs.values())
                           >= (WIKI_K if is_wiki else TOP_K)):
            continue
        seen.add(key)
        doc = docs.setdefault(m["doc_id"], {
            "title": m["title"], "manual": m.get("manual", ""), "source": m["source"],
            "source_type": m["source_type"], "signal_id": m["signal_id"], "chunks": {},
        })
        doc["chunks"][m["chunk_no"]] = body
    # 대상 신호(화면 선택, 없으면 가장 유력한 신호)의 메타데이터 청크(signal_meta)는 항상 근거 [1]로 넣는다.
    # 긴급도 초안 줄은 뺀다. 남겨 두면 LLM이 '긴급도: 즉시 조치'를 근거로 정차·점검을 지어냈다(RAG 전후 비교 #7).
    # 진단 카드는 긴급도 초안을 '신호 정보'로 따로 받는다.
    evidence = []
    if ids:
        s = by_id[ids[0]]
        meta_text = "\n".join(l for l in get_meta_chunk(s.id).splitlines() if not l.startswith("긴급도"))
        evidence.append({"n": 1, "title": f"{s.name} (신호 정보)", "manual": s.source, "source": "",
                         "source_type": META_SOURCE, "signal_id": s.id, "text": meta_text})
    ordered = sorted(docs.values(), key=lambda x: x["source_type"] == WIKI_SOURCE)
    for doc in ordered:
        chunks = doc.pop("chunks")
        evidence.append({"n": len(evidence) + 1, **doc, "text": "\n".join(chunks[i] for i in sorted(chunks))})
    # 용어 뜻을 물으면("액추에이터가 뭐야?") 그 용어 문서를 쓴다 (신호에 연결해 둔 위키에 없으면 질문 때 받는다).
    # 찾으면 검색이 고른 신호에 붙은 다른 위키 문서는 뺀다 — '이모빌라이저' 문서만 있다고 용어 문서를 찾지 않았었다.
    if term:
        try:
            found = lookup_term(term)[:WIKI_K]
        except Exception as e:  # noqa: BLE001  위키 연결 실패: 용어 문서 없이 답한다
            log.warning("용어 문서를 받지 못함 (%s): %r", term, e)
            found = []
        if (found and not query.get("신호") and not state.get("selected_signal")
                and not _SIGNAL_WORDS.search(question)):
            # 신호를 말하지 않은 용어 질문: 검색이 억지로 고른 신호 문서는 빼고 용어 문서만 근거로 쓴다
            # (신호 문서 5건 뒤에 섞여 있을 때 LLM이 용어 문서를 두고도 '확인되지 않습니다'라고 답했다).
            # 질문에 경고등 이야기가 있으면 빼지 않는다 — "요소수가 뭐고 경고등이 뜨면?"에서 매뉴얼 대처가 빠졌었다.
            evidence = []
        elif found:
            evidence = [e for e in evidence if e["source_type"] != WIKI_SOURCE]  # 위키는 맨 뒤라 번호가 그대로 이어진다
        for d in found:
            evidence.append({"n": len(evidence) + 1, "title": d["title"], "manual": d["manual"], "source": d["source"],
                             "source_type": d["source_type"], "signal_id": "", "text": d["text"]})

    signals = [{"id": i, "name": by_id[i].name, "kind": by_id[i].kind, "color": by_id[i].color,
                "urgency": by_id[i].urgency} for i in ids]

    # 4) 내 차 관련 리콜. 리콜을 묻는 질문이면 답변 근거에도 넣는다(카드는 신호 근거만 쓴다).
    asks_recall = state.get("mode") != "card" and bool(_RECALL_WORDS.search(question))
    recalls = _find_recalls(state, query, by_id[ids[0]].category if ids else "", asks_recall)
    if asks_recall:
        if not query.get("신호") and not state.get("selected_signal"):
            evidence = []  # 신호 없이 리콜만 물으면 질문과 상관없이 검색된 신호의 매뉴얼은 빼고 리콜만 근거로 쓴다
        for r in recalls[:RECALL_CONTEXT_K]:
            evidence.append({"n": len(evidence) + 1, "title": r["title"], "manual": "공공데이터포털",
                             "source": r["source"], "source_type": r["source_type"], "signal_id": "",
                             "text": r["text"]})
    return {"signals": signals, "evidence": evidence, "recalls": recalls}


def _find_recalls(state: QAState, query: dict, category: str, asks_recall: bool) -> list[dict]:
    """차종은 화면 선택 > 질문에 직접 쓴 차명 (추출한 차명은 화면의 참고 매뉴얼 이름일 수 있어
    질문에 그 이름이 있을 때만 쓴다). 신호 장치 키워드가 리콜사유에 들어 있는 리콜만 찾는다."""
    model = state.get("model") or ""
    named = str(query.get("차명") or "")
    if not model and named and re.sub(r"\s", "", named) in re.sub(r"\s", "", state["question"]):
        model = named
    if not model:
        return []
    year = state.get("selected_year") or (int(query["연식"]) if str(query.get("연식") or "").isdigit() else None)
    powertrain = state.get("powertrain") or ""
    keywords = [k for k in query.get("키워드", [])
                if isinstance(k, str) and k.strip() not in _GENERIC_WORDS and not _RECALL_WORDS.search(k)]
    if asks_recall:
        # 신호를 말했으면 그 장치 키워드로, "내 차 리콜 있었어?"처럼 장치를 말하지 않거나
        # 맞는 게 없으면 그 차의 리콜을 최근 순으로
        if query.get("신호") or state.get("selected_signal"):
            keywords = signal_keywords(category) + keywords
        found = find_recalls(model, powertrain, year, keywords, RECALL_K) if keywords else []
        return found or find_recalls(model, powertrain, year, None, RECALL_K)
    if state.get("mode") == "card":
        keywords = []  # 카드는 고른 신호의 장치 키워드만 쓴다
    return find_recalls(model, powertrain, year, signal_keywords(category) + keywords, RECALL_K)


def _context(evidence: list[dict]) -> str:
    return "\n\n".join(
        f"[{e['n']}] | {e['source_type']} | {e['title']} ({e['manual']})\n{e['text']}" for e in evidence
    )


def generate(state: QAState) -> QAState:
    evidence = state.get("evidence", [])
    if state.get("mode") == "card":
        return {"card": _card(state, evidence)}
    if not evidence:
        return {"answer": f"{NOT_FOUND}. 질문을 바꾸거나 신호를 직접 골라 주세요."}
    reply = get_llm().invoke(ANSWER_PROMPT.format_messages(
        history=_history_text(state.get("history")), context=_context(evidence), question=state["question"],
        car=_car_text(state),
    )).content
    # 화면은 실제로 인용한 근거만 보여 준다.
    cited = sorted(n for n in _cited_numbers(reply) if 1 <= n <= len(evidence))
    return {"answer": reply, "cited": cited}


def _car_text(state: QAState) -> str:
    """프롬프트에 넣을 내 차 설명 (실제 차 + 연식, 근거 매뉴얼이 다른 차종일 수 있다는 안내)."""
    car = " ".join(str(x) for x in (state.get("vehicle") or state.get("selected_car"), state.get("selected_year")) if x)
    car = car or "(지정 안 함)"
    if state.get("vehicle") and state.get("selected_car"):
        car += f" (근거는 같은 동력 방식의 {state['selected_car']} 매뉴얼일 수 있음)"
    return car


def _card(state: QAState, evidence: list[dict]) -> dict:
    empty = {"urgency": "확인되지 않음", "meaning": NOT_FOUND, "do_now": [], "dont": [], "evidence": []}
    if not evidence:
        return empty
    s = (state.get("signals") or [{}])[0]
    signal = (f"{s['name']} (유형: {s['kind']}, 색상: {s['color']}, 긴급도 초안: {s['urgency']})"
              if s else state["question"])
    llm = get_llm().bind(response_format={"type": "json_object"})
    reply = llm.invoke(DIAGNOSIS_CARD_PROMPT.format_messages(context=_context(evidence), signal=signal,
                                                             car=_car_text(state))).content
    try:
        card = {**empty, **_json(reply)}
    except json.JSONDecodeError:
        return empty
    # 화면이 그대로 그릴 수 있게 형식을 맞춘다.
    if card["urgency"] not in URGENCY_LEVELS:
        card["urgency"] = "확인되지 않음"
    for key in ("do_now", "dont"):
        if not isinstance(card[key], list):
            card[key] = [str(card[key])]
        # 근거 번호가 없는 행동 지시는 근거 문서와 대조해 번호를 붙이고, 맞는 근거가 없으면 버린다.
        card[key] = [g for g in (_ground(item, evidence) for item in card[key]) if g]
    card["meaning"] = _ground(str(card["meaning"]), evidence) or card["meaning"]
    cited = _cited_numbers(card["meaning"], *card["do_now"], *card["dont"])
    card["evidence"] = sorted(n for n in cited | {n for n in card.get("evidence", []) if isinstance(n, int)}
                              if 1 <= n <= len(evidence))
    return card


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\w가-힣]", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _ground(item: str, evidence: list[dict], threshold: float = 0.7) -> str | None:
    """[n]이 있으면 그대로, 없으면 글자(2-gram)가 가장 많이 겹치는 근거 번호를 붙인다. 겹침이 낮으면 None."""
    if _CITE.search(item):
        return item
    grams = _bigrams(item)
    if not grams:
        return None
    best = max(evidence, key=lambda e: len(grams & _bigrams(e["text"])))
    if len(grams & _bigrams(best["text"])) / len(grams) < threshold:
        return None
    return f"{item.rstrip()} [{best['n']}]"


# ---------- 그래프 ----------
def build_graph():
    g = StateGraph(QAState)
    g.add_node("extract", extract)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", generate)
    g.add_node("chat", chat)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", route, {"retrieve": "retrieve", "chat": "chat"})
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    g.add_edge("chat", END)
    return g.compile()


_graph = None


def _inputs(question, history, selected_signal, selected_car, selected_year, mode, vehicle="",
            model="", powertrain="") -> QAState:
    return {
        "question": question, "history": history or [], "selected_signal": selected_signal,
        "selected_car": selected_car, "selected_year": selected_year, "mode": mode, "vehicle": vehicle,
        "model": model, "powertrain": powertrain,
    }


def ask(question: str, history: list[tuple[str, str]] | None = None, selected_signal: str = "",
        selected_car: str = "", selected_year: int | None = None, mode: str = "answer", vehicle: str = "",
        model: str = "", powertrain: str = "") -> QAState:
    global _graph
    _graph = _graph or build_graph()
    return _graph.invoke(_inputs(question, history, selected_signal, selected_car, selected_year, mode, vehicle,
                                 model, powertrain))


def stream_ask(question: str, history: list[tuple[str, str]] | None = None, selected_signal: str = "",
               selected_car: str = "", selected_year: int | None = None, mode: str = "answer", vehicle: str = "",
               model: str = "", powertrain: str = ""):
    """진행 상태 표시용. 노드가 끝날 때마다 (노드 이름, 지금까지 합친 상태)를 내보낸다."""
    global _graph
    _graph = _graph or build_graph()
    state = _inputs(question, history, selected_signal, selected_car, selected_year, mode, vehicle,
                    model, powertrain)
    for update in _graph.stream(state, stream_mode="updates"):
        for node, changes in update.items():
            state = {**state, **(changes or {})}
            yield node, state


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for q in ["엔진 경고등이 켜진 것과 깜빡이는 것은 다른가? 계속 달려도 되나?",
              "브레이크 경고등과 ABS 경고등이 동시에 켜지면 무엇이 문제인가?"]:
        r = ask(q)
        print(f"\n▶ {q}\n  검색어: {r['query']}\n  신호: {[s['name'] for s in r['signals']]}\n")
        print(r["answer"])
        for e in r["evidence"]:
            print(f"  [{e['n']}] {e['title']} ({e['manual']}, {e['source_type']})")
