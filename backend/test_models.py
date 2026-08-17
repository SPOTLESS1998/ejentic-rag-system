import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

try:
    client = genai.Client()
    for model in client.models.list():
        print(f"Model: {model.name}")
except Exception as e:
    print(f"Error: {e}")
