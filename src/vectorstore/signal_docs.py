"""선별한 외부 문서를 청크로 나눠 signal_docs 컬렉션에 색인한다.
청크 ID = 신호ID:문서ID:순번 → 여러 신호가 같은 문서를 골라도 서로 덮어쓰지 않는다."""
from functools import lru_cache

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.config import CHROMA_DIR, CHUNK_OVERLAP, CHUNK_SIZE, DOC_COLLECTION
from src.llm import get_embeddings

_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)


@lru_cache(maxsize=1)
def get_docs_store() -> Chroma:
    return Chroma(
        collection_name=DOC_COLLECTION,
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_DIR,
    )


def chunk_document(signal_id: str, doc: dict) -> tuple[list[Document], list[str]]:
    """청크마다 출처·제목을 앞에 붙여, 청크만 따로 검색돼도 무슨 문서인지 알 수 있게 한다."""
    header = f"[{doc.get('manual') or doc['source_type']}] {doc['title']}"
    chunks = _splitter.split_text(doc["text"])
    docs = [
        Document(
            page_content=f"{header}\n{chunk}",
            metadata={
                "signal_id": signal_id, "doc_id": doc["doc_id"], "chunk_no": n,
                "title": doc["title"], "manual": doc.get("manual", ""),
                "source": doc["source"], "source_type": doc["source_type"],
            },
        )
        for n, chunk in enumerate(chunks)
    ]
    return docs, [f"{signal_id}:{doc['doc_id']}:{n}" for n in range(len(chunks))]


def index_signal_docs(signal_id: str, docs: list[dict], store: Chroma | None = None) -> int:
    """신호 하나의 문서를 다시 색인한다. 선별 결과가 바뀌었을 수 있으니 그 신호의 기존 청크는 먼저 지운다."""
    store = store or get_docs_store()
    store._collection.delete(where={"signal_id": signal_id})
    chunks, ids = [], []
    for doc in docs:
        c, i = chunk_document(signal_id, doc)
        chunks += c
        ids += i
    if chunks:
        store.add_documents(chunks, ids=ids)
    return len(chunks)
