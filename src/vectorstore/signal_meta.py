"""계기판 신호 100건을 signal_meta 컬렉션에 색인한다.
신호 1건 = 1청크, 문서 ID = 신호 ID 고정이라 다시 색인하면 같은 항목을 덮어쓴다.
검색은 벡터 + 키워드(BM25)를 순위 융합(RRF)으로 섞는다."""
import re
from dataclasses import dataclass
from functools import lru_cache

from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.config import CHROMA_DIR, META_COLLECTION
from src.data_prep.signals import Signal, load_signals
from src.llm import get_embeddings
from src.vectorstore.bm25 import BM25

# 매뉴얼은 같은 황색 경고등을 '노랑'·'주황'으로 섞어 쓰고, 사용자는 '빨간불'처럼 말한다.
_COLOR_SYNONYMS = {
    "빨강": ["빨간", "붉은", "적색"],
    "노랑": ["노란", "주황", "황색"],
    "초록": ["녹색"],
    "파랑": ["파란", "청색"],
    "흰색": ["하얀", "흰(?!색)"],
}
_COLOR_RE = [(re.compile("|".join(v)), k) for k, v in _COLOR_SYNONYMS.items()]
# 모양·색 질문은 키워드 일치가 더 정확해서 키워드 순위에 가중치를 더 준다.
# (평가 15건 기준 Top-3 적중: 벡터 8, 키워드 14, k=60 동일 가중 11, 아래 값 14)
_RRF_K = 5
_KEYWORD_WEIGHT = 2.0
_SHAPE_REPEAT = 2           # 키워드 색인에서 모양 설명을 추가로 넣는 횟수 (모양 일치에 가중치)
_COLOR_MISMATCH = 0.5       # 질문에 나온 색을 갖지 않은 신호의 점수 배율 (기억이 틀릴 수 있어 빼지는 않는다)
_KEYWORD_FLOOR = 0.2        # 키워드 점수가 1위의 이 비율 미만이면 잡음으로 보고 버린다
# 계기판과 상관없는 설명("페이커", "후드티") 판정. RRF는 순위만 쓰기 때문에 아무 말이나 넣어도 1위가 생긴다.
# 값은 evaluation/finder.py 조정용 질의로 정했다.
# - 글자가 하나도 안 겹치고(키워드 0) 1위 신호와의 코사인도 _OFF_TOPIC_VECTOR 미만이면 바로 상관없다고 본다.
#   글자가 안 겹치는 엉뚱한 설명은 최대 0.29, 키워드 없는 신호 설명('seat belt' 등 영어)은 최소 0.35
# - 코사인이 _CONFIDENT_VECTOR 이상이면 계기판 설명으로 본다. 글자만 겹치는 엉뚱한 설명은 최대 0.48
# - 그 사이에는 글자만 우연히 겹친 말("후드티"의 '후드')과 임베딩이 약한 정상 모양 설명("눈송이 모양" 0.24)이
#   섞여 있어 점수로 가를 수 없다 → finder 가 LLM(프롬프트 4)에 찾은 후보의 모양·색과 함께 묻는다
_OFF_TOPIC_VECTOR = 0.30
_CONFIDENT_VECTOR = 0.50
# 모든 신호에 흔한 말이라 키워드 검색에서 빼는 낱말 (예: '거북이 표시'의 '표시'가 '표시등'과 겹쳐 엉뚱한 신호가 오름).
# 낱말 전체가 일치할 때만 뺀다 ('거북이'의 '거'를 지우지 않도록).
_PARTICLE = r"(이|가|은|는|을|를|에|에서|도|요|야|이야|임|이에요|예요)?"
_GENERIC_WORD = re.compile(
    rf"(모양|그림|표시|마크|경고등|표시등|불|빨간불|램프|아이콘|거|것|게|계기판){_PARTICLE}"
    r"|(있|떴|뜨|떠|켜|나와|나오|나옴|들어)\w*"
)
# 일상어 → 매뉴얼 표현. 매뉴얼 '모양' 칸의 낱말과 글자가 겹치지 않아 키워드 검색이 못 찾는 말들.
_EVERYDAY_TERMS = {
    "스패너": "렌치", "공구": "렌치", "눈송이": "눈 결정", "눈꽃": "눈 결정", "동그라미": "원", "동그란": "원",
    "기름": "연료", "주유": "주유기", "핸들": "핸들 스티어링", "운전대": "핸들", "벨트": "안전벨트",
    "사이드 브레이크": "파킹 브레이크", "주차 브레이크": "파킹 브레이크", "깔때기": "필터", "온도계": "온도",
    "물방울": "물방울 수분", "거북": "거북이", "열쇠": "열쇠 키", "삼각형": "삼각형 통합",
    "보닛": "후드", "본넷": "후드", "트렁크": "테일게이트",
}


@dataclass(frozen=True)
class Relevance:
    """질문이 계기판 신호와 얼마나 맞닿아 있는지 (위 _OFF_TOPIC_VECTOR 설명 참고)."""
    keyword: float   # 질문 글자 중 색인에 있는 2-gram 의 idf 합 (0이면 하나도 안 겹침)
    cosine: float    # 1위 신호와의 코사인 유사도

    @property
    def off_topic(self) -> bool:
        return self.keyword == 0 and self.cosine < _OFF_TOPIC_VECTOR

    @property
    def confident(self) -> bool:
        return self.cosine >= _CONFIDENT_VECTOR


def normalize_colors(text: str) -> str:
    for pattern, canonical in _COLOR_RE:
        text = pattern.sub(canonical, text)
    return text


def _colors_in(text: str) -> set[str]:
    text = normalize_colors(text)
    return {c for c in _COLOR_SYNONYMS if c in text}


def _keyword_query(query: str) -> str:
    for word, manual_word in _EVERYDAY_TERMS.items():
        if word in query:
            query += " " + manual_word
    return " ".join(w for w in query.split() if not _GENERIC_WORD.fullmatch(w))


@lru_cache(maxsize=1)
def get_meta_store() -> Chroma:
    return Chroma(
        collection_name=META_COLLECTION,
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_DIR,
    )


def get_meta_chunk(signal_id: str) -> str:
    """색인된 신호 메타데이터 청크(1신호 = 1청크) 본문. 없으면 빈 문자열."""
    got = get_meta_store()._collection.get(ids=[signal_id], include=["documents"])
    return got["documents"][0] if got["documents"] else ""


def _to_document(s: Signal) -> Document:
    # Chroma 메타데이터는 문자열·숫자만 허용하므로 목록은 ';'로 합친다.
    return Document(
        page_content=s.to_index_text(),
        metadata={
            "signal_id": s.id, "name": s.name, "kind": s.kind, "category": s.category,
            "color": s.color, "urgency": s.urgency, "powertrain": s.powertrain,
            "brand": s.brand, "wiki": ";".join(s.wiki), "source": s.source,
            "source_type": "제조사 매뉴얼",
        },
    )


def index_signals(signals: list[Signal] | None = None) -> int:
    """색인 후 컬렉션의 전체 건수를 돌려준다."""
    signals = signals if signals is not None else load_signals()
    store = get_meta_store()
    store.add_documents([_to_document(s) for s in signals], ids=[s.id for s in signals])
    return store._collection.count()


@lru_cache(maxsize=1)
def _keyword_index() -> tuple[BM25, dict[str, Signal]]:
    signals = load_signals()
    texts = [s.to_index_text() + f"\n{s.shape}" * _SHAPE_REPEAT for s in signals]
    index = BM25([s.id for s in signals], texts, preprocess=normalize_colors)
    return index, {s.id: s for s in signals}


def search_signals(query: str, k: int = 5, pool: int = 30) -> list[tuple[Signal, float]]:
    """벡터·키워드 검색 결과를 각각 pool건까지 받아 RRF 점수로 합친다.
    질문에 색이 있으면 그 색을 갖지 않은 신호는 점수를 낮춘다."""
    return search_signals_checked(query, k, pool)[0]


def search_signals_checked(query: str, k: int = 5, pool: int = 30) -> tuple[list[tuple[Signal, float]], Relevance]:
    """search_signals 결과와 함께, 질문이 계기판 신호와 얼마나 맞닿아 있는지(Relevance)를 돌려준다."""
    index, by_id = _keyword_index()
    vector_hits = get_meta_store().similarity_search_with_score(query, k=pool)
    vector_ids = [d.metadata["signal_id"] for d, _ in vector_hits]
    keyword_query = _keyword_query(query) or query
    keyword_hits = index.search(keyword_query, k=pool)
    # 임베딩이 단위 벡터라 Chroma 기본 거리(제곱 L2) d 는 2 - 2·코사인
    relevance = Relevance(index.known_weight(keyword_query), 1 - vector_hits[0][1] / 2 if vector_hits else 0.0)

    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(vector_ids, 1):
        scores[doc_id] = 1 / (_RRF_K + rank)
    # 키워드는 순위에 더해 점수 비율도 반영한다. 순위만 쓰면 '1위 16점, 2위 2.8점'처럼 확신이 큰 차이가 사라진다.
    best = keyword_hits[0][1] if keyword_hits else 0
    for rank, (doc_id, s) in enumerate(keyword_hits, 1):
        if s >= best * _KEYWORD_FLOOR:
            scores[doc_id] = scores.get(doc_id, 0.0) + _KEYWORD_WEIGHT * (s / best) / (_RRF_K + rank)

    asked = _colors_in(query)
    if asked:
        for doc_id in scores:
            if not asked & _colors_in(by_id[doc_id].color):
                scores[doc_id] *= _COLOR_MISMATCH

    top = sorted(scores.items(), key=lambda x: -x[1])[:k]
    return [(by_id[doc_id], score) for doc_id, score in top], relevance


if __name__ == "__main__":
    print(f"색인 후 건수: {index_signals()}")
    print(f"재색인 후 건수: {index_signals()} (ID 고정이라 늘지 않아야 함)")

    for q in ["주황색인데 타이어 안에 느낌표 있는 거", "빨간 온도계 모양", "거북이 표시"]:
        print(f"\n▶ {q}")
        for s, score in search_signals(q, k=3):
            print(f"  - {s.id} {s.name} ({s.color}, {s.urgency}) 점수={score:.3f}")
