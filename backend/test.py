import sys
print("1. Starting imports...", flush=True)
from fastapi import FastAPI
print("2. Imported FastAPI", flush=True)
from llama_index.core import VectorStoreIndex
print("3. Imported llama_index", flush=True)
from llama_index.llms.nvidia import NVIDIA
print("4. Imported NVIDIA", flush=True)
from pinecone import Pinecone
print("5. Imported Pinecone", flush=True)
print("All imports successful!", flush=True)
