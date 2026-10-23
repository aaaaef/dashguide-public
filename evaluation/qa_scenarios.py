"""질문하기·진단 카드 시나리오를 실제 LLM으로 돌려 사람이 읽고 판정한다 (자동 채점 없음).
도메인 문서의 '근거가 필요한 질문' + 근거 없음 + 진단 카드 + 이어지는 질문.
실행: python -m evaluation.qa_scenarios  (LLM 호출 약 30회)

판정할 때 볼 것:
- 근거에 없는 단정이 없는가 (예: 엔진 경고등 켜짐/깜빡임 차이 → "확인되지 않음"이어야 함)
- 카드 긴급도: TPMS 주의(또는 빠른 점검), 변속기 과열 즉시 정차, 후측방 충돌·핸즈오프 즉시 조치
- 매뉴얼에 없는 질문(와이퍼)은 "근거 문서에서 확인되지 않습니다"
- 인사("고마워")는 근거 검색 없이 짧은 대화(chat), 범위 밖("페이커")은 정해 둔 안내, 용어("액추에이터")는 위키 문서 인용
"""
import json
import sys

from src.rag.graph import ask

QUESTIONS = [
    "엔진 경고등이 켜진 것과 깜빡이는 것은 다른가? 계속 달려도 되나?",
    "TPMS 경고등이 1분간 깜빡이다가 켜지는 것과 바로 켜지는 것의 차이는?",
    "전기차 거북이 표시가 뜨면 무엇이 제한되나?",
    "연료 캡을 덜 잠갔을 때도 엔진 경고등이 켜질 수 있나?",
    "와이퍼 고무는 얼마마다 갈아야 해?",
    "고마워",
    "페이커가 누구야?",
    "액추에이터가 뭐야?",
]
CARDS = [
    ("주황색인데 타이어 안에 느낌표 있는 거", ""),
    ("변속기 과열! 시동을 켠 채로 정차하십시오 라고 떴어", "투싼"),
    ("후측방 충돌 경고가 떴어", ""),
    ("핸들 그림이 빨갛게 뜨면서 경고가 나와", ""),
]


def show(r: dict, label: str):
    print(f"\n{'=' * 70}\n▶ {label}{'  (근거 검색 없이 답함)' if r.get('chat') else ''}\n"
          f"  검색어: {json.dumps(r.get('query'), ensure_ascii=False)}")
    print(f"  신호: {[s['name'] for s in r.get('signals', [])]}")
    if "card" in r:
        print("  카드:", json.dumps(r["card"], ensure_ascii=False, indent=1))
    else:
        print(f"\n{r['answer']}\n  인용: {r.get('cited')}")
    for e in r.get("evidence", []):
        print(f"  [{e['n']}] {e['title'][:30]} ({e['manual']}, {e['signal_id']})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for q in QUESTIONS:
        show(ask(q), q)
    for q, car in CARDS:
        show(ask(q, mode="card", selected_car=car), f"진단 카드: {q} ({car or '차종 미지정'})")
    first = "빨간 브레이크 경고등이 켜졌어"
    r1 = ask(first)
    show(r1, first)
    show(ask("그럼 브레이크액은 어떻게 보충해?", history=[("user", first), ("assistant", r1["answer"])]),
         "이어지는 질문: 브레이크액 보충")
