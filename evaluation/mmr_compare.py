"""유사도 검색과 MMR 검색의 근거 목록 비교.
같은 질문·같은 검색어(프롬프트 1을 질문마다 한 번만 실행)로 retrieve 만 검색 방식을 바꿔 돌리고 근거 목록을 나란히 기록한다.
답변 생성(프롬프트 3)은 하지 않는다 — 검색 방식만의 차이를 보려는 것.

- 유사도: 질문과 가까운 순으로 TOP_K*4 청크 → 같은 문단 합치기 → 매뉴얼 문서 TOP_K건
- MMR: MMR_FETCH_K 청크 → 같은 문단 합치기 → MMR 순서(MMR_LAMBDA) → 매뉴얼 문서 TOP_K건
- 겹침: 근거 문서끼리 본문 글자 2-gram 자카드 유사도 (신호 메타데이터 [1]은 두 방식이 같아서 뺌)

실행: python -m evaluation.mmr_compare   (LLM 약 12회 = 질문마다 검색어 추출 1회, 임베딩 약 50회)
     python -m evaluation.mmr_compare --lambdas 0.3 0.5 0.7   (λ 값 여러 개)
결과: docs/MMR_비교_실행결과.md (자동 생성)
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from itertools import combinations
from pathlib import Path

from evaluation.rag_compare import _car_text
from src.config import MMR_FETCH_K, MMR_LAMBDA, ROOT, TOP_K
from src.rag import graph
from src.services.diagnosis import signal_name
from src.vehicles import describe, reference_manual

OUT = ROOT / "docs" / "MMR_비교_실행결과.md"
NEAR_DUP = 0.5   # 2-gram 자카드가 이 이상이면 '거의 같은 내용'으로 센다

# (질문, 고른 신호, 내 차(차종, 동력 방식, 연식) 또는 None, 모드). 질문이 ""이면 진단 카드 질문을 쓴다.
CASES = [
    # 대표 질문 (리콜만 묻는 ICCU 질문은 매뉴얼 근거를 쓰지 않아서 뺌)
    ("엔진 경고등이 켜진 것과 깜빡이는 것은 다른가? 계속 달려도 되나?", "", None, "answer"),
    ("브레이크 경고등과 ABS 경고등이 동시에 켜지면 무엇이 문제인가?", "", None, "answer"),
    ("TPMS 경고등이 1분간 깜빡이다가 켜지는 것과 바로 켜지는 것의 차이는?", "", None, "answer"),
    ("전기차 거북이 표시가 뜨면 무엇이 제한되나?", "", None, "answer"),
    ("연료 캡을 덜 잠갔을 때도 엔진 경고등이 켜질 수 있나?", "", None, "answer"),
    ("'변속기 과열! 시동을 켠 채로 정차하십시오'가 떴는데 시동을 끄는 게 낫지 않나?", "", ("투싼", "휘발유", None), "answer"),
    ("핸들 그림이 빨갛게 뜨면서 경고음이 울리면 차를 세우고 정비소에 가야 해?", "", None, "answer"),
    ("와이퍼 고무는 얼마마다 갈아야 해?", "", None, "answer"),
    # 원리 질문 (위키가 섞이는 경우)
    ("요소수가 뭐고 경고등이 뜨면 어떻게 해야 해?", "", ("팰리세이드", "디젤", 2024), "answer"),
    # 진단 카드 (고른 신호 하나의 문서만 검색)
    ("", "SIG-017", ("팰리세이드", "디젤", 2024), "card"),
    ("", "SIG-011", ("아이오닉 5", "전기차", 2024), "card"),
    ("", "SIG-016", None, "card"),
]


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"\W", "", text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def _state(question: str, signal: str, car: tuple | None, mode: str) -> dict:
    if not question:
        question = f"{signal_name(signal)}이(가) 떴어요. 무슨 뜻이고 무엇을 해야 하나요?"
    kw = {}
    if car:
        model, powertrain, year = car
        kw = {"selected_car": reference_manual(powertrain, model), "selected_year": year,
              "vehicle": describe(model, powertrain), "model": model, "powertrain": powertrain}
    return graph._inputs(question, [], signal, kw.get("selected_car", ""), kw.get("selected_year"), mode,
                         kw.get("vehicle", ""), kw.get("model", ""), kw.get("powertrain", ""))


def run(state: dict, search_type: str, lam: float) -> list[dict]:
    """retrieve 만 실행. graph 는 import 때 읽은 설정값을 쓰므로 그 자리만 바꾼다."""
    graph.SEARCH_TYPE, graph.MMR_LAMBDA = search_type, lam
    ev = graph.retrieve(state)["evidence"]
    return [e for e in ev if e["source_type"] not in (graph.META_SOURCE, "공공 리콜")]


def stats(ev: list[dict], car: str) -> dict:
    sims = [_jaccard(a["text"], b["text"]) for a, b in combinations(ev, 2)]
    return {
        "n": len(ev),
        "titles": len({e["title"] for e in ev}),
        "mean": sum(sims) / len(sims) if sims else 0.0,
        "near": sum(s >= NEAR_DUP for s in sims),
        "car": sum(car in re.sub(r"\s", "", e["manual"]) for e in ev) if car else None,
    }


def _key(e: dict) -> tuple[str, str]:
    return e["title"], e["manual"]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lambdas", type=float, nargs="+", default=[MMR_LAMBDA], help="비교할 MMR_LAMBDA 값")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    methods = [("유사도", "similarity", MMR_LAMBDA)] + [(f"MMR λ={lam}", "mmr", lam) for lam in args.lambdas]

    head = " | ".join(f"{name}: 문서/제목 종류/평균 겹침/거의 같은 쌍" for name, _, _ in methods[1:])
    summary = [f"| # | 질문 | 유사도: 문서/제목 종류/평균 겹침/거의 같은 쌍 | {head} | 유사도와 같은 문서 | 내 차 매뉴얼 근거 |",
               "|---|---|---|" + "---|" * len(methods[1:]) + "---|---|"]
    detail, totals = [], {name: {"near": 0, "titles": 0, "mean": 0.0} for name, _, _ in methods}
    for i, case in enumerate(CASES, 1):
        state = _state(*case)
        state["query"] = graph.extract(state)["query"]   # 두 방식이 같은 검색어를 쓰도록 한 번만
        car = re.sub(r"\s", "", state["selected_car"])
        results = {}
        for name, st, lam in methods:
            ev = run(state, st, lam)
            results[name] = (ev, stats(ev, car))
            t = totals[name]
            t["near"] += results[name][1]["near"]
            t["titles"] += results[name][1]["titles"]
            t["mean"] += results[name][1]["mean"] / len(CASES)

        base_keys = {_key(e) for e in results["유사도"][0]}
        cells = [f"{s['n']} / {s['titles']} / {s['mean']:.2f} / {s['near']}" for _, s in results.values()]
        same = " · ".join(f"{len(base_keys & {_key(e) for e in ev})}/{len(base_keys)}"
                          for name, (ev, _) in results.items() if name != "유사도")
        cars = " · ".join(str(s["car"]) for _, s in results.values()) if car else "-"
        label = case[0] or f"[진단 카드] {signal_name(case[1])}"
        summary.append(f"| {i} | {label} | " + " | ".join(cells) + f" | {same} | {cars} |")

        detail += [f"## {i}. {label}", "",
                   f"- 내 차: {_car_text(case[2]) or '지정 안 함'} (참고 매뉴얼: {state['selected_car'] or '없음'})",
                   f"- 검색어: 신호 '{state['query'].get('신호', '')}', 키워드 {state['query'].get('키워드', [])}", ""]
        for name, (ev, s) in results.items():
            detail += [f"### {name}", ""]
            detail += [f"{k}. {'🆕 ' if name != '유사도' and _key(e) not in base_keys else ''}"
                       f"{e['source_type']} · {e['title']} ({e['manual']})  \n"
                       f"   > {re.sub(r'\s+', ' ', e['text'])[:160]}…" for k, e in enumerate(ev, 1)] or ["(근거 없음)"]
            detail.append("")
        print(f"[{i}/{len(CASES)}] {label}  " + " | ".join(f"{n}: {c}" for n, c in zip(results, cells)))

    total_rows = ["| 방식 | 거의 같은 쌍 합계 | 제목 종류 합계 | 평균 겹침 |", "|---|---|---|---|"]
    total_rows += [f"| {name} | {t['near']} | {t['titles']} | {t['mean']:.2f} |" for name, t in totals.items()]
    out = ["# 유사도 검색과 MMR 검색의 근거 목록 비교 (자동 생성)", "",
           f"`python -m evaluation.mmr_compare --lambdas {' '.join(map(str, args.lambdas))}` · {datetime.now():%Y-%m-%d %H:%M}"
           f" · TOP_K {TOP_K}, MMR_FETCH_K {MMR_FETCH_K}", "",
           "- 같은 질문·같은 검색어로 retrieve 만 검색 방식을 바꿔 실행 (답변 생성 없음)",
           "- 근거 문서 = 신호 메타데이터 [1]과 리콜을 뺀 매뉴얼·백과사전 문서",
           f"- 평균 겹침 = 근거 문서끼리 본문 글자 2-gram 자카드 평균, 거의 같은 쌍 = 자카드 {NEAR_DUP} 이상인 쌍 수",
           "- 🆕 = 유사도 검색 목록에 없던 문서", "",
           "## 합계", ""] + total_rows + ["", "## 질문별", ""] + summary + [""] + detail
    Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
