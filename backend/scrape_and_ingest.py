import os
from dotenv import load_dotenv
from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, StorageContext, Settings
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.embeddings.nvidia import NVIDIAEmbedding
from pinecone import Pinecone, ServerlessSpec

# Load environment variables
load_dotenv()

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "ejentic-global")

if not PINECONE_API_KEY or not NVIDIA_API_KEY:
    raise ValueError("PINECONE_API_KEY and NVIDIA_API_KEY must be set in .env")

# Configure LlamaIndex to use NVIDIA Embeddings
Settings.embed_model = NVIDIAEmbedding(model="nvidia/nv-embedqa-e5-v5", api_key=NVIDIA_API_KEY)
Settings.chunk_size = 256

def main():
    print("Loading Ejentic AI knowledge base from local data folder...")
    # Load documents from the data directory
    documents = SimpleDirectoryReader("data").load_data()
            
    if not documents:
        print("No documents were found in the data directory. Exiting.")
        return

    print("Initializing Pinecone...")
    pc = Pinecone(api_key=PINECONE_API_KEY)
    
    # Check if index exists, if not create it
    if INDEX_NAME not in pc.list_indexes().names():
        print(f"Creating Pinecone index '{INDEX_NAME}'...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=1024, # Dimension for NV-Embed-QA
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
    else:
        print(f"Index '{INDEX_NAME}' already exists. We will append to it.")
    
    print("Setting up vector store...")
    pinecone_index = pc.Index(INDEX_NAME)
    vector_store = PineconeVectorStore(pinecone_index=pinecone_index, namespace="ejentic-internal")
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    
    print("Generating embeddings and indexing documents via NVIDIA NIM...")
    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context
    )
    
    print("Ingestion complete!")

if __name__ == "__main__":
    main()
