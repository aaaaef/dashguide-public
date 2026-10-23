"""Streamlit 진입점 (UI 영역). 실행: streamlit run app.py"""
import logging

import streamlit as st

from src.config import DOC_COLLECTION, META_COLLECTION, OPENAI_API_KEY
from src.data_prep.signals import load_signals
from src.rag.prompts import NOT_FOUND
from src.rag.graph import stream_ask
from src.services.diagnosis import signal_name, stream_card
from src.services.finder import COLORS, KINDS, find_signals_split
from src.ui.components import (diagnosis_card, disclaimer, evidence_list, friendly_error, recall_list,
                               run_with_status, signal_summary)
from src.vectorstore.status import collection_counts
from src.vehicles import (MODELS, NOT_LISTED, NOT_SELECTED, applies, describe, find_recalls, is_exact_manual,
                          powertrain_options, reference_manual)

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="대시가이드", page_icon="🚗", layout="centered")
st.title("🚗 대시가이드")
st.caption("현대·기아 공식 취급설명서를 근거로, 계기판 신호의 뜻과 지금 할 일을 알려 드립니다.")
disclaimer()

PAGE_FIND, PAGE_CARD, PAGE_ASK = "🔍 경고등 찾기", "🩺 진단 카드", "💬 질문하기"
FREE = ""  # 질문하기에서 신호를 고르지 않은 자유 질문 대화

ss = st.session_state
ss.setdefault("cards", {})       # (신호ID, 차명, 연식) → (카드, 근거, 리콜). 화면이 다시 그려져도 LLM을 다시 부르지 않는다.
ss.setdefault("chats", {})       # 신호ID(자유 질문은 "") → 대화 기록. 신호별로 대화를 따로 둔다
ss.setdefault("finder_pick", None)
ss.setdefault("page", PAGE_FIND)


@st.cache_data(show_spinner=False)
def signal_options() -> dict[str, str]:
    return {s.id: f"{s.name}  ({s.kind})" for s in load_signals()}


@st.cache_data(show_spinner=False)
def car_recalls(model: str, powertrain: str, year: int | None) -> list[dict]:
    return find_recalls(model, powertrain, year, limit=200)


# 다른 화면에서 신호를 골라 넘어갈 때 쓰는 콜백. 위젯이 그려지기 전에 값을 바꿔야 해서 콜백으로 둔다.
def go_ask(signal_id: str):
    ss.ask_signal, ss.page = signal_id, PAGE_ASK


def go_card(signal_id: str):
    ss.card_pick, ss.page = signal_id, PAGE_CARD


counts = collection_counts()

with st.sidebar:
    st.header("내 차 정보")
    brand = st.selectbox("제조사", list(MODELS))
    model = st.selectbox("차종 (선택)", [NOT_SELECTED, *MODELS[brand]], key=f"model_{brand}")
    model = "" if model == NOT_SELECTED else model
    powertrain = st.radio("연료·동력 방식", powertrain_options(model), horizontal=True, key=f"pt_{brand}_{model}",
                          help="같은 동력 방식이면 계기판 경고등 체계가 대부분 같습니다. 모르면 '모름'을 고르세요.")
    year = st.number_input("연식 (선택)", min_value=0, max_value=2030, value=0, step=1,
                           help="0이면 지정하지 않습니다.") or None
    car = reference_manual(powertrain, model)
    vehicle = describe(model, powertrain)
    # 내 차 매뉴얼이 아닐 때 근거 목록에 붙일 안내
    ref_note = ""
    if car and not is_exact_manual(powertrain, model):
        ref_note = (f"{vehicle or powertrain} 차량 기준으로 같은 동력 방식의 {car} 취급설명서를 참고했습니다. "
                    "내 차의 실제 매뉴얼과 표시·문구가 다를 수 있어요.")
        st.caption(ref_note)
    elif car:
        st.caption(f"**{car}** 취급설명서를 근거로 합니다.")
    else:
        st.caption("동력 방식을 고르면 같은 방식 차량의 취급설명서를 먼저 참고합니다.")
    if model and model != NOT_LISTED:
        try:
            all_recalls = car_recalls(model, powertrain, year)
        except Exception as e:  # noqa: BLE001
            st.caption(friendly_error(e))
            all_recalls = []
        if all_recalls:
            recall_list(all_recalls[:20], "이 차종 국내 리콜 기록",
                        f"전체 {len(all_recalls)}건 중 최근 20건" if len(all_recalls) > 20 else "")
        else:
            st.caption(f"{vehicle} {year or ''} 조건의 국내 리콜 기록을 찾지 못했습니다.")
    elif model == NOT_LISTED:
        st.caption("목록에 없는 차종은 리콜 기록을 연결하지 않습니다.")
    st.divider()
    st.caption(f"🏷️ 신호 메타데이터 색인: {counts[META_COLLECTION]}건 · 📚 문서 청크: {counts[DOC_COLLECTION]}개")
    st.caption("출처: 📘 현대·기아 취급설명서(교육 목적 비상업적 이용, 저작권은 각 제조사) · "
               "📗 위키백과(CC BY-SA 4.0, 원리·용어 질문에만 사용) · 🔔 한국교통안전공단 차종별 리콜(공공데이터포털)")

# ---------- 실행 전 점검: 키 누락·색인 없음 ----------
if not OPENAI_API_KEY:
    st.error("API 키(OPENAI_API_KEY)가 설정되지 않았습니다. `.env.example`을 `.env`로 복사해 키를 넣은 뒤 "
             "다시 실행하세요.")
    st.stop()
if not counts[META_COLLECTION] or not counts[DOC_COLLECTION]:
    st.error("검색 색인이 없습니다. 아래 명령으로 데이터를 준비·색인한 뒤 다시 실행하세요.")
    st.code("python -m src.data_prep.manuals\npython -m src.data_prep.wiki\n"
            "python -m src.vectorstore.signal_meta\npython -m src.rag.collect", language="bash")
    st.stop()


def show_card(signal_id: str, where: str):
    """진단 카드를 만들거나(처음) 저장된 것을 그린다. where 는 버튼 키를 화면마다 다르게 하려는 구분값."""
    sig = next(s for s in load_signals() if s.id == signal_id)
    if not applies(sig.powertrain, powertrain):
        st.info(f"이 신호는 {sig.powertrain} 차량에 있는 신호입니다. 선택한 '{powertrain}' 차량에는 보통 없으니 "
                "다른 신호를 잘못 본 건 아닌지 확인해 보세요.")
    key = (signal_id, car, vehicle, year)
    if key not in ss.cards:
        state = run_with_status(stream_card(signal_id, car, year, vehicle, model, powertrain), "진단 카드를 만드는 중…")
        if state is None:
            return
        ss.cards[key] = (state["card"], state.get("evidence", []), state.get("recalls", []))
    card, evidence, recalls = ss.cards[key]
    diagnosis_card(card, evidence, signal_name(signal_id), ref_note)
    recall_list(recalls, "이 신호의 장치와 관련된 내 차 리콜", "리콜 사유에 이 신호 장치 이름이 들어 있는 기록입니다.")
    st.button("💬 이 신호에 대해 더 물어보기", key=f"ask_{where}_{signal_id}", on_click=go_ask, args=(signal_id,))


page = st.segmented_control("화면", [PAGE_FIND, PAGE_CARD, PAGE_ASK], key="page", label_visibility="collapsed") \
    or PAGE_FIND

# ---------- 경고등 찾기 ----------
if page == PAGE_FIND:
    st.markdown("이름을 몰라도 괜찮아요. **색과 모양**으로 설명해 보세요.")
    desc = st.text_input("어떻게 생겼나요?", placeholder="예: 주황색인데 타이어 안에 느낌표 있는 거", key="find_desc",
                         max_chars=100)
    c1, c2 = st.columns(2)
    colors = c1.multiselect("색상", COLORS, help="노랑에는 주황(황색)도 포함됩니다.", key="find_colors")
    kinds = c2.multiselect("종류", KINDS, key="find_kinds")

    def candidate(s):
        with st.container(border=True):
            left, right = st.columns([3, 1])
            with left:
                signal_summary(s)
            if right.button("🩺 진단 카드", key=f"pick_{s.id}", width="stretch"):
                ss.finder_pick = s.id
            right.button("💬 질문하기", key=f"chat_{s.id}", on_click=go_ask, args=(s.id,), width="stretch")

    if desc or colors or kinds:
        try:
            results, others, off_topic = find_signals_split(desc, colors, kinds, k=5)
        except Exception as e:  # noqa: BLE001
            st.error(friendly_error(e))
            results, others, off_topic = [], [], False
        if off_topic:
            st.info(f"'{desc}'은(는) 계기판 경고등·표시와 관련된 설명이 아닌 것 같아요. "
                    "색·모양·화면 문구로 설명해 주세요. 예: 주황색 타이어 안에 느낌표, 거북이 모양, 빨간 주전자")
        elif not results:
            st.info("조건에 맞는 신호를 찾지 못했습니다. 설명을 바꾸거나 필터를 줄여 보세요.")
        for s in results:
            candidate(s)
        if others:
            with st.expander("그래도 후보 보기" if off_topic else "찾는 신호가 없나요? 다른 후보 보기"):
                for s in others:
                    candidate(s)
        if ss.finder_pick and ss.finder_pick in {s.id for s in results + others}:
            st.divider()
            show_card(ss.finder_pick, "find")

# ---------- 진단 카드 ----------
elif page == PAGE_CARD:
    options = signal_options()
    picked = st.selectbox("신호 선택", list(options), format_func=options.get, index=None,
                          placeholder="신호 이름을 입력해 검색하세요", key="card_pick")
    if picked:
        show_card(picked, "card")
    else:
        st.caption("신호를 고르면 긴급도·의미·지금 할 일·하지 말 것을 근거와 함께 보여 드립니다.")

# ---------- 질문하기 ----------
else:
    options = {FREE: "선택 안 함 (자유 질문)", **signal_options()}
    sig = st.selectbox("질문할 신호", list(options), format_func=options.get, key="ask_signal",
                       help="신호를 고르면 그 신호의 문서를 먼저 근거로 쓰고, 대화 기록도 신호별로 따로 저장됩니다.")
    messages = ss.chats.setdefault(sig, [])
    if sig:
        st.button("🩺 이 신호 진단 카드 보기", key="to_card", on_click=go_card, args=(sig,))
    if not messages:
        st.caption("예: 엔진 경고등이 켜진 것과 깜빡이는 것은 다른가요? · 전기차 거북이 표시가 뜨면 무엇이 제한되나요?"
                   if not sig else f"'{signal_name(sig)}'에 대해 궁금한 점을 물어보세요.")
    for m in messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] == "assistant" and not m.get("chat"):  # 인사·범위 밖 답에는 근거 안내를 붙이지 않는다
                evidence_list(m.get("evidence", []), m.get("cited"), m.get("note", ""))
                recall_list(m.get("recalls", []), "관련 내 차 리콜")
    if messages and st.button("이 대화 지우기"):
        ss.chats[sig] = []
        st.rerun()

    # 질문하기 화면에서만 입력창을 보여 준다 (다른 화면에서 입력해 답이 안 보이던 혼란 해소)
    question = st.chat_input(f"'{signal_name(sig)}'에 대해 물어보세요" if sig else "계기판 신호에 대해 물어보세요",
                             max_chars=500)
    if question:
        history = [(m["role"], m["content"]) for m in messages]
        messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            state = run_with_status(stream_ask(question, history, selected_signal=sig, selected_car=car,
                                               selected_year=year, vehicle=vehicle, model=model,
                                               powertrain=powertrain),
                                    "답변을 준비하는 중…")
            if state is not None:
                answer = state.get("answer") or NOT_FOUND
                cited = state.get("cited", [])
                evidence = state.get("evidence", [])
                # 리콜을 물어 답변 근거에 리콜이 들어갔으면 근거 목록에 이미 있으므로 따로 보여 주지 않는다.
                # 인용한 근거가 없는 답(확인되지 않음)에는 억지로 고른 신호 기준의 리콜을 붙이지 않는다.
                in_evidence = any(e["source_type"] == "공공 리콜" for e in evidence)
                signal_cited = any(e.get("signal_id") for e in evidence if e["n"] in cited)
                recalls = state.get("recalls", []) if signal_cited and not in_evidence else []
                is_chat = bool(state.get("chat"))
                st.markdown(answer)
                if not is_chat:
                    evidence_list(evidence, cited, ref_note)
                recall_list(recalls, "관련 내 차 리콜")
                messages.append({"role": "assistant", "content": answer, "evidence": evidence, "cited": cited,
                                 "note": ref_note, "recalls": recalls, "chat": is_chat})
            else:
                messages.pop()  # 실패한 질문은 기록에서 빼서 다시 물을 수 있게

st.divider()
st.caption("⚠️ 정비 전문가의 진단을 대신하지 않습니다. 근거 문서에서 확인되지 않는 내용은 답하지 않습니다.")
