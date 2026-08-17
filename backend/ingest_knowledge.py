"""
Canonical, clearance-aware ingestion for the Ejentic RAG system.

This is THE ingestion process — the one repeatable step that turns a company's
knowledge into a securely-filterable vector index. It is deliberately the only
script that writes to Pinecone, so there is exactly one place where the
security-critical `clearance` tag is attached to every chunk.

WHY THIS EXISTS
---------------
The retrieval layer filters documents by a `clearance` metadata tag
(public | internal | executive). If a document is ingested WITHOUT that tag,
it becomes invisible to every non-executive user — the whole knowledge base
looks empty. So ingestion must *guarantee* the tag. This script validates it
up front and refuses to proceed if any record is mistagged.

REPEATABLE FOR ANY CLIENT
-------------------------
To onboard a new business, you only change the input JSON — not this code:

    [
      {"text": "...public marketing copy...",      "clearance": "public"},
      {"text": "...internal handbook...",           "clearance": "internal"},
      {"text": "...board-only financials...",       "clearance": "executive"}
    ]

Then run:  python ingest_knowledge.py            # clean rebuild (default)
           python ingest_knowledge.py --append   # add to existing index
           python ingest_knowledge.py --file acme_knowledge.json

See RUNBOOK.md for the full end-to-end process.
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
from llama_index.core import Document, VectorStoreIndex, StorageContext, Settings
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.embeddings.nvidia import NVIDIAEmbedding

load_dotenv()

# --- Config (matches the server's expectations in main.py) ------------------
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "ejentic-global")
NAMESPACE = os.environ.get("PINECONE_NAMESPACE", "ejentic-internal")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nvidia/nv-embedqa-e5-v5")
EMBED_DIM = 1024  # nv-embedqa-e5-v5 output dimension; must match the index metric/space

# The only clearance levels the retrieval layer understands. Any other value is
# a typo that would silently make a document unreachable — so we reject it.
VALID_CLEARANCE = {"public", "internal", "executive"}


def _fatal(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_and_validate(path: str) -> list[Document]:
    """Load the knowledge JSON and turn it into LlamaIndex Documents, failing
    loudly if any record is missing text or carries an unknown clearance tag."""
    if not os.path.exists(path):
        _fatal(f"knowledge file not found: {path}")

    with open(path, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            _fatal(f"{path} is not valid JSON: {e}")

    if not isinstance(data, list) or not data:
        _fatal(f"{path} must be a non-empty JSON array of objects")

    documents: list[Document] = []
    counts = {lvl: 0 for lvl in VALID_CLEARANCE}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            _fatal(f"record #{i} is not an object")
        text = (item.get("text") or "").strip()
        clearance = (item.get("clearance") or "").strip().lower()
        if not text:
            _fatal(f"record #{i} has empty 'text'")
        if clearance not in VALID_CLEARANCE:
            _fatal(
                f"record #{i} has invalid clearance {clearance!r}; "
                f"must be one of {sorted(VALID_CLEARANCE)}"
            )
        counts[clearance] += 1
        documents.append(
            Document(
                text=text,
                # `clearance` is the security tag the retriever filters on.
                # `source` gives citations a readable [Source N] ref.
                metadata={
                    "clearance": clearance,
                    "source": os.path.basename(path),
                    "doc_id": i,
                },
                # Belt-and-suspenders: never let clearance leak into the text
                # the LLM sees, and never let it be dropped from retrieval.
                excluded_llm_metadata_keys=["doc_id", "source"],
                excluded_embed_metadata_keys=["clearance", "doc_id", "source"],
            )
        )

    print(f"Validated {len(documents)} records: "
          + ", ".join(f"{n} {lvl}" for lvl, n in counts.items()))
    return documents


def ensure_index(pc: Pinecone):
    """Create the index if it doesn't exist (cosine, dim 1024), else reuse it."""
    if INDEX_NAME not in pc.list_indexes().names():
        print(f"Creating Pinecone index '{INDEX_NAME}' (dim={EMBED_DIM}, cosine)...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
    else:
        print(f"Index '{INDEX_NAME}' already exists — reusing it.")
    return pc.Index(INDEX_NAME)


def reset_namespace(pinecone_index) -> None:
    """Delete all vectors in our namespace so a rebuild is clean and idempotent.
    Safe if the namespace doesn't exist yet (fresh index)."""
    try:
        pinecone_index.delete(delete_all=True, namespace=NAMESPACE)
        print(f"Cleared namespace '{NAMESPACE}' for a clean rebuild.")
    except Exception as e:
        # 404 = namespace not created yet; anything else we surface but continue.
        print(f"(namespace '{NAMESPACE}' not cleared — likely empty/new: {e})")


def ingest(path: str, append: bool) -> None:
    if not PINECONE_API_KEY or not NVIDIA_API_KEY:
        _fatal("PINECONE_API_KEY and NVIDIA_API_KEY must be set in .env")

    # Embeddings must match the model the SERVER queries with, or scores are junk.
    Settings.embed_model = NVIDIAEmbedding(model=EMBED_MODEL, api_key=NVIDIA_API_KEY)
    Settings.chunk_size = 512  # records are short; this keeps each one whole

    documents = load_and_validate(path)

    print("Connecting to Pinecone...")
    pc = Pinecone(api_key=PINECONE_API_KEY)
    pinecone_index = ensure_index(pc)

    if not append:
        reset_namespace(pinecone_index)

    vector_store = PineconeVectorStore(
        pinecone_index=pinecone_index, namespace=NAMESPACE
    )
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    print(f"Embedding + upserting {len(documents)} documents into "
          f"'{INDEX_NAME}' / namespace '{NAMESPACE}' via NVIDIA NIM...")
    VectorStoreIndex.from_documents(documents, storage_context=storage_context)

    print("Ingestion complete. Run  python verify_clearance.py  to confirm "
          "each role sees the right slice.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clearance-aware RAG ingestion.")
    parser.add_argument(
        "--file", default="ejentic_knowledge.json",
        help="Path to the clearance-tagged knowledge JSON (default: ejentic_knowledge.json)",
    )
    parser.add_argument(
        "--append", action="store_true",
        help="Add to the existing namespace instead of wiping it first "
             "(default: clean rebuild).",
    )
    args = parser.parse_args()
    ingest(args.file, append=args.append)
