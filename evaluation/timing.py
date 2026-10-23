"""처리 시간: 첫 질문과 두 번째 질문을 나눠 잰다.
첫 질문에는 색인 로드와 LLM·임베딩 연결이 더해져 조금 더 걸린다. 단계(노드)별 시간도 함께 보여 준다
(LLM API가 가끔 한 호출만 17~19초씩 걸리는데, 어느 단계였는지 보이게).

실행: python -m evaluation.timing   (LLM 약 4회)
"""
import sys
import time

from src.rag.graph import stream_ask

QUESTIONS = ["엔진 경고등이 켜졌어. 계속 달려도 돼?", "타이어 공기압 경고등이 깜빡이다가 계속 켜져 있어"]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for i, q in enumerate(QUESTIONS, 1):
        start = last = time.time()
        steps = []
        for node, _ in stream_ask(q):
            now = time.time()
            steps.append(f"{node} {now - last:.1f}초")
            last = now
        print(f"{i}번째 질문 {time.time() - start:.1f}초 ({', '.join(steps)}) — {q}")


if __name__ == "__main__":
    main()
