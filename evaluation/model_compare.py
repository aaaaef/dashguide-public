"""모델별 답변·토큰·응답 시간 비교. 서비스 기본 모델(.env LLM_MODEL)은 바꾸지 않는다.
와 같은 9문항을 서비스와 같은 LangGraph(검색어 추출 → 근거 검색 → 프롬프트 3)로 묻고 LLM 만 바꾼다.
후보 선별(프롬프트 2)은 색인 때 캐시된 결과를 쓰므로 모든 모델이 같은 근거 후보에서 시작한다.
temperature 0 을 받지 않는 모델(NO_TEMPERATURE)은 기본 temperature 로 부른다 → 조건이 완전히 같지는 않음.

실행: python -m evaluation.model_compare --runs 2   (모델당 LLM 약 36회)
시험: python -m evaluation.model_compare --questions 1 --out <경로>.md
결과: docs/모델_비교_실행결과.md + 같은 이름 .json (자동 생성). 사실 정확성 판정은 사람이 따로 정리한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from evaluation.rag_compare import QUESTIONS, _car_text, _cited_block, with_rag
from src.config import OPENAI_API_KEY, OPENAI_BASE_URL, ROOT
from src.rag import graph
from src.rag.prompts import NOT_FOUND_RE

OUT = ROOT / "docs" / "모델_비교_실행결과.md"
MODELS = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-5.4-mini", "gpt-5.4", "gpt-6-luna"]  # gpt-5.5 는 luna 와 겹치고 비용이 커서 뺌
NO_TEMPERATURE = {"gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5.5", "gpt-6-luna"}  # 2026-10-23 직접 호출로 확인


class Usage(BaseCallbackHandler):
    """LLM 호출마다 토큰을 더한다. 임베딩은 모든 모델이 같으므로 세지 않는다."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls, self.input, self.output, self.reasoning, self.served = 0, 0, 0, 0, set()

    def on_llm_end(self, response, **kwargs):
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                u = getattr(msg, "usage_metadata", None) or {}
                self.calls += 1
                self.input += u.get("input_tokens", 0)
                self.output += u.get("output_tokens", 0)
                self.reasoning += (u.get("output_token_details") or {}).get("reasoning", 0)
                if msg is not None and msg.response_metadata.get("model_name"):
                    self.served.add(msg.response_metadata["model_name"])


def use_model(model: str, usage: Usage):
    """graph 노드는 호출 때마다 get_llm() 을 부르므로 그 자리만 바꿔 끼운다."""
    kw = {} if model in NO_TEMPERATURE else {"temperature": 0}
    llm = ChatOpenAI(model=model, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, callbacks=[usage], **kw)
    graph.get_llm = lambda temperature=0.0: llm


def run(models: list[str], runs: int, qnums: list[int]) -> list[dict]:
    rows, usage = [], Usage()
    for model in models:
        use_model(model, usage)
        for i in qnums:
            q, car, _ = QUESTIONS[i - 1]
            for k in range(1, runs + 1):
                usage.reset()
                t = time.time()
                try:
                    r, err = with_rag(q, car), ""
                except Exception as e:  # 한 모델이 실패해도 나머지는 계속
                    r, err = None, f"{type(e).__name__}: {str(e)[:200]}"
                row = {"model": model, "q": i, "run": k, "sec": round(time.time() - t, 1),
                       "calls": usage.calls, "input": usage.input, "output": usage.output,
                       "reasoning": usage.reasoning, "served": sorted(usage.served), "error": err,
                       "answer": r["answer"].strip() if r else "", "cited": len(r.get("cited", [])) if r else 0,
                       "not_found": bool(r) and bool(NOT_FOUND_RE.search(r["answer"])), "evidence": _cited_block(r) if r else ""}
                rows.append(row)
                print(f"[{model} · {i} · {k}] {row['sec']}s 호출 {row['calls']} 입력 {row['input']} "
                      f"출력 {row['output']}(추론 {row['reasoning']}) 인용 {row['cited']} {err}")
                time.sleep(1)  # 호출 간격
    return rows


def _avg(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0


def report(rows: list[dict], models: list[str], runs: int, qnums: list[int], path: Path):
    out = ["# 모델 비교 실행 결과 (자동 생성)", "",
           f"`python -m evaluation.model_compare --runs {runs}` · {datetime.now():%Y-%m-%d %H:%M} · "
           f"문항 {len(qnums)}개 × {runs}회", "",
           "- 서비스와 같은 LangGraph로 묻고 LLM 만 바꿈. 토큰은 검색어 추출 + 답변 생성 합계 (임베딩 제외)",
           f"- temperature 0: {', '.join(m for m in models if m not in NO_TEMPERATURE)} / "
           f"기본값(0 거부): {', '.join(m for m in models if m in NO_TEMPERATURE) or '-'}", "",
           "| 모델 | 실제 응답 모델 | 오류 | 평균 시간(초) | 최대 시간 | 평균 입력 토큰 | 평균 출력 토큰 | 그중 추론 | 평균 인용 | '확인되지 않음' |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        ok = [r for r in rs if not r["error"]]
        served = sorted({s for r in rs for s in r["served"]})
        out.append(f"| {m} | {', '.join(served) or '-'} | {len(rs) - len(ok)}/{len(rs)} | "
                   f"{_avg([r['sec'] for r in ok]):.1f} | {max([r['sec'] for r in ok], default=0):.1f} | "
                   f"{_avg([r['input'] for r in ok]):,.0f} | {_avg([r['output'] for r in ok]):,.0f} | "
                   f"{_avg([r['reasoning'] for r in ok]):,.0f} | {_avg([r['cited'] for r in ok]):.1f} | "
                   f"{sum(r['not_found'] for r in ok)} |")
    for i in qnums:
        q, car, why = QUESTIONS[i - 1]
        out += ["", f"## {i}. {q}", "", f"- 고른 이유: {why}", f"- 내 차: {_car_text(car) or '지정 안 함'}"]
        for r in (r for r in rows if r["q"] == i):
            out += ["", f"### {r['model']} · 실행 {r['run']} ({r['sec']}초, 입력 {r['input']:,} / 출력 {r['output']:,})", ""]
            out += [f"**오류**: {r['error']}"] if r["error"] else [r["answer"], "", "**인용 근거**", "", r["evidence"]]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {path} (+ .json)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--runs", type=int, default=2, help="질문마다 실행할 횟수")
    ap.add_argument("--questions", type=int, nargs="+", default=list(range(1, len(QUESTIONS) + 1)),
                    help="문항 번호 (1부터)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    rows = run(args.models, args.runs, args.questions)
    report(rows, args.models, args.runs, args.questions, Path(args.out))


if __name__ == "__main__":
    main()
