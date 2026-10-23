"""화면 공통 요소: 면책 문구, 진행 상태, 오류 안내, 진단 카드·근거 표시."""
import re

import streamlit as st

from src.data_prep.signals import Signal
from src.services.diagnosis import URGENCY_STYLE
from src.services.errors import DISCLAIMER, STEP_LABELS, friendly_error  # noqa: F401 (app.py 가 여기서 가져다 쓴다)

SOURCE_ICON = {"신호 메타데이터": "🏷️", "제조사 매뉴얼": "📘", "백과사전": "📗", "공공 리콜": "🔔"}  # 출처 종류
COLOR_EMOJI = {"빨강": "🔴", "노랑": "🟡", "주황": "🟠", "초록": "🟢", "파랑": "🔵", "흰색": "⚪", "회색": "⚫"}


def disclaimer():
    st.warning(DISCLAIMER, icon="⚠️")


def run_with_status(stream, label: str) -> dict | None:
    """그래프 스트림을 돌리며 단계마다 진행 상태를 보여 준다. 실패하면 안내를 띄우고 None."""
    state = None
    with st.status(label, expanded=False) as status:
        try:
            for node, state in stream:
                status.write(f"✓ {STEP_LABELS.get(node, node)}")
        except Exception as e:  # noqa: BLE001 — 어떤 오류든 화면에는 안내만
            status.update(label="처리하지 못했습니다", state="error")
            st.error(friendly_error(e))
            return None
        status.update(label="완료", state="complete")
    return state


def color_label(color: str) -> str:
    emojis = "".join(e for name, e in COLOR_EMOJI.items() if name in color)
    return f"{emojis} {color}" if emojis else "💬 표시창 문구"


def signal_summary(s: Signal):
    st.markdown(f"**{s.name}**")
    st.caption(f"{s.kind} · {color_label(s.color)} · 모양: {s.shape} · 적용: {s.powertrain}")


def evidence_list(evidence: list[dict], numbers: list[int] | None = None, note: str = ""):
    """근거 목록. numbers 를 주면 그 번호만 보여 준다(실제로 인용한 근거)."""
    shown = [e for e in evidence if numbers is None or e["n"] in numbers]
    if not shown:
        if numbers is not None:  # 인용된 근거가 하나도 없으면 그 사실을 알린다
            st.caption("ℹ️ 이 답변에 인용된 근거 문서가 없습니다. 근거 문서에서 확인되지 않은 내용일 수 있어요.")
        return
    with st.expander(f"근거 {len(shown)}건", expanded=False):
        if note:
            st.caption(f"ℹ️ {note}")
        for e in shown:
            icon = SOURCE_ICON.get(e["source_type"], "📄")
            link = f" · [원문 보기]({e['source']})" if e["source"] else ""
            st.markdown(f"**[{e['n']}] {e['title']}**  \n{icon} {e['source_type']} · {e['manual']}{link}")
            st.caption(e["text"][:300] + ("…" if len(e["text"]) > 300 else ""))


RECALL_NOTE = ("내 차가 실제 리콜 대상인지는 [자동차리콜센터](https://www.car.go.kr)에서 차대번호로 확인하세요. "
               "출처: 한국교통안전공단 차종별 리콜대수(공공데이터포털)")


def recall_list(recalls: list[dict], label: str, note: str = ""):
    """국내 리콜 목록 (출처 종류: 공공 리콜). 신호 근거와 따로 보여 준다."""
    if not recalls:
        return
    # 차명 괄호 안 세대 코드(투싼(TL)·투싼(NX4))가 여러 개면 내 차 세대를 확인하라고 알린다
    gens = list(dict.fromkeys(m.group(1).split()[0] for r in recalls
                              if (m := re.search(r"\(\s*([A-Z0-9]+)", r["car"]))))
    with st.expander(f"🔔 {label} {len(recalls)}건", expanded=False):
        if note:
            st.caption(f"ℹ️ {note}")
        if len(gens) > 1:
            st.caption(f"ℹ️ 여러 세대({', '.join(gens)}) 기록이 섞여 있어요. 차명 괄호 안 코드로 내 차 세대를 확인하세요.")
        for r in recalls:
            st.markdown(f"**{r['car']}** · 리콜 개시 {r['date']}  \n"
                        f"{r['source_type']} · 생산기간 {r['period']} · {r['count']}대 · [원문 보기]({r['source']})")
            st.caption(r["reason"][:300] + ("…" if len(r["reason"]) > 300 else ""))
        st.caption(RECALL_NOTE)


def diagnosis_card(card: dict, evidence: list[dict], signal_name: str, note: str = ""):
    color, icon = URGENCY_STYLE.get(card["urgency"], URGENCY_STYLE["확인되지 않음"])
    with st.container(border=True):
        st.badge(card["urgency"], icon=icon, color=color)
        st.markdown(f"#### {signal_name}")
        st.markdown(f"**의미**  \n{card['meaning']}")
        if card["do_now"]:
            st.markdown("**지금 할 일**\n" + "\n".join(f"- {x}" for x in card["do_now"]))
        if card["dont"]:
            st.markdown("**하지 말 것**\n" + "\n".join(f"- {x}" for x in card["dont"]))
        if not card["do_now"] and not card["dont"]:
            st.caption("근거 문서에서 확인된 행동 안내가 없습니다.")
        evidence_list(evidence, card["evidence"], note)
