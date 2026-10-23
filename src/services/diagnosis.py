"""진단 카드. 신호 하나를 골라 긴급도·의미·지금 할 일·하지 말 것·근거를 JSON 카드로 만든다."""
from src.data_prep.signals import load_signals
from src.rag.graph import stream_ask

URGENCY_STYLE = {   # 화면 배지: (색, 아이콘). 즉시 정차와 즉시 조치는 둘 다 빨강이고 아이콘으로 구분한다.
    "즉시 정차": ("red", "🛑"),
    "즉시 조치": ("red", "🚨"),
    "빠른 점검": ("orange", "🔧"),
    "주의": ("yellow", "⚠️"),
    "정보": ("blue", "ℹ️"),
    "확인되지 않음": ("gray", "❔"),
}


def signal_name(signal_id: str) -> str:
    return next((s.name for s in load_signals() if s.id == signal_id), signal_id)


def stream_card(signal_id: str, car: str = "", year: int | None = None, vehicle: str = "",
                model: str = "", powertrain: str = ""):
    """stream_ask 와 같은 형식으로 (노드 이름, 상태)를 내보낸다. 최종 상태의 'card'가 진단 카드."""
    question = f"{signal_name(signal_id)}이(가) 떴어요. 무슨 뜻이고 무엇을 해야 하나요?"
    yield from stream_ask(question, selected_signal=signal_id, selected_car=car, selected_year=year, mode="card",
                          vehicle=vehicle, model=model, powertrain=powertrain)
