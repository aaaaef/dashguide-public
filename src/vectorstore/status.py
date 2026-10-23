"""색인 상태 확인 (색인 건수 표시, 색인 없음 안내).
임베딩 없이 chromadb 에 직접 물어서 API 키가 없어도 건수를 셀 수 있다."""
import chromadb

from src.config import CHROMA_DIR, DOC_COLLECTION, META_COLLECTION


def collection_counts() -> dict[str, int]:
    """{컬렉션 이름: 청크 수}. 컬렉션이 없으면 0."""
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    names = {c if isinstance(c, str) else c.name for c in client.list_collections()}
    return {name: client.get_collection(name).count() if name in names else 0
            for name in (META_COLLECTION, DOC_COLLECTION)}


if __name__ == "__main__":
    print(collection_counts())
