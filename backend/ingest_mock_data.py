import json
import os
from dotenv import load_dotenv
from pinecone import Pinecone
from llama_index.core import Document, VectorStoreIndex, StorageContext, Settings
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.embeddings.nvidia import NVIDIAEmbedding

# NOTE: superseded by ingest_knowledge.py (the canonical, validated ingestion
# path — see RUNBOOK.md). Kept for reference. The important fix vs. the original
# is wiring the vector store through a StorageContext so chunks actually persist
# to Pinecone; passing vector_store= to from_documents() builds an in-memory
# index that never reaches the database.

load_dotenv()

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "ejentic-global")

def ingest_data():
    print("Initializing Pinecone and Embedding Model...")
    Settings.embed_model = NVIDIAEmbedding(model="nvidia/nv-embedqa-e5-v5", api_key=NVIDIA_API_KEY)
    
    pc = Pinecone(api_key=PINECONE_API_KEY)
    pinecone_index = pc.Index(INDEX_NAME)
    vector_store = PineconeVectorStore(pinecone_index=pinecone_index, namespace="ejentic-internal")
    
    with open("ejentic_knowledge.json", "r") as f:
        data = json.load(f)
        
    documents = []
    for item in data:
        doc = Document(
            text=item["text"],
            metadata={"clearance": item["clearance"]}
        )
        documents.append(doc)
        
    print(f"Loaded {len(documents)} documents. Ingesting into Pinecone...")

    # Persist THROUGH a storage context, or the vectors never reach Pinecone.
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_documents(documents, storage_context=storage_context)
    
    print("Ingestion complete!")

if __name__ == "__main__":
    ingest_data()
