"""화면에 보여 줄 오류·진행 문구 (Streamlit 화면과 웹 화면이 함께 쓴다)."""
import logging

import openai

from src.llm import ConfigError

log = logging.getLogger(__name__)

DISCLAIMER = ("이 안내는 정비 전문가의 진단을 대신하지 않습니다. "
              "빨간색 경고등이 켜지면 안전한 곳에 정차하고 점검을 받으세요.")

STEP_LABELS = {   # LangGraph 노드 → 진행 상태 문구
    "extract": "질문에서 신호와 검색어를 찾았습니다",
    "retrieve": "취급설명서에서 근거를 찾았습니다",
    "generate": "근거를 바탕으로 답변을 작성했습니다",
    "chat": "근거 검색이 필요 없는 말이라 바로 답했습니다",   # 인사·감사·범위 밖 질문
}


def friendly_error(e: Exception) -> str:
    """사용자에게 보여 줄 한국어 안내. 원인은 로그에만 남긴다."""
    log.exception("요청 처리 실패")
    if isinstance(e, ConfigError):
        return str(e)
    if isinstance(e, openai.AuthenticationError):
        return "API 키가 올바르지 않습니다. .env 의 OPENAI_API_KEY 를 확인하세요."
    if isinstance(e, openai.RateLimitError):
        return "요청이 많아 잠시 처리할 수 없습니다. 잠시 후 다시 시도하세요."
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        return "AI 서버에 연결하지 못했습니다. 네트워크를 확인한 뒤 다시 시도하세요."
    return "일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도하세요."
