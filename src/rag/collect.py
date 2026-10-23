"""RAG 2: 신호별 외부 문서 수집 → 후보 선별 → 색인.
- 후보 검색: 매뉴얼 '제공 문서 묶음'에서 신호 이름으로 찾는다. 위키(보조 출처)는 신호의 '관련 위키 문서'를
  그대로 붙인다 (data_prep/wiki.py 로 먼저 수집). 리콜은 내 차에 따라 달라 색인하지 않고 질의 때 찾는다.
- 후보 선별: 프롬프트 2가 제목·요약만 보고 고른다. 신호 자체 문서(제목 일치)는 항상 포함.
- 색인: src/vectorstore/signal_docs.py
선별 결과는 data/raw 에 캐시해 같은 신호로 LLM을 다시 부르지 않는다.
"""
from __future__ import annotations

import json
import re

from src.config import RAW_DIR
from src.data_prep import manuals, wiki
from src.data_prep.signals import Signal, load_signals
from src.llm import get_llm
from src.rag.prompts import CANDIDATE_SELECT_PROMPT

SELECTIONS = RAW_DIR / "doc_selections.json"
CANDIDATES_PER_SIGNAL = 8
MAX_SELECTED = 4


# ---------- 후보 검색 ----------
def find_candidates(signal: Signal, k: int = CANDIDATES_PER_SIGNAL) -> tuple[list[dict], list[str]]:
    """(후보 문서, 필수 문서 ID).
    같은 문단이 매뉴얼·페이지마다 반복되므로 본문이 같은 문서는 하나만 남긴다.
    필수 = 제목이 신호 이름과 일치하는 문서 → 없으면 본문에 그 문구가 있는 첫 문서
    → 그래도 없으면 제목이 신호 이름의 일부인 문서 (앞 단계보다 느슨해서 마지막에 본다:
    '변속기의 온도가 높아 파워가 제한됩니다'에 EV의 '파워가 제한됩니다'가 걸리는 것을 막기 위해)."""
    name = re.sub(r"\(.*?\)", "", signal.name).strip() or signal.name  # 괄호 속 보충 설명은 매뉴얼 제목에 없다
    candidates, seen = [], set()
    for d in manuals.search(name, k=k * 4):
        key = manuals._norm(d["text"])[:300]
        if key not in seen:
            seen.add(key)
            candidates.append(d)
        if len(candidates) == k:
            break

    tiers = {d["doc_id"]: manuals.match_tier(d, name) for d in candidates}
    title_core = manuals._norm(re.split(r"[(.!]", signal.name)[0])
    required = [i for i, t in tiers.items() if t <= 1]
    if not required:
        required = [i for i, t in tiers.items() if t <= 3][:1]
    if not required:  # 매뉴얼 제목이 더 짧은 경우 (예: '운전자 휴식 권유' → '휴식 권유')
        required = [d["doc_id"] for d in candidates
                    if manuals._norm(d["title"]) and manuals._norm(d["title"]) in title_core]
    return candidates, required


# ---------- 후보 선별 ----------
def _summary(doc: dict) -> str:
    return re.sub(r"\s+", " ", doc["text"])[:80]


def select_candidates(question: str, query: dict, candidates: list[dict], required: list[str],
                      max_docs: int = MAX_SELECTED) -> list[str]:
    lines = [f"{d['doc_id']} | {d['source_type']}({d.get('manual', '')}) | {d['title']} | {_summary(d)}"
             for d in candidates]
    llm = get_llm().bind(response_format={"type": "json_object"})
    reply = llm.invoke(CANDIDATE_SELECT_PROMPT.format_messages(
        question=question, query_json=json.dumps(query, ensure_ascii=False),
        required_ids=", ".join(required) or "(없음)", candidates="\n".join(lines), max_docs=max_docs,
    )).content
    try:
        picked = json.loads(reply).get("selected", [])
    except (json.JSONDecodeError, AttributeError):
        picked = []
    valid = {d["doc_id"] for d in candidates}
    # LLM이 빠뜨려도 필수 문서는 넣고, 후보에 없는 ID(지어낸 ID)는 버린다.
    selected = list(dict.fromkeys(required + [p for p in picked if p in valid]))
    return selected[:max(max_docs, len(required))]


# ---------- 신호 단위 실행 ----------
def _load_selections() -> dict:
    return json.loads(SELECTIONS.read_text(encoding="utf-8")) if SELECTIONS.exists() else {}


def collect_for_signal(signal: Signal, selections: dict, force: bool = False) -> tuple[list[dict], bool]:
    """신호 하나의 선별 문서와 '선별을 새로 했는지'를 돌려준다.
    후보·필수 문서가 캐시와 같으면 LLM을 다시 부르지 않는다 (검색 로직이 바뀌면 자동으로 다시 고른다)."""
    by_id = manuals._index()[1]
    candidates, required = find_candidates(signal)
    cand_ids = [d["doc_id"] for d in candidates]
    cached = selections.get(signal.id)
    if cached and not force and cached["candidates"] == cand_ids and cached["required"] == required:
        return [by_id[i] for i in cached["selected"] if i in by_id], False

    question = f"{signal.name}이(가) 무슨 뜻이고, 켜지면 무엇을 해야 하나요?"
    query = {"신호": signal.name, "차명": "", "연식": None, "키워드": signal.wiki}
    selected = select_candidates(question, query, candidates, required)
    selections[signal.id] = {"candidates": cand_ids, "required": required, "selected": selected}
    SELECTIONS.write_text(json.dumps(selections, ensure_ascii=False, indent=1), encoding="utf-8")
    return [by_id[i] for i in selected], True


def twin_docs(docs: list[dict]) -> list[dict]:
    """docs와 본문이 같은 다른 매뉴얼의 문서. 후보 단계에서는 같은 문단을 하나만 남기지만,
    검색 때 '내 차 매뉴얼' 표기로 근거를 보여 주려면 매뉴얼마다 있어야 한다
    (예: DPF 경고등 문단은 투싼·팰리세이드 매뉴얼에 글자까지 같다)."""
    groups: dict[str, list[dict]] = {}
    for d in manuals._index()[1].values():
        groups.setdefault(manuals._norm(d["text"])[:300], []).append(d)
    have = {d["doc_id"] for d in docs}
    twins = []
    for d in docs:
        for t in groups.get(manuals._norm(d["text"])[:300], []):
            if t["doc_id"] not in have and t["manual"] != d["manual"]:
                have.add(t["doc_id"])
                twins.append(t)
    return twins


def build_all(force: bool = False) -> dict[str, int]:
    """신호 100건 전체의 문서를 선별·색인한다. 색인할 문서 목록이 색인된 것과 다른 신호만 임베딩한다.
    반환: 새로 색인한 신호 ID → 청크 수."""
    from src.vectorstore.signal_docs import get_docs_store, index_signal_docs

    store, selections, indexed = get_docs_store(), _load_selections(), {}
    by_id = manuals._index()[1]
    for s in load_signals():
        docs, changed = collect_for_signal(s, selections, force=force)
        # 신호 자체 문서(필수 문서)는 같은 문단이 있는 다른 매뉴얼 문서도 함께 색인한다
        docs += twin_docs([by_id[i] for i in selections[s.id]["required"] if i in by_id])
        # 위키(보조 출처)는 신호에 적힌 용어로 이미 정해져 있어 선별 없이 붙인다
        docs += wiki.docs_for_signal(s.wiki)
        expected = {d["doc_id"] for d in docs if d["text"].strip()}
        stored = {m["doc_id"] for m in store._collection.get(where={"signal_id": s.id}, include=["metadatas"])["metadatas"]}
        if changed or expected != stored:
            indexed[s.id] = index_signal_docs(s.id, docs, store)
    return indexed


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from src.vectorstore.signal_docs import get_docs_store

    indexed = build_all()
    print(f"새로 색인한 신호: {len(indexed)}건 {sorted(indexed) if len(indexed) <= 20 else ''}")
    col = get_docs_store()._collection
    metas = col.get(include=["metadatas"])["metadatas"]
    covered = {m["signal_id"] for m in metas}
    missing = [s.id for s in load_signals() if s.id not in covered]
    print(f"signal_docs 전체 청크: {col.count()}, 문서가 있는 신호: {len(covered)}/100, 없는 신호: {missing or '없음'}")
