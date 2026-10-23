"""신호 검색 방식 비교: 벡터 단독 / 키워드(BM25) 단독 / 하이브리드(search_signals)에서 정답 신호의 순위.
질문하기 그래프의 신호 특정 단계가 쓰는 검색이라 점수 자르기 없이 본다.
실행: python -m evaluation.signal_search
"""
import sys

from src.vectorstore.signal_meta import _keyword_index, _keyword_query, get_meta_store, search_signals

CASES = [
    ("주황색인데 타이어 안에 느낌표 있는 거", "SIG-019"),
    ("거북이 표시", "SIG-004"),
    ("거북이 모양 경고등", "SIG-004"),
    ("빨간색 원 안에 느낌표", "SIG-011"),
    ("커피잔 모양", "SIG-032"),
    ("주전자 모양 빨간불", "SIG-016"),
    ("렌치 모양", "SIG-002"),
    ("눈 결정 표시", "SIG-047"),
    ("노란 엔진 모양", "SIG-017"),
    ("차가 미끄러지는 그림", "SIG-036"),
    ("ABS 불 들어옴", "SIG-012"),
    ("핸들에 느낌표", "SIG-014"),
    ("변속기가 너무 뜨겁다고 나와", "SIG-062"),
    ("기름이 얼마 안 남았대", "SIG-015"),
    ("디젤차 매연 필터", "SIG-025"),
]


def _rank(ids: list[str], target: str):
    return ids.index(target) + 1 if target in ids else "-"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    store = get_meta_store()
    index, _ = _keyword_index()
    print(f"{'질의':<28}{'정답':<9}{'벡터':>5}{'키워드':>6}{'하이브리드':>8}")
    hits = {"v": 0, "b": 0, "h": 0}
    for q, target in CASES:
        v = _rank([d.metadata["signal_id"] for d in store.similarity_search(q, k=100)], target)
        b = _rank([i for i, _ in index.search(_keyword_query(q) or q, k=100)], target)
        h = _rank([s.id for s, _ in search_signals(q, k=100)], target)
        for key, r in (("v", v), ("b", b), ("h", h)):
            hits[key] += r != "-" and r <= 3
        print(f"{q:<28}{target:<9}{v!s:>5}{b!s:>6}{h!s:>8}")
    n = len(CASES)
    print(f"\nTop-3 적중: 벡터 {hits['v']}/{n}, 키워드 {hits['b']}/{n}, 하이브리드 {hits['h']}/{n}")
