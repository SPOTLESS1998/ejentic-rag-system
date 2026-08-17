import os
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
index_name = "mtn-rag"
if index_name in pc.list_indexes().names():
    print(f"Deleting existing index {index_name} to recreate with new dimensions...")
    pc.delete_index(index_name)
