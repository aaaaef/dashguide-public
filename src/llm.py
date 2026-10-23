"""LLM·임베딩 객체 생성. 키와 모델은 .env 에서만 읽는다.
객체는 한 번만 만들어 재사용한다. 새로 만들면 첫 호출마다 연결을 다시 맺어 5~8초가 더 걸렸다."""
from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from src.config import EMBEDDING_MODEL, LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL


class ConfigError(RuntimeError):
    pass


def _check_key():
    if not OPENAI_API_KEY:
        raise ConfigError("API 키(OPENAI_API_KEY)가 설정되지 않았습니다. .env 를 확인하세요.")


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    _check_key()
    return ChatOpenAI(model=LLM_MODEL, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, temperature=temperature)


@lru_cache(maxsize=1)
def get_judge_llm(timeout: float = 6.0) -> ChatOpenAI:
    """짧은 판정용 (경고등 찾기 입력 검사). 화면이 오래 멈추지 않게 재시도 없이 timeout 초 안에 끝낸다.
    API가 가끔 한 호출만 17~19초씩 걸릴 때가 있다."""
    _check_key()
    return ChatOpenAI(model=LLM_MODEL, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, temperature=0,
                      timeout=timeout, max_retries=0)


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    _check_key()
    return OpenAIEmbeddings(model=EMBEDDING_MODEL, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


if __name__ == "__main__":
    print("base_url:", OPENAI_BASE_URL or "(OpenAI 기본)")
    print("LLM 응답:", get_llm().invoke("한 단어로만 답해: 안녕").content)
    print("임베딩 차원:", len(get_embeddings().embed_query("ABS 경고등")))
