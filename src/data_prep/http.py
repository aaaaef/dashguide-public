"""외부 API 호출 공통 규칙: 실패하면 간격을 늘려 다시 시도하고, 서버가 Retry-After 를 주면 따른다.
호출 사이 간격(REQUEST_INTERVAL)과 User-Agent 는 출처마다 다르므로 각 수집 모듈이 정한다."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

RETRIES = 3           # 첫 시도 뒤 다시 시도하는 횟수
BACKOFF = 2.0         # 대기 시간(초): 2 → 4 → 8
_RETRY_STATUS = {429, 500, 502, 503, 504}


def get(url: str, *, retries: int = RETRIES, backoff: float = BACKOFF, **kwargs) -> requests.Response:
    """requests.get + 재시도. 연결 오류·429·5xx 는 다시 시도하고, 그 밖의 응답은 그대로 돌려준다.
    끝까지 연결하지 못하면 마지막 requests.RequestException 을 그대로 올린다."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, **kwargs)
        except requests.RequestException:
            if attempt == retries:
                raise
            wait = backoff * 2 ** attempt
        else:
            if r.status_code not in _RETRY_STATUS or attempt == retries:
                return r
            retry_after = r.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else backoff * 2 ** attempt
        log.warning("외부 호출 실패, %.0f초 뒤 다시 시도 (%d/%d): %s", wait, attempt + 1, retries, url)
        time.sleep(wait)
    raise AssertionError("도달하지 않음")
