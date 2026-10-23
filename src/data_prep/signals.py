"""계기판 신호 메타데이터를 로드하고 항목 1건을 하나의 색인 텍스트로 구성한다."""
import csv
from dataclasses import dataclass
from src.config import SIGNALS_CSV


@dataclass
class Signal:
    id: str
    name: str
    kind: str          # 경고등 / 표시등 / 표시창 경고문 / 표시창 정보
    category: str
    color: str
    shape: str
    powertrain: str
    urgency: str
    wiki: list[str]
    brand: str
    source: str

    def to_index_text(self) -> str:
        """검색용 텍스트. 사용자가 이름 대신 색·모양으로 묻는 경우를 위해 색상·모양을 반드시 포함한다."""
        lines = [
            f"이름: {self.name}",
            f"유형: {self.kind} / 분류: {self.category}",
            f"색상: {self.color} / 모양: {self.shape}",
            f"적용 파워트레인: {self.powertrain}",
            f"긴급도: {self.urgency}",
            f"관련 시스템: {', '.join(self.wiki) if self.wiki else '-'}",
            f"제조사: {self.brand}",
        ]
        return "\n".join(lines)


def load_signals(path=SIGNALS_CSV) -> list[Signal]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [
        Signal(
            id=r["id"], name=r["이름"], kind=r["유형"], category=r["범주"],
            color=r["색상(초안)"], shape=r["모양(초안)"], powertrain=r["적용 파워트레인"],
            urgency=r["긴급도(초안)"],
            wiki=[w for w in r["관련 위키 문서"].split(";") if w],
            brand=r["제조사"], source=r["출처 매뉴얼"],
        )
        for r in rows
    ]


if __name__ == "__main__":
    items = load_signals()
    print(f"항목 수: {len(items)}")
    print(items[0].to_index_text())
