"""Simulate NVIDIA EngineCore crashing MID-stream and prove the fallback recovers."""
import os
from pathlib import Path

# Load .env so keys are present
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import asyncio
import main


class FakeChunk:
    def __init__(self, d):
        self.delta = d


async def crashing_stream(messages, **kw):
    # Simulate NVIDIA EngineCore dying MID-stream: 2 chunks, then crash.
    yield FakeChunk("Ejentic offers ")
    yield FakeChunk("three core ")
    raise RuntimeError("simulated EngineCore crash mid-stream")


async def test():
    main._retry_astream_chat = crashing_stream
    chunks = []
    async for c in main.answer_stream(
        "What services does Ejentic AI offer?", "guest", "simtest"
    ):
        chunks.append(c)
    out = "".join(chunks)
    print("CHUNKS:", len(chunks))
    print("OUT:", (out[:300] + "...") if len(out) > 300 else out)
    ok = "recovered" in out and "Ejentic" in out and "simulated EngineCore" not in out
    print("VERDICT:", "RECOVERY OK" if ok else "FALLBACK FAILED")


asyncio.run(test())
