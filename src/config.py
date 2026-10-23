"""실행 설정. 청크 크기·검색 개수 등은 코드가 아니라 .env 로 조정한다."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # 수집한 문서 캐시
SIGNALS_CSV = DATA_DIR / "signals.csv"

# OpenAI 키. OpenAI 호환 게이트웨이를 쓸 때는 OPENAI_BASE_URL 도 넣는다 (비우면 OpenAI 기본 주소).
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "") or None
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
WIKI_CONTACT = os.getenv("WIKI_CONTACT", "")
DATA_GO_KR_KEY = os.getenv("DATA_GO_KR_KEY", "")  # 공공데이터포털 일반 인증키(Decoding)
CHROMA_DIR = os.getenv("CHROMA_DIR", str(ROOT / "chroma_db"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", 600))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", 100))
TOP_K = int(os.getenv("TOP_K", 5))
# 근거 검색 방식: similarity = 질문과 가까운 순, mmr = 후보를 넉넉히 가져와 겹치는 내용을 피해 고름
SEARCH_TYPE = os.getenv("SEARCH_TYPE", "similarity").strip().lower()
MMR_FETCH_K = int(os.getenv("MMR_FETCH_K", 30))       # MMR 후보 수
MMR_LAMBDA = float(os.getenv("MMR_LAMBDA", 0.5))      # 1에 가까울수록 관련성, 0에 가까울수록 다양성

META_COLLECTION = "signal_meta"     # 항목 메타데이터 (RAG 1)
DOC_COLLECTION = "signal_docs"      # 외부 문서 청크 (RAG 2)
