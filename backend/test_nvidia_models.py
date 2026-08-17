import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.environ.get("NVIDIA_API_KEY"),
    base_url="https://integrate.api.nvidia.com/v1"
)

models = client.models.list()
print("Available Models:")
for model in models:
    if "embed" in model.id.lower() or "nv" in model.id.lower():
        print(model.id)
