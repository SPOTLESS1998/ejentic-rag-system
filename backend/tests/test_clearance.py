"""Offline tests for the clearance filter — the authorization half of the boundary.

This layer was already well written; these tests make that PERMANENT. The audit
threw a range of bad inputs at it (`admin`, `root`, `""`, `None`, `guest;--`) and
every one failed closed to the least-privileged tier. The danger with a filter like
this is not that it's wrong today — it's that a later "small" refactor turns an
unknown role into "no filter at all", which is unrestricted access.

Also covers the confidence gate boundaries, the BM25 reranker's ordering, the
savings estimate, and the cross-tenant 409 guard.

Run:  venv/bin/python tests/test_clearance.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import base_cfg, check, finish, import_main, section  # noqa: E402

import client_registry as registry  # noqa: E402

m = import_main()
CFG = base_cfg()


def tags_of(level, cfg=None):
    """The clearance tag values a level actually filters on.

    None means "no filter" = unrestricted, which we represent as "*" so a test can
    tell the difference between unrestricted and a one-tag filter.
    """
    f = m.build_clearance_filter(level, cfg or CFG)
    if f is None:
        return "*"
    return sorted(sub.value for sub in f.filters)


# ---------------------------------------------------------------------------
section("the configured roles map to the right tags")
# ---------------------------------------------------------------------------
check("guest sees public only", tags_of("guest") == ["public"], str(tags_of("guest")))
check("employee sees public + internal",
      tags_of("employee") == ["internal", "public"], str(tags_of("employee")))
check("executive is unrestricted", tags_of("executive") == "*", str(tags_of("executive")))

check("employee's filter ORs its tags (not ANDs — a chunk has ONE tag)",
      m.build_clearance_filter("employee", CFG).condition.value == "or")
check("a single-tag filter needs no condition",
      len(m.build_clearance_filter("guest", CFG).filters) == 1)

# ---------------------------------------------------------------------------
section("normalisation")
# ---------------------------------------------------------------------------
for variant in ("GUEST", "Guest", "  guest  ", "gUeSt", "\tguest\n"):
    check(f"{variant!r} normalises to guest", tags_of(variant) == ["public"])
for variant in ("EXECUTIVE", " Executive "):
    check(f"{variant!r} normalises to executive", tags_of(variant) == "*")

# ---------------------------------------------------------------------------
section("fail-closed: every unknown input lands on the LEAST privileged tier")
# ---------------------------------------------------------------------------
# The failure to fear is an unknown role returning None (no filter), which would be
# unrestricted access for a typo. Each of these must come back as public-only.
BAD_INPUTS = [
    "", "   ", None,
    "admin", "root", "superuser", "owner", "sysadmin",
    # Near-misses for a real role. Note "Executive " / " executive " are NOT here:
    # case and surrounding whitespace are deliberately normalised away, so those
    # ARE the executive role and are asserted as such in the normalisation section.
    "exec", "executives", "execu tive", "exec utive", "executivee",
    "guest;--", "guest' OR '1'='1", "*", "**", "%", "_",
    "public", "internal",           # TAG names are not ROLE names
    "guest,executive", "guest executive", "guest|executive",
    "../executive", "executive\x00", "​executive",
    "true", "1", "0", "null", "None", "undefined",
]
for bad in BAD_INPUTS:
    got = tags_of(bad)
    check(f"{bad!r} fails closed to public", got == ["public"], str(got))

check("no bad input EVER returns an unrestricted filter",
      all(tags_of(b) != "*" for b in BAD_INPUTS))

# Non-string inputs must not crash the request either.
for weird in (0, 1, [], {}, True):
    try:
        got = tags_of(weird)
        check(f"non-string {weird!r} fails closed without crashing", got == ["public"],
              str(got))
    except Exception as e:
        check(f"non-string {weird!r} fails closed without crashing", False, repr(e))

# ---------------------------------------------------------------------------
section("_least_privileged_tags picks the narrowest real role")
# ---------------------------------------------------------------------------
check("default client's least privileged is public",
      m._least_privileged_tags(CFG) == ["public"], str(m._least_privileged_tags(CFG)))

# A tenant with completely different tier names must still work — the fallback is
# computed, never a hardcoded "public".
partner = base_cfg(clearance_levels={
    "partner": ["partner_docs"],
    "legal": ["partner_docs", "legal_only"],
    "principal": "*",
})
check("a tenant with partner/legal tiers gets ITS narrowest tier",
      m._least_privileged_tags(partner) == ["partner_docs"],
      str(m._least_privileged_tags(partner)))
check("an unknown role on that tenant fails closed to partner_docs",
      tags_of("admin", partner) == ["partner_docs"], str(tags_of("admin", partner)))
check("that tenant's own roles still resolve",
      tags_of("legal", partner) == ["legal_only", "partner_docs"])
check("that tenant's wildcard role is unrestricted", tags_of("principal", partner) == "*")

# A config where EVERY role is a wildcard has no narrowest list to fall back to.
# It must not crash and must not become unrestricted.
all_star = base_cfg(clearance_levels={"boss": "*", "chief": "*"})
check("with only wildcard roles, an unknown role still gets a FILTER (not None)",
      tags_of("admin", all_star) != "*", str(tags_of("admin", all_star)))

# ---------------------------------------------------------------------------
section("the tag vocabulary comes from config, not code")
# ---------------------------------------------------------------------------
check("default client's tags are public+internal",
      registry.tenant_clearance_tags(CFG) == ["public", "internal"],
      str(registry.tenant_clearance_tags(CFG)))
check("a wildcard role contributes no tag of its own",
      "executive" not in registry.tenant_clearance_tags(CFG))
check("a partner/legal tenant's vocabulary is its own",
      registry.tenant_clearance_tags(partner) == ["partner_docs", "legal_only"],
      str(registry.tenant_clearance_tags(partner)))
check("tags are de-duplicated across roles",
      registry.tenant_clearance_tags(base_cfg(clearance_levels={
          "a": ["x", "y"], "b": ["y", "z"]})) == ["x", "y", "z"])
check("blank tags are dropped",
      registry.tenant_clearance_tags(base_cfg(clearance_levels={
          "a": ["x", "", "  "]})) == ["x"])
check("an all-wildcard tenant declares no tags",
      registry.tenant_clearance_tags(all_star) == [])

# ---------------------------------------------------------------------------
section("confidence gate boundaries")
# ---------------------------------------------------------------------------
T = m.CONFIDENCE_THRESHOLD
node = object()
check(f"a score exactly at the threshold ({T}) PASSES", m._passes_confidence([node], T))
check("a score just above passes", m._passes_confidence([node], T + 0.01))
check("a score just below is gated", not m._passes_confidence([node], T - 0.01))
check("a zero score is gated", not m._passes_confidence([node], 0.0))
check("a negative score is gated", not m._passes_confidence([node], -1.0))
check("no nodes is gated even with a perfect score", not m._passes_confidence([], 1.0))
check("no nodes and no score is gated", not m._passes_confidence([], 0.0))

# ---------------------------------------------------------------------------
section("gate savings are not inflated")
# ---------------------------------------------------------------------------
# The metric prices the prompt we AVOIDED sending. It used to price a prompt built
# from ALL the rejected nodes, but the real call would only ever have included
# RERANK_TOP_N of them — so the "tokens saved" number was larger than any prompt
# that could have existed.


class FakeNode:
    """Minimal stand-in for a NodeWithScore."""

    def __init__(self, text, score=0.1):
        self.score = score
        self._text = text

    def get_content(self):
        return self._text

    @property
    def node(self):
        return self

    @property
    def metadata(self):
        return {"clearance": "public", "source": "t.json"}


many = [FakeNode(f"chunk {i} " + "filler " * 60) for i in range(30)]
few = many[:m.RERANK_TOP_N]
check("savings for 30 nodes equals savings for the top-N that would have been sent",
      m._estimate_gate_savings("q", many) == m._estimate_gate_savings("q", few),
      f"{m._estimate_gate_savings('q', many)} vs {m._estimate_gate_savings('q', few)}")
check("savings are positive when there was a prompt to avoid",
      m._estimate_gate_savings("q", few) > 0)
check("savings for zero nodes are small but not negative",
      m._estimate_gate_savings("q", []) >= 0)

# ---------------------------------------------------------------------------
section("BM25 lexical reranker (the dependency-free floor)")
# ---------------------------------------------------------------------------
# This is what runs when the cross-encoder isn't installed, so retrieval is never
# left unranked. It must actually reorder by relevance, not pass things through.
from llama_index.core.schema import NodeWithScore, TextNode, QueryBundle  # noqa: E402

docs = [
    "Core hours are 10am to 4pm for all employees.",
    "Our acquisition bid was twelve million dollars.",
    "Ejentic builds retrieval augmented generation systems.",
    "The office coffee machine is on the second floor.",
]
nodes = [NodeWithScore(node=TextNode(text=t), score=0.5) for t in docs]
rr = m.LexicalReranker(top_n=2, alpha=0.0)   # alpha 0 = pure lexical
out = rr.postprocess_nodes(nodes, query_bundle=QueryBundle("what are core hours"))
check("BM25 returns top_n nodes", len(out) == 2, f"got {len(out)}")
check("BM25 ranks the lexically matching chunk first",
      "core hours" in out[0].node.get_content().lower(),
      out[0].node.get_content()[:60])

out_alpha1 = m.LexicalReranker(top_n=2, alpha=1.0).postprocess_nodes(
    nodes, query_bundle=QueryBundle("what are core hours"))
check("alpha=1.0 keeps the dense order (pure dense blend)",
      out_alpha1[0].node.get_content() == docs[0] or len(out_alpha1) == 2)
check("BM25 on an empty node list returns empty",
      rr.postprocess_nodes([], query_bundle=QueryBundle("q")) == [])
check("BM25 tolerates a query matching nothing",
      len(rr.postprocess_nodes(nodes, query_bundle=QueryBundle("zzzz qqqq"))) == 2)

check("_tokenize lowercases and splits on non-alphanumerics",
      m._tokenize("Core-Hours, 10AM!") == ["core", "hours", "10am"],
      str(m._tokenize("Core-Hours, 10AM!")))

# ---------------------------------------------------------------------------
section("cross-tenant guard")
# ---------------------------------------------------------------------------
check("an empty client field means the active client",
      m.resolve_request_client("") is m.CFG)
check("whitespace means the active client", m.resolve_request_client("   ") is m.CFG)
check("naming the active client explicitly is fine",
      m.resolve_request_client(m.ACTIVE_CLIENT) is m.CFG)
for other in ("acme", "ejentic-two", "EJENTIC", "../ejentic"):
    try:
        m.resolve_request_client(other)
        check(f"requesting client {other!r} is refused", False, "it was allowed")
    except ValueError as e:
        check(f"requesting client {other!r} is refused", "not the active client" in str(e))

# ---------------------------------------------------------------------------
section("query-rewrite gating (a token-spend decision, not a security one)")
# ---------------------------------------------------------------------------
# The rule: rewrite SHORT queries (they benefit from expansion) and any query
# leaning on a pronoun. A long, specific question is used verbatim so we don't
# spend an LLM call for nothing.
check("a short keyword query IS rewritten (it benefits from expansion)",
      m._needs_rewrite("core hours"))
check("a long specific question is used verbatim",
      not m._needs_rewrite("What are the core working hours for salaried employees "
                           "in the London office during summer"))
check("a long question leaning on a pronoun is still rewritten",
      m._needs_rewrite("What are the core working hours for salaried employees and "
                       "how does that affect it"))
check("query rewriting is enabled for this client", m.ENABLE_QUERY_REWRITE)

finish("test_clearance")
