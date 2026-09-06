"""Offline tests for the two user-visible streaming bugs.

BUG 1 — DUPLICATED ANSWERS. `_retry_astream_chat` yielded deltas as they arrived
but restarted the generator on a transient failure, re-sending text the client
already had. Reproduced during the audit with a fake LLM: the user saw
'Hello worldHello world!'. A stream is not replayable once one token is out.

BUG 2 — INVERTED PREFIX CHECK. The old inline code asked
`buffer.startswith("assistant")`. For a PARTIAL buffer that is the wrong
direction: with buffer="A" it is False, so the code decided "not a marker" and
flushed — meaning a real `assistant:` split across deltas leaked to the user. The
right question is whether the MARKER starts with the buffer.

Both are mutation-verified: the comments name exactly what to break to see the
matching assertions go red.

Run:  venv/bin/python tests/test_stream.py
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, import_main, section  # noqa: E402

m = import_main()


class FakeChunk:
    """What LlamaIndex hands back per streamed token."""

    def __init__(self, delta):
        self.delta = delta


class Transient(Exception):
    """An error _is_transient() recognises, so the retry path engages."""

    def __str__(self):
        return "503 Service Unavailable"


class FakeLLM:
    """An LLM that fails partway through the stream a fixed number of times."""

    def __init__(self, deltas, fail_after=None, fail_times=0):
        self.deltas = deltas
        self.fail_after = fail_after     # emit this many deltas, then raise
        self.fail_times = fail_times
        self.attempts = 0

    async def astream_chat(self, messages):
        self.attempts += 1
        failing = self.fail_times > 0
        if failing:
            self.fail_times -= 1

        async def gen():
            for i, d in enumerate(self.deltas):
                if failing and self.fail_after is not None and i >= self.fail_after:
                    raise Transient()
                yield FakeChunk(d)
        return gen()


class FakeSettings:
    """Stands in for llama_index's global Settings, holding just our fake LLM.

    We swap `main.Settings` rather than assigning `Settings.llm`, for two reasons
    that both bite in offline mode: the real setter asserts `isinstance(llm, LLM)`
    and rejects a stub, and merely READING `Settings.llm` makes LlamaIndex resolve a
    default OpenAI model — which raises for a missing OPENAI_API_KEY and would make
    this suite depend on a key it has no business needing.
    """

    def __init__(self, llm):
        self.llm = llm


async def collect(llm, attempts=3):
    """Drive _retry_astream_chat against a fake LLM and gather what a client sees."""
    saved_settings = m.Settings
    m.Settings = FakeSettings(llm)

    out = []
    err = None
    # No real sleeping: the retry path waits 2s then 4s between attempts.
    saved_sleep = asyncio.sleep

    async def no_sleep(_s):
        return None
    asyncio.sleep = no_sleep
    try:
        async for chunk in m._retry_astream_chat([], attempts=attempts):
            out.append(chunk.delta)
    except Exception as e:
        err = e
    finally:
        asyncio.sleep = saved_sleep
        m.Settings = saved_settings
    return "".join(out), err, llm.attempts


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
section("BUG 1: a stream that already emitted must NOT be replayed")
# ---------------------------------------------------------------------------
# MUTATION: in _retry_astream_chat, change the guard
#   `if emitted or attempt >= attempts or not _is_transient(e): raise`
# back to `if attempt >= attempts or not _is_transient(e): raise`
# and the next two assertions go red with 'Hello worldHello world!'.
llm = FakeLLM(["Hello ", "world", "!"], fail_after=2, fail_times=1)
text, err, attempts = run(collect(llm))

check("a mid-stream failure does not duplicate delivered text",
      text == "Hello world", f"got {text!r}")
check("no word appears twice", text.count("Hello") <= 1 and text.count("world") <= 1,
      f"got {text!r}")
check("the failure is re-raised so the caller can recover",
      isinstance(err, Exception), f"err={err!r}")
check("it did NOT restart the stream", attempts == 1, f"{attempts} attempts")

# A failure BEFORE any delta is safe to retry: nothing was delivered, so restarting
# is invisible to the client. This is the case the retry exists for.
llm = FakeLLM(["Hello ", "world", "!"], fail_after=0, fail_times=1)
text, err, attempts = run(collect(llm))
check("a failure before the first delta IS retried", attempts == 2, f"{attempts} attempts")
check("that retry delivers the full answer exactly once", text == "Hello world!",
      f"got {text!r}")
check("that retry raises nothing", err is None, repr(err))

# Two pre-delta failures, three attempts allowed: still recovers cleanly.
llm = FakeLLM(["A", "B"], fail_after=0, fail_times=2)
text, err, attempts = run(collect(llm))
check("it retries up to the attempt limit before the first delta",
      text == "AB" and err is None and attempts == 3, f"{text!r} err={err!r} n={attempts}")

# Exhausting the attempts must raise, not return a partial answer silently.
llm = FakeLLM(["A", "B"], fail_after=0, fail_times=5)
text, err, attempts = run(collect(llm))
check("exhausting the attempts raises", isinstance(err, Exception), repr(err))
check("nothing was emitted when every attempt failed pre-delta", text == "", repr(text))

# A clean stream must not retry at all.
llm = FakeLLM(["one ", "two"], fail_times=0)
text, err, attempts = run(collect(llm))
check("a healthy stream runs once and delivers everything",
      text == "one two" and attempts == 1 and err is None)

check("the retry guard checks `emitted`",
      "emitted" in inspect.getsource(m._retry_astream_chat),
      "without this flag there is nothing stopping a replay")

# ---------------------------------------------------------------------------
section("BUG 2: 'assistant:' split across deltas must not leak")
# ---------------------------------------------------------------------------
# MUTATION: in _could_be_marker_prefix, swap
#   `return _ASSISTANT_MARKER.startswith(probe)`
# for `return probe.startswith(_ASSISTANT_MARKER)`
# and the split-marker assertions below go red.
check("a single character that could start the marker is held",
      m._could_be_marker_prefix("a"))
check("the check is case-insensitive", m._could_be_marker_prefix("A"))
check("a longer partial is held", m._could_be_marker_prefix("assist"))
check("the full marker is still a prefix of itself",
      m._could_be_marker_prefix("assistant:"))
check("leading whitespace does not confuse it", m._could_be_marker_prefix("  as"))
check("an empty buffer is held (nothing to judge yet)", m._could_be_marker_prefix(""))

check("text that cannot become the marker is released", not m._could_be_marker_prefix("b"))
check("'The' is released", not m._could_be_marker_prefix("The"))
check("'assist me' is released (diverges after 'assist')",
      not m._could_be_marker_prefix("assist me"))
check("'assistants' is released", not m._could_be_marker_prefix("assistants"))


def feed(deltas):
    """Push deltas through the marker state machine like answer_stream does."""
    buffer, out, done = "", [], False
    for d in deltas:
        if not done:
            buffer, text, done = m._consume_prefix_delta(buffer, d)
            if text:
                out.append(text)
            continue
        out.append(d)
    if buffer:
        cleaned = m._strip_assistant_prefix(buffer)
        if cleaned:
            out.append(cleaned)
    return "".join(out)


# THE ACTUAL BUG: one character at a time is exactly how it leaked before.
check("a marker split one char at a time is stripped",
      feed(list("assistant: Hi there")) == "Hi there",
      repr(feed(list("assistant: Hi there"))))
check("a marker split into two deltas is stripped",
      feed(["assis", "tant: Hi"]) == "Hi", repr(feed(["assis", "tant: Hi"])))
check("a marker arriving whole in one delta is stripped",
      feed(["assistant: Hi"]) == "Hi", repr(feed(["assistant: Hi"])))
check("an uppercase marker is stripped",
      feed(["Assistant: Hi"]) == "Hi", repr(feed(["Assistant: Hi"])))
check("a marker with leading whitespace is stripped",
      feed(["  assistant: Hi"]) == "Hi", repr(feed(["  assistant: Hi"])))

# And the other half: real text must survive UNCHANGED, including text that starts
# with the same letters. Over-stripping would silently eat the answer's first word.
for text in ["Hello world", "A quick answer", "The answer is 42",
             "assistants are helpful", "assist with that", "Q2 revenue was $2.4M"]:
    got = feed(list(text))
    check(f"{text!r} passes through unchanged", got == text, repr(got))

check("a one-character answer survives", feed(["A"]) == "A", repr(feed(["A"])))
check("an empty stream yields nothing", feed([]) == "", repr(feed([])))
check("a stream of empty deltas yields nothing", feed(["", ""]) == "", repr(feed(["", ""])))

# The state machine's contract, checked directly.
buf, out, done = m._consume_prefix_delta("", "a")
check("an ambiguous first delta is buffered, not yielded",
      buf == "a" and out == "" and done is False, f"{buf!r} {out!r} {done}")
buf, out, done = m._consume_prefix_delta("", "b")
check("an unambiguous first delta is released immediately and checking stops",
      buf == "" and out == "b" and done is True, f"{buf!r} {out!r} {done}")
buf, out, done = m._consume_prefix_delta("assistant", ": Hi")
check("completing the marker strips it and stops checking",
      buf == "" and out == "Hi" and done is True, f"{buf!r} {out!r} {done}")

check("_strip_assistant_prefix leaves normal text alone",
      m._strip_assistant_prefix("Hello") == "Hello")
check("_strip_assistant_prefix strips the marker",
      m._strip_assistant_prefix("assistant: Hello") == "Hello")
check("_strip_assistant_prefix only strips a LEADING marker",
      m._strip_assistant_prefix("Ask the assistant: now") == "Ask the assistant: now")

# ---------------------------------------------------------------------------
section("dead parameters were removed from _retry_achat")
# ---------------------------------------------------------------------------
params = inspect.signature(m._retry_achat).parameters
check("_retry_achat takes messages + attempts only",
      set(params) == {"messages", "attempts"}, str(list(params)))
for dead in ("meter", "fallback_prompt_text"):
    check(f"the dead {dead!r} kwarg is gone", dead not in params)

finish("test_stream")
