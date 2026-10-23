"""웹 화면용 API 서버 (화면 web/ 과 서버 분리). 실행: uvicorn src.api.main:app --port 8000

요청·응답 형식은 아래 pydantic 모델로 정의한다. LLM을 부르는 /api/card, /api/ask 는 진행 상태를 보여 주려고
한 줄에 JSON 하나씩 보내는 스트림(NDJSON)으로 답한다.
  {"type": "step", "node": "retrieve", "label": "취급설명서에서 근거를 찾았습니다"}
  {"type": "result", ...}  또는  {"type": "error", "message": "한국어 안내"}
화면에는 한국어 안내만 보내고, 오류 원인·프롬프트·키는 서버 로그에만 남긴다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import DOC_COLLECTION, META_COLLECTION, OPENAI_API_KEY, ROOT
from src.data_prep.signals import Signal, load_signals
from src.rag.graph import stream_ask
from src.rag.prompts import NOT_FOUND, URGENCY_LEVELS
from src.services.diagnosis import stream_card
from src.services.errors import DISCLAIMER, STEP_LABELS, friendly_error
from src.services.finder import COLORS, KINDS, find_signals_split
from src.vectorstore.status import collection_counts
from src.vehicles import (MODELS, NOT_LISTED, POWERTRAINS, UNKNOWN, applies, describe, find_recalls,
                          is_exact_manual, reference_manual)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="계기판 경고등·표시 해설 API")

FIND_MAX_LENGTH = 100   # 경고등 찾기 설명 (색·모양 설명은 짧다. 긴 글을 그대로 임베딩하지 않게)
ASK_MAX_LENGTH = 500    # 질문
_FIELD_NAMES = {"description": "설명", "question": "질문"}


# ---------- 요청 형식 ----------
class Car(BaseModel):
    brand: str = ""
    model: str = ""                       # 화면 차종 이름. 없으면 ""
    powertrain: str = UNKNOWN             # 모름 | 휘발유 | 디젤 | 하이브리드 | 전기차
    year: int | None = None


class FindRequest(BaseModel):
    description: str = Field("", max_length=FIND_MAX_LENGTH)
    colors: list[str] = []
    kinds: list[str] = []


class CardRequest(BaseModel):
    signal_id: str
    car: Car = Car()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=ASK_MAX_LENGTH)
    history: list[tuple[str, str]] = []   # [("user"|"assistant", 내용), ...] — 화면이 신호별로 들고 있다
    signal_id: str = ""                   # 질문할 신호 (자유 질문은 "")
    car: Car = Car()


# ---------- 공통 ----------
_SIGNALS: dict[str, Signal] = {s.id: s for s in load_signals()}
_CARD_CACHE: dict[tuple, dict] = {}       # (신호, 차종, 동력, 연식) → 카드 결과. 같은 카드는 LLM을 다시 부르지 않는다


def _signal_json(s: Signal) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "category": s.category, "color": s.color,
            "shape": s.shape, "powertrain": s.powertrain, "urgency": s.urgency}


def _car_context(car: Car) -> dict:
    """화면 차종 → 참고 매뉴얼, 프롬프트용 차량 설명, '다른 차종 매뉴얼 기준' 안내."""
    model = "" if car.model == NOT_LISTED else car.model
    manual = reference_manual(car.powertrain, model)
    vehicle = describe(model, car.powertrain)
    note = ""
    if manual and not is_exact_manual(car.powertrain, model):
        note = (f"{vehicle or car.powertrain} 차량 기준으로 같은 동력 방식의 {manual} 취급설명서를 참고했습니다. "
                "내 차의 실제 매뉴얼과 표시·문구가 다를 수 있어요.")
    return {"model": model, "manual": manual, "vehicle": vehicle, "note": note}


def _stream(events: Iterator[dict]) -> StreamingResponse:
    def lines():
        try:
            for e in events:
                yield json.dumps(e, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 — 어떤 오류든 화면에는 한국어 안내만
            yield json.dumps({"type": "error", "message": friendly_error(e)}, ensure_ascii=False) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson")


def _steps(stream) -> Iterator[tuple[dict | None, dict]]:
    """그래프 스트림 → (진행 이벤트, 지금까지의 상태)."""
    state: dict = {}
    for node, state in stream:
        yield {"type": "step", "node": node, "label": STEP_LABELS.get(node, node)}, state


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception):
    return JSONResponse(status_code=500, content={"detail": friendly_error(e)})


@app.exception_handler(RequestValidationError)
async def _invalid(_: Request, e: RequestValidationError):
    """입력 형식 오류도 한국어 안내 한 줄로 (기본 응답은 영어 오류 목록에 입력 원문까지 담는다)."""
    err = e.errors()[0] if e.errors() else {}
    field = _FIELD_NAMES.get(str(err.get("loc", ["", ""])[-1]), "입력")
    if err.get("type") == "string_too_long":
        msg = f"{field}이 너무 길어요. {err.get('ctx', {}).get('max_length')}자 안으로 줄여 주세요."
    elif err.get("type") == "string_too_short":
        msg = f"{field}을 입력해 주세요."
    else:
        msg = "요청 형식이 올바르지 않습니다. 화면을 새로 고친 뒤 다시 시도하세요."
    return JSONResponse(status_code=422, content={"detail": msg})


# ---------- API ----------
@app.get("/api/meta")
def meta():
    """화면을 그리는 데 필요한 고정 정보와 실행 상태(키 누락·색인 없음)."""
    counts = collection_counts()
    return {
        "status": {"key_ok": bool(OPENAI_API_KEY), "meta_count": counts[META_COLLECTION],
                   "doc_count": counts[DOC_COLLECTION]},
        "vehicles": MODELS, "powertrains": POWERTRAINS, "colors": COLORS, "kinds": KINDS,
        "urgency_levels": URGENCY_LEVELS, "disclaimer": DISCLAIMER,
        "signals": [_signal_json(s) for s in _SIGNALS.values()],
    }


@app.post("/api/car")
def car_info(car: Car):
    """내 차 설정이 바뀔 때: 참고 매뉴얼 안내 + 이 차종의 국내 리콜 기록 전체."""
    ctx = _car_context(car)
    recalls = find_recalls(ctx["model"], car.powertrain, car.year, limit=200) if ctx["model"] else []
    return {**ctx, "recalls": recalls}


@app.post("/api/find")
def find(req: FindRequest):
    """경고등 찾기: 색·모양 설명 + 색·종류 필터 → 주요 후보 / 다른 후보.
    off_topic: 설명이 계기판 신호와 상관없어 보임 (주요 후보 없이 다른 후보만)."""
    main, others, off_topic = find_signals_split(req.description, req.colors, req.kinds, k=5)
    return {"main": [_signal_json(s) for s in main], "others": [_signal_json(s) for s in others],
            "off_topic": off_topic}


@app.post("/api/card")
def card(req: CardRequest):
    """진단 카드 (스트림)."""
    sig = _SIGNALS.get(req.signal_id)
    if not sig:
        raise HTTPException(404, "선택한 신호를 찾지 못했습니다. 목록에서 다시 골라 주세요.")
    ctx = _car_context(req.car)
    warning = "" if applies(sig.powertrain, req.car.powertrain) else (
        f"이 신호는 {sig.powertrain} 차량에 있는 신호입니다. 선택한 '{req.car.powertrain}' 차량에는 보통 없으니 "
        "다른 신호를 잘못 본 건 아닌지 확인해 보세요.")
    key = (sig.id, ctx["model"], req.car.powertrain, req.car.year)

    def events():
        if key not in _CARD_CACHE:
            state: dict = {}
            for step, state in _steps(stream_card(sig.id, ctx["manual"], req.car.year, ctx["vehicle"],
                                                  ctx["model"], req.car.powertrain)):
                yield step
            _CARD_CACHE[key] = {"card": state["card"], "evidence": state.get("evidence", []),
                                "recalls": state.get("recalls", [])}
        yield {"type": "result", "signal": _signal_json(sig), "warning": warning, "note": ctx["note"],
               **_CARD_CACHE[key]}
    return _stream(events())


@app.post("/api/ask")
def ask(req: AskRequest):
    """질문하기 (스트림). 답변·실제 인용 번호·근거 전체·관련 리콜을 돌려준다."""
    ctx = _car_context(req.car)
    signal_id = req.signal_id if req.signal_id in _SIGNALS else ""

    def events():
        state: dict = {}
        for step, state in _steps(stream_ask(req.question, req.history, selected_signal=signal_id,
                                             selected_car=ctx["manual"], selected_year=req.car.year,
                                             vehicle=ctx["vehicle"], model=ctx["model"],
                                             powertrain=req.car.powertrain)):
            yield step
        evidence, cited = state.get("evidence", []), state.get("cited", [])
        # 리콜을 물어 답변 근거에 리콜이 들어갔으면 근거 목록에 이미 있으므로 따로 보내지 않는다.
        # 신호 문서를 인용하지 않은 답(확인되지 않음, 용어 문서만 인용)은 검색이 억지로 고른 신호 기준이라
        # 리콜·후보 신호도 보내지 않는다 ("액추에이터가 뭐야?"에 엉뚱한 신호 후보가 붙었다).
        in_evidence = any(e["source_type"] == "공공 리콜" for e in evidence)
        signal_cited = any(e.get("signal_id") for e in evidence if e["n"] in cited)
        # 근거를 찾은 신호 후보. 자유 질문에서 설명이 여러 신호에 맞을 수 있을 때 화면이 후보를 함께 보여 준다
        # (LLM이 한 신호로 단정해도 사용자가 다른 후보를 확인할 수 있게, #7).
        signals = [_signal_json(_SIGNALS[s["id"]]) for s in state.get("signals", []) if s["id"] in _SIGNALS]
        yield {"type": "result", "answer": state.get("answer") or NOT_FOUND, "cited": cited, "evidence": evidence,
               "recalls": state.get("recalls", []) if signal_cited and not in_evidence else [],
               "note": ctx["note"], "signals": signals if signal_cited else [], "chat": bool(state.get("chat"))}
    return _stream(events())


# 화면 파일 (API 경로를 먼저 등록한 뒤 나머지를 web/ 으로)
app.mount("/", StaticFiles(directory=Path(ROOT) / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="127.0.0.1", port=8000)
