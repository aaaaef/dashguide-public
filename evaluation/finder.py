"""경고등 찾기 평가: 화면에 실제로 보이는 주요 후보 기준 Top-1 / Top-3 / 오답 노출 수.
실행: python -m evaluation.finder

- TUNED: 검색 규칙(흔한 낱말·일상어 사전·가중치)을 조정할 때 실패 사례를 본 질의. 실제보다 좋게 나올 수 있다.
- HOLDOUT: 조정에 쓰지 않은 질의. 결과를 보기 전에 정했다. 실제 성능은 이쪽으로 판단하고, 여기 실패에 맞춰 규칙을 고치지 않는다.
- 상관없는 설명("페이커")도 같은 방식으로 나눈다: OFF_TOPIC_TUNED·NO_KEYWORD 로 기준값을 정했고, OFF_TOPIC_HOLDOUT 은 확인만 한다.
  글자만 겹치는 말("후드티")은 FALSE_FRIEND_TUNED 로 입력 검사(프롬프트 4)를 만들고 FALSE_FRIEND_HOLDOUT 으로 확인한다.
  (OFF_TOPIC_HOLDOUT 은 첫 규칙에서 '롤 티어' 실패를 이미 본 뒤라 완전한 미사용 질의는 아니다)
- 입력 검사 때문에 LLM 을 약 70회 부른다 (검색 점수가 애매한 질의만 결과를 쓰지만 검색과 동시에 부른다).
"""
import sys

from src.services.finder import find_signals, find_signals_split


def S(*numbers: int) -> set[str]:
    return {f"SIG-{n:03d}" for n in numbers}


TUNED = [
    # 데이터의 모양 표현을 보고 만든 질의
    ("주황색인데 타이어 안에 느낌표 있는 거", S(19, 93, 72)),
    ("거북이 표시", S(4)),
    ("거북이 모양 경고등", S(4)),
    ("빨간색 원 안에 느낌표", S(11)),
    ("커피잔 모양", S(32, 81)),
    ("주전자 모양 빨간불", S(16)),
    ("렌치 모양", S(2)),
    ("눈 결정 표시", S(47)),
    ("노란 엔진 모양", S(17)),
    ("차가 미끄러지는 그림", S(36, 37)),
    ("ABS 불 들어옴", S(12, 13)),
    ("핸들에 느낌표", S(14)),
    ("변속기가 너무 뜨겁다고 나와", S(61, 62)),
    ("기름이 얼마 안 남았대", S(15)),
    ("디젤차 매연 필터", S(25)),
    # 데이터 표현과 일부러 다르게 쓴 질의
    ("노란불에 스패너 그림", S(2)),
    ("빨간 사람이 벨트 맨 그림", S(8)),
    ("초록색 화살표 두 개 깜빡여", S(40)),
    ("파란 전조등 표시", S(42)),
    ("열쇠 그림 노란색", S(39)),
    ("계기판에 빨간 배터리 그림", S(7, 18)),
    ("눈송이 모양", S(47)),
    ("동그라미 안에 P랑 느낌표", S(20)),
]

HOLDOUT = [
    ("빨간 기름통에서 물 떨어지는 그림", S(16)),
    ("노란 삼각형 안에 느낌표", S(33)),
    ("에어백 불 들어와", S(9)),
    ("초록색 READY 글자", S(1)),
    ("자동차 옆에 구불구불한 선 노란색", S(36, 37)),
    ("노란 코일 같은 거 디젤", S(27)),
    ("주유기 그림 노란불", S(15)),
    ("빨간색 배터리 플러스 마이너스", S(7, 18)),
    ("빨간 차 그림에 문 열린 표시", S(46, 70)),
    ("요소수 경고", S(26)),
    ("핸들 잡으라고 빨갛게 뜸", S(80)),
    ("앞차랑 부딪힐 것 같은 그림", S(28, 83)),
]


# 계기판과 상관없는 설명. 경고등 찾기가 후보 대신 안내를 보여야 한다.
OFF_TOPIC_TUNED = ["페이커", "오늘 날씨 어때", "라면 맛있게 끓이는 법", "아이유 노래 추천", "삼성전자 주가", "ㅋㅋㅋㅋ", "asdfgh",
                   "안녕하세요", "점심 뭐 먹지", "손흥민"]
OFF_TOPIC_HOLDOUT = ["비트코인 시세", "롤 티어 올리는 법", "강아지 산책 시간", "김치찌개 레시피", "블랙핑크", "수학 숙제 도와줘",
                     "1234", "너 누구야", "여자친구 선물 추천", "ㅁㄴㅇㄹ"]
# 검색 점수가 약하거나 입력 검사가 오해하기 쉬운 정상 설명. 상관없다고 판정하면 안 된다 (기준값·프롬프트 4를 정할 때 씀).
# 보닛·트렁크·디젤차는 첫 판정 규칙(키워드 3.0 미만 + 코사인 0.38 미만)에서, 후드 열림은 후보 없이 묻던 프롬프트 4에서
# 잘못 걸러졌던 말
NO_KEYWORD = ["check engine light", "tire pressure warning", "battery light", "brake warning", "seat belt",
              "차 문 열렸다는 표시", "보닛 열림", "트렁크 열림", "디젤차", "후드 열림"]
# 모양·색 낱말이 없어 입력 검사(LLM)를 거치는 정상 설명. 결과를 보기 전에 정해 두었다 (걸러지면 안 됨).
VALID_HOLDOUT = ["엔진 과열", "냉각수 부족", "브레이크액 부족", "와셔액 부족", "문 열림", "트렁크 안 닫힘", "핸들 무거워짐",
                 "충전 커넥터", "rear seat reminder", "차선 이탈"]
# 자동차 낱말과 글자만 겹치거나(후드티의 '후드') 같은 낱말을 다른 뜻으로 쓴 말. 걸러야 한다.
# TUNED 는 입력 검사(프롬프트 4)를 만들 때 본 것, HOLDOUT 은 결과를 보기 전에 정해 둔 것.
FALSE_FRIEND_TUNED = ["후드티", "브레이크댄스", "도어락", "오일파스타", "핸드폰 배터리", "블루투스 이어폰", "후드집업 추천",
                      "거북이 키우는 법", "커피 맛집", "벨트 사이즈", "삼각형 넓이 공식", "눈 오는 날"]
FALSE_FRIEND_HOLDOUT = ["에어팟 배터리 교체", "도어벨 설치", "오일 마사지", "눈사람 만들기", "열쇠고리 추천", "커피 머신 추천",
                        "삼각김밥 칼로리", "타이어 그네 만들기", "벨트백 추천", "파워에이드"]
# 색·모양 낱말이 들어간 엉뚱한 말. 색·모양 낱말이 있으면 입력 검사를 건너뛰던 때 통과했다("빨간 후드티").
# 결과를 보기 전에 정했고, 건너뛰기를 없앨지 정하는 실험에서 한 번 본 뒤로 규칙을 바꾸지 않았다.
COLOR_TRAP_HOLDOUT = ["빨간 후드티", "노란 바나나", "빨간 립스틱 추천", "파란 셔츠", "빨간 장미 꽃다발", "초록색 개구리 키우기",
                      "노란 우비 사이즈", "하얀 운동화 세탁", "빨간 고추장 레시피", "파란색 볼펜", "하트 모양 쿠키",
                      "별 모양 목걸이", "고양이 그림 그리기"]


def evaluate_off_topic(name: str, queries: list[str], expect: bool) -> int:
    """expect=True 면 상관없다고 판정해야 하는 질의, False 면 판정하면 안 되는 질의. 기대와 맞은 수를 돌려준다."""
    ok = 0
    print(f"\n== {name} ({len(queries)}건, {'상관없음으로 걸러야 함' if expect else '걸러지면 안 됨'}) ==")
    for q in queries:
        main, others, off = find_signals_split(q)
        ok += off == expect
        shown = [s.name[:12] for s in (others if off else main)]
        print(f"{'✅' if off == expect else '❌'} {q:<24} {'상관없음 판정' if off else '후보 표시'}  {shown}")
    print(f"→ 기대대로 {ok}/{len(queries)}")
    return ok


def evaluate(name: str, cases: list[tuple[str, set[str]]]) -> dict:
    top1 = top3 = noise = 0
    print(f"\n== {name} ({len(cases)}건) ==")
    for q, good in cases:
        shown = find_signals(q, k=5)
        ids = [s.id for s in shown]
        rank = next((i + 1 for i, sid in enumerate(ids) if sid in good), None)
        bad = sum(sid not in good for sid in ids)
        top1 += rank == 1
        top3 += bool(rank and rank <= 3)
        noise += bad
        mark = "✅" if rank == 1 else ("🟡" if rank and rank <= 3 else "❌")
        print(f"{mark} {q:<24} 정답순위={rank or '-'}  표시 {len(ids)}건 중 오답 {bad}  {[s.name[:12] for s in shown]}")
    n = len(cases)
    result = {"top1": top1, "top3": top3, "noise": noise / n, "n": n}
    print(f"→ Top-1 {top1}/{n}  Top-3 {top3}/{n}  오답 노출 평균 {noise / n:.2f}건")
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # 입력 검사 시간 초과 경고(로그)가 결과 사이에 보이도록
    evaluate("조정용 (TUNED)", TUNED)
    evaluate("조정에 안 쓴 질의 (HOLDOUT)", HOLDOUT)
    evaluate_off_topic("상관없는 설명 조정용 (OFF_TOPIC_TUNED)", OFF_TOPIC_TUNED, True)
    evaluate_off_topic("검색 점수가 약한 정상 설명 조정용 (NO_KEYWORD)", NO_KEYWORD, False)
    evaluate_off_topic("입력 검사를 거치는 정상 설명, 조정에 안 씀 (VALID_HOLDOUT)", VALID_HOLDOUT, False)
    evaluate_off_topic("상관없는 설명, 조정에 안 씀 (OFF_TOPIC_HOLDOUT)", OFF_TOPIC_HOLDOUT, True)
    evaluate_off_topic("글자만 겹치는 엉뚱한 말 조정용 (FALSE_FRIEND_TUNED)", FALSE_FRIEND_TUNED, True)
    evaluate_off_topic("글자만 겹치는 엉뚱한 말, 조정에 안 씀 (FALSE_FRIEND_HOLDOUT)", FALSE_FRIEND_HOLDOUT, True)
    evaluate_off_topic("색·모양 낱말이 들어간 엉뚱한 말, 조정에 안 씀 (COLOR_TRAP_HOLDOUT)", COLOR_TRAP_HOLDOUT, True)
