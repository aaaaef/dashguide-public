"""RAG 사용 전후 답변 비교.
같은 질문을 같은 모델(.env LLM_MODEL, temperature 0)에 RAG 없이 한 번, RAG(근거 검색 + 프롬프트 3)로 한 번 묻고 나란히 기록한다.
RAG 없는 쪽도 같은 '자동차 정비 안내 전문가' 역할을 주고, 근거 문서만 주지 않는다.

실행: python -m evaluation.rag_compare   (LLM 약 27회)
결과: docs/RAG_전후_비교_실행결과.md (자동 생성). 사실 정확성 판정은 docs/RAG_전후_비교.md 에 사람이 정리한다.

프롬프트를 고친 뒤 RAG 답만 다시 볼 때 (LLM 답이 실행마다 흔들리므로 여러 번):
  python -m evaluation.rag_compare --rag-only --runs 2 --out docs/RAG_프롬프트개선_실행결과.md
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
import sys
import time
from datetime import datetime

from langchain_core.prompts import ChatPromptTemplate

from src.config import LLM_MODEL, ROOT
from src.llm import get_llm
from src.rag.graph import ask
from src.rag.prompts import NOT_FOUND_RE
from src.vehicles import describe, reference_manual

OUT = ROOT / "docs" / "RAG_전후_비교_실행결과.md"

# (질문, 내 차(차종, 동력 방식, 연식) 또는 None, 고른 이유)
QUESTIONS = [
    ("엔진 경고등이 켜진 것과 깜빡이는 것은 다른가? 계속 달려도 되나?", None,
     "일반 상식(깜빡임 = 실화)과 매뉴얼 내용이 다를 수 있음"),
    ("브레이크 경고등과 ABS 경고등이 동시에 켜지면 무엇이 문제인가?", None, "두 경고등의 조합 의미는 매뉴얼에 명시"),
    ("TPMS 경고등이 1분간 깜빡이다가 켜지는 것과 바로 켜지는 것의 차이는?", None, "점등 방식에 따라 원인과 대처가 다름"),
    ("전기차 거북이 표시가 뜨면 무엇이 제한되나?", None, "'거북이'는 일상어. 매뉴얼 이름(파워다운 표시등)과 조건을 알아야 함"),
    ("연료 캡을 덜 잠갔을 때도 엔진 경고등이 켜질 수 있나?", None, "매뉴얼에 명시된 사례. 고장으로 오인하기 쉬움"),
    ("'변속기 과열! 시동을 켠 채로 정차하십시오'가 떴는데 시동을 끄는 게 낫지 않나?", ("투싼", "휘발유", None),
     "상식(과열이면 끈다)과 매뉴얼 지시(시동을 켠 채로 정차)가 반대"),
    ("핸들 그림이 빨갛게 뜨면서 경고음이 울리면 차를 세우고 정비소에 가야 해?", None,
     "핸즈오프 경고는 고장이 아니라 핸들을 잡으라는 경고. 고장으로 답하면 위험한 오안내"),
    ("내 차에 ICCU 관련 리콜이 있었어?", ("아이오닉 5", "전기차", 2022), "리콜 이력은 공공데이터 근거 없이 답할 수 없음"),
    ("와이퍼 고무는 얼마마다 갈아야 해?", None, "수집한 근거 문서에 없는 질문 — 모르는 것을 모른다고 하는지"),
]

NO_RAG_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "당신은 자동차 정비 안내 전문가입니다. 운전자의 질문에 한국어로 답합니다."),
    ("human", "내 차: {car}\n질문: {question}"),
])

_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def no_rag(question: str, car: str) -> str:
    return get_llm().invoke(NO_RAG_PROMPT.format_messages(car=car or "(지정 안 함)", question=question)).content


def with_rag(question: str, car: tuple | None) -> dict:
    if not car:
        return ask(question)
    model, powertrain, year = car
    return ask(question, selected_car=reference_manual(powertrain, model), selected_year=year,
               vehicle=describe(model, powertrain), model=model, powertrain=powertrain)


def _car_text(car: tuple | None) -> str:
    return describe(car[0], car[1]) + (f" {car[2]}" if car[2] else "") if car else ""


def _cited_block(r: dict) -> str:
    cited = [e for e in r["evidence"] if e["n"] in r.get("cited", [])]
    return "\n".join(f"- [{e['n']}] {e['source_type']} · {e['title']} ({e['manual']})\n  > "
                     + re.sub(r"\s+", " ", e["text"])[:400] for e in cited) or "- (인용 근거 없음)"


def rag_only(runs: int, path: Path):
    """프롬프트 개선 확인용: RAG 답만 질문마다 runs 번."""
    out = ["# RAG 답변 반복 실행 (자동 생성)", "",
           f"`python -m evaluation.rag_compare --rag-only --runs {runs}` · {datetime.now():%Y-%m-%d %H:%M} · 모델 {LLM_MODEL}", ""]
    for i, (q, car, why) in enumerate(QUESTIONS, 1):
        out += [f"## {i}. {q}", "", f"- 고른 이유: {why}", f"- 내 차: {_car_text(car) or '지정 안 함'}", ""]
        for k in range(1, runs + 1):
            r = with_rag(q, car)
            out += [f"### 실행 {k}", "", r["answer"].strip(), "", "**인용 근거**", "", _cited_block(r), ""]
            print(f"[{i}/{len(QUESTIONS)} · {k}] {q}  (인용 {len(r.get('cited', []))})")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"저장: {path}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--rag-only", action="store_true", help="RAG 답만 (프롬프트 개선 전후 비교용)")
    ap.add_argument("--runs", type=int, default=1, help="--rag-only 에서 질문마다 실행할 횟수")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    if args.rag_only:
        return rag_only(args.runs, Path(args.out))
    out = ["# RAG 사용 전후 답변 (자동 생성)", "",
           f"`python -m evaluation.rag_compare` 실행 결과 · {datetime.now():%Y-%m-%d %H:%M} · 모델 {LLM_MODEL} (temperature 0)", "",
           "- **RAG 없이**: 시스템 프롬프트 '자동차 정비 안내 전문가 … 한국어로 답합니다' + 질문(+ 내 차)",
           "- **RAG**: 서비스와 같은 LangGraph(검색어 추출 → 근거 검색 → 프롬프트 3). 아래 근거는 답변이 실제 인용한 것만",
           "", "| # | 질문 | RAG 없이: 근거 번호 | RAG: 인용 근거 | RAG: '확인되지 않음' 표시 | 시간(없이/RAG) |", "|---|---|---|---|---|---|"]
    detail = []
    for i, (q, car, why) in enumerate(QUESTIONS, 1):
        car_text = _car_text(car)
        t = time.time()
        base = no_rag(q, car_text)
        t_base = time.time() - t
        t = time.time()
        r = with_rag(q, car)
        t_rag = time.time() - t
        cited = [e for e in r["evidence"] if e["n"] in r.get("cited", [])]
        out.append(f"| {i} | {q} | {'있음' if _CITE.search(base) else '없음'} | "
                   f"{len(cited)}건 ({', '.join(sorted({e['source_type'] for e in cited})) or '-'}) | "
                   f"{'예' if NOT_FOUND_RE.search(r['answer']) else '아니오'} | {t_base:.1f}초 / {t_rag:.1f}초 |")
        ev = _cited_block(r)
        detail += [f"## {i}. {q}", "", f"- 고른 이유: {why}", f"- 내 차: {car_text or '지정 안 함'}", "",
                   "### RAG 없이", "", base.strip(), "", "### RAG", "", r["answer"].strip(), "", "**인용 근거**", "", ev, ""]
        print(f"[{i}/{len(QUESTIONS)}] {q}  ({t_base:.1f}s / {t_rag:.1f}s, 인용 {len(cited)})")
    OUT.write_text("\n".join(out + [""] + detail) + "\n", encoding="utf-8")
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
