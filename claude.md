# Phase 1: PDF Parsing, Summary Extraction, and Two-Layer Storage

## Objective
We are building a "Parent-Child" (Summary-to-Chunk) ingestion pipeline. 
1. Parse the PDF.
2. Generate a structured "Executive Summary / Keyword Map" (Parent Node).
3. Chunk the raw text (Child Nodes).
4. Store both in the database, linking the Child Nodes to the Parent Node.

## Tech Stack for this Phase
- Python 3.10+
- `llama-index` (for document parsing and node structures)
- `llama-parse` (for high-quality PDF extraction)
- `qdrant-client` (Vector DB)
- `pydantic` (for structured data validation)

## Step-by-Step Execution

### Step 1: Setup & PDF Parsing
- Initialize the project structure.
- Create a function `parse_pdf(file_path: str) -> List[Document]`.
- Use `llama-parse` to extract the text. Ensure page numbers are captured in the metadata of every parsed page.

### Step 2: The "Layer 1" Extraction (The Keyword Map)
- Create a function `extract_document_summary(text: str) -> dict`.
- Use an LLM (via LlamaIndex `OpenAI` or similar) with a strict Pydantic output parser to extract the summary exactly like this structure:
  - `core_info`: {organization, meeting_title, date, status}
  - `key_personnel`: [list of names and roles]
  - `major_projects`: [list of projects and brief context]
  - `finance_and_admin`: [list of budgets, audits, HR decisions]
  - `searchable_keywords`: [list of 20-30 highly specific tags/entities]
- **CRITICAL:** Save this entire JSON structure as a "Parent Node" in our database. This is our "Map".

### Step 3: The "Layer 2" Chunking (The Raw Text)
- Take the raw parsed text from Step 1.
- Use LlamaIndex `SentenceSplitter` or `TokenTextSplitter` to chunk the text into manageable pieces (e.g., 512 tokens, 50 token overlap).
- **CRITICAL:** Every single chunk (Child Node) must have a metadata field called `parent_doc_id` that matches the ID of the Parent Node created in Step 2.

### Step 4: Database Storage (Qdrant)
- Initialize Qdrant.
- Create TWO collections (or use one collection with distinct payload filters, but two is cleaner for PoC):
  1. `document_summaries`: Stores the Layer 1 Parent Nodes (The Keyword Maps). Vectorize the `searchable_keywords` and `major_projects` text.
  2. `document_chunks`: Stores the Layer 2 Child Nodes (The Raw Text). Vectorize the actual text chunks.
- Ensure `parent_doc_id` is indexed in the `document_chunks` collection for fast lookups.

## Definition of Done for Phase 1
- [ ] The agent can pass a 120-page PDF and successfully generate the structured JSON summary (Layer 1).
- [ ] The raw text is successfully chunked (Layer 2).
- [ ] Both the Summary and the Chunks are visible in the local Qdrant dashboard (localhost:6333/dashboard).
- [ ] The Chunks correctly reference the Summary's Document ID.

*Agent: Please execute Steps 1 through 4. Provide the code for the extraction prompt and the database storage logic. Wait for my review before we move to Phase 2 (Querying).*