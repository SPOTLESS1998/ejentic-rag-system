"""
Three-role verification — proves clearance isolation end to end.

This is both a test and a demo script. It asks the SAME questions as three
different roles (guest, employee, executive) and shows that each role only gets
answers to material it is cleared for. When you show this to a client, this is
the "watch the security boundary work" moment.

Expected result grid (with the shipped ejentic_knowledge.json):

    question \\ role        guest      employee   executive
    ----------------------------------------------------------
    public  (services)     ANSWER     ANSWER     ANSWER
    internal(Project Delta) escalate  ANSWER     ANSWER
    executive(Q2 revenue)   escalate  escalate   ANSWER

"escalate" = the retriever found nothing the role is cleared to see, so the
confidence gate refuses to answer (and spends ~0 answer tokens).

Run:  python verify_clearance.py
"""
import asyncio

import main  # importing boots the same pipeline the server uses
from database import init_db


PROBES = [
    ("public",    "What services does Ejentic AI offer?"),
    ("internal",  "What is Project Delta?"),
    ("executive", "What was the Q2 revenue and the authorized acquisition bid?"),
]
ROLES = ["guest", "employee", "executive"]


def _verdict(text: str, gated: bool) -> str:
    if gated or main.ESCALATION_LINE.split(".")[0] in text:
        return "escalate"
    return "ANSWER"


async def run():
    # Running standalone does NOT trigger FastAPI's startup hook, so we run the
    # DB migration ourselves. Otherwise the per-query audit-log writes fail on an
    # older DB schema (the queries still work, but the log is noisy).
    await init_db()

    print("\nClearance isolation matrix (question tier x user role)\n")
    header = f"{'question tier':<12} | " + " | ".join(f"{r:^9}" for r in ROLES)
    print(header)
    print("-" * len(header))

    details = []
    for tier, question in PROBES:
        cells = []
        for role in ROLES:
            text, meter, gated, saved = await main.answer_once(question, role, "verify")
            cells.append(_verdict(text, gated))
            details.append((tier, role, text, meter.as_dict(), gated, saved))
        print(f"{tier:<12} | " + " | ".join(f"{c:^9}" for c in cells))

    print("\n--- sample answers (what each role actually receives) ---")
    for tier, role, text, tokens, gated, saved in details:
        # Show the interesting ones: the executive answering its own tier, and a
        # lower role being correctly refused.
        if (tier == "executive" and role == "executive") or \
           (tier == "executive" and role == "guest") or \
           (tier == "internal" and role == "employee"):
            print(f"\n[{role} asks {tier} question]")
            print(f"  -> {text[:220].strip()}")
            print(f"  tokens={tokens} gated={gated} saved={saved}")


if __name__ == "__main__":
    asyncio.run(run())
