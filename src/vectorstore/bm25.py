"""키워드 검색(BM25). 형태소 분석기 없이 한글은 글자 2-gram, 영문·숫자는 단어 단위로 자른다.
짧은 모양 표현("거북이", "커피잔")은 임베딩보다 글자 일치가 더 정확해서 벡터 검색과 섞어 쓴다."""
import math
import re
from collections import Counter
from typing import Callable

_TOKEN = re.compile(r"[가-힣]+|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    tokens = []
    for t in _TOKEN.findall(text.lower()):
        if t[0] >= "가" and len(t) > 1:
            tokens += [t[i:i + 2] for i in range(len(t) - 1)]
        else:
            tokens.append(t)
    return tokens


class BM25:
    def __init__(self, ids: list[str], texts: list[str],
                 preprocess: Callable[[str], str] = lambda s: s, k1: float = 1.5, b: float = 0.75):
        self.ids, self.preprocess, self.k1, self.b = ids, preprocess, k1, b
        self.docs = [Counter(tokenize(preprocess(t))) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lens) / len(self.lens)
        df = Counter(tok for d in self.docs for tok in d)
        n = len(self.docs)
        # 항상 양수인 idf (모든 문서에 나오는 "이름:" 같은 라벨은 0에 가까워진다)
        self.idf = {tok: math.log(1 + (n - c + 0.5) / (c + 0.5)) for tok, c in df.items()}

    def known_weight(self, query: str) -> float:
        """질문 토큰 중 색인 어딘가에 있는 토큰의 idf 합. 0이면 질문 글자가 색인과 하나도 겹치지 않는다."""
        return sum(self.idf.get(tok, 0.0) for tok in set(tokenize(self.preprocess(query))))

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        q = set(tokenize(self.preprocess(query)))
        scores = []
        for doc_id, doc, length in zip(self.ids, self.docs, self.lens):
            s = 0.0
            for tok in q & doc.keys():
                tf = doc[tok]
                s += self.idf[tok] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            if s > 0:
                scores.append((doc_id, s))
        return sorted(scores, key=lambda x: -x[1])[:k]
