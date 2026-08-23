from typing import List

try:
    from llama_index import TokenTextSplitter
except ImportError:
    TokenTextSplitter = None

from .pdf_parser import Document


def _fallback_split(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    tokens = text.split()
    if not tokens:
        return []
    step = max(chunk_size - chunk_overlap, 1)
    return [" ".join(tokens[i : i + chunk_size]) for i in range(0, len(tokens), step)]


def chunk_documents(documents: List[Document], parent_doc_id: str, chunk_size: int = 512, chunk_overlap: int = 50) -> List[Document]:
    chunks: List[Document] = []
    for document in documents:
        if TokenTextSplitter is not None:
            splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            split_texts = splitter.split_text(document.text)
        else:
            split_texts = _fallback_split(document.text, chunk_size, chunk_overlap)
        for split_text in split_texts:
            metadata = dict(document.metadata)
            metadata["parent_doc_id"] = parent_doc_id
            chunks.append(Document(text=split_text, metadata=metadata))
    return chunks
