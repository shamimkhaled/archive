import argparse
import uuid

from .chunker import chunk_documents
from .pdf_parser import parse_pdf
from .qdrant_store import QdrantIndexer, embed_texts
from .summary_extractor import extract_document_summary


def build_summary_embedding_text(summary: dict) -> str:
    parts = []
    parts.append(" ".join(summary.get("searchable_keywords", [])))
    for project in summary.get("major_projects", []):
        parts.append(project.get("project_name", ""))
        parts.append(project.get("brief_context", ""))
    return " \n ".join([p for p in parts if p])


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a PDF into Qdrant with summary and chunk layers.")
    parser.add_argument("--pdf-path", required=True, help="Path to the PDF file to ingest.")
    parser.add_argument("--qdrant-host", default="127.0.0.1", help="Qdrant host.")
    parser.add_argument("--qdrant-port", type=int, default=6333, help="Qdrant port.")
    parser.add_argument("--collection-summaries", default="document_summaries", help="Qdrant summary collection name.")
    parser.add_argument("--collection-chunks", default="document_chunks", help="Qdrant chunk collection name.")
    args = parser.parse_args()

    print(f"Parsing PDF: {args.pdf_path}")
    pages = parse_pdf(args.pdf_path)
    if not pages:
        raise RuntimeError(
            "No text could be extracted from the PDF. "
            "If the file contains scanned pages, install pdf2image/pytesseract or provide a text-based PDF."
        )

    document_text = "\n\n".join(page.text for page in pages)
    print("Extracting structured document summary...")
    summary = extract_document_summary(document_text)

    parent_doc_id = uuid.uuid4().hex
    print(f"Generated summary parent_doc_id={parent_doc_id}")

    qdrant = QdrantIndexer(host=args.qdrant_host, port=args.qdrant_port)
    qdrant.create_collections(summaries_collection=args.collection_summaries, chunks_collection=args.collection_chunks)

    summary_payload = {
        "parent_doc_id": parent_doc_id,
        "core_info": summary.core_info,
        "key_personnel": summary.key_personnel,
        "major_projects": summary.major_projects,
        "finance_and_admin": summary.finance_and_admin,
        "searchable_keywords": summary.searchable_keywords,
    }
    summary_text = build_summary_embedding_text(summary_payload)
    summary_vector = embed_texts([summary_text])[0]
    qdrant.upload_summary(summary_id=parent_doc_id, payload=summary_payload, vector=summary_vector, collection_name=args.collection_summaries)
    print(f"Stored summary in collection {args.collection_summaries}.")

    print("Chunking raw text into child documents...")
    child_documents = chunk_documents(pages, parent_doc_id=parent_doc_id)
    if not child_documents:
        raise RuntimeError("No chunks were created from the PDF text.")
    chunk_records = qdrant.prepare_chunk_records(child_documents, parent_doc_id)
    chunk_vectors = embed_texts([record["text"] for record in chunk_records])
    qdrant.upload_chunks(chunks=chunk_records, vectors=chunk_vectors, collection_name=args.collection_chunks)
    print(f"Stored {len(chunk_records)} chunks in collection {args.collection_chunks}.")
    print("Ingestion complete.")


if __name__ == "__main__":
    main()
