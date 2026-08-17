import asyncio
from llama_index.core.agent import ReActAgent
from llama_index.core.tools import FunctionTool
from llama_index.core import Settings
from llama_index.llms.nvidia import NVIDIA
import os
from dotenv import load_dotenv

load_dotenv()
Settings.llm = NVIDIA(model="meta/llama-3.1-70b-instruct", api_key=os.environ.get("NVIDIA_API_KEY"))

def add_numbers(a: int, b: int) -> int:
    """Adds two numbers"""
    return a + b

tool = FunctionTool.from_defaults(fn=add_numbers)

async def main():
    agent = ReActAgent(tools=[tool], llm=Settings.llm, verbose=True, streaming=True)
    try:
        # Check if there is a stream_run method
        if hasattr(agent, "stream_run"):
            print("Found stream_run method!")
            response = await agent.stream_run(user_msg="What is 5 + 7?")
            async for token in response:
                print(token)
        else:
            # Maybe run() returns an async generator if streaming=True?
            response = await agent.run(user_msg="What is 5 + 7?")
            print("run() returned type:", type(response))
            if hasattr(response, "__aiter__"):
                async for chunk in response:
                    print("CHUNK:", chunk)
            else:
                print("run() result is not async iterable.")
    except Exception as e:
        print("Async run failed:", e)

if __name__ == "__main__":
    asyncio.run(main())
