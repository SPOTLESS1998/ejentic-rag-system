"""Offline tests for POST /upload — path traversal and per-caller scoping.

TWO REAL BUGS THIS PINS
-----------------------
1. ARBITRARY FILE WRITE. The old code did `os.path.join(upload_dir, file.filename)`
   with no sanitisation. A filename of "../../../../tmp/pwned.pdf" resolved outside
   the upload directory, from an endpoint that had no authentication either.

2. ONE CALLER'S PDF ANSWERED EVERYONE'S QUESTIONS. The parsed index was a
   process-wide global, so any upload became retrievable context for every other
   caller — a cross-user leak that no clearance filter would catch, because the
   caller's own document isn't clearance-tagged.

Run:  venv/bin/python tests/test_upload_safety.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, import_main, section  # noqa: E402

from fastapi import HTTPException  # noqa: E402

m = import_main()

UPLOAD_DIR = os.path.join(tempfile.mkdtemp(prefix="rag_upload_test_"), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ROOT = os.path.realpath(UPLOAD_DIR)

# ---------------------------------------------------------------------------
section("traversal names cannot escape the upload directory")
# ---------------------------------------------------------------------------
TRAVERSALS = [
    "../../../../tmp/pwned.pdf",
    "../../etc/passwd.pdf",
    "..\\..\\..\\windows\\system32\\evil.pdf",
    "/etc/cron.d/pwned.pdf",
    "/tmp/absolute.pdf",
    "....//....//pwned.pdf",
    "subdir/../../pwned.pdf",
    "a/b/c/../../../../../../pwned.pdf",
    "%2e%2e%2fpwned.pdf",
    "..%252f..%252fpwned.pdf",
    "../pwned.pdf",
    ".../.../pwned.pdf",
]
for name in TRAVERSALS:
    try:
        path = m._safe_upload_path(UPLOAD_DIR, name)
        inside = os.path.realpath(path).startswith(ROOT + os.sep)
        check(f"{name!r} stays inside the upload dir", inside, os.path.realpath(path))
        # The stored name must contain NONE of the caller's text — the whole point
        # is that no caller-supplied character reaches the filesystem.
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]
        check(f"{name!r} contributes nothing to the stored name",
              all(c in "0123456789abcdef" for c in stem), base)
    except HTTPException as e:
        # Rejecting outright is equally correct (a bad extension, say).
        check(f"{name!r} is rejected outright", e.status_code == 400, str(e.detail))

# A file that truly landed outside would be the bug; assert the directory is clean.
check("nothing was written outside the upload dir during the traversal tests",
      not os.path.exists("/tmp/pwned.pdf"))

# ---------------------------------------------------------------------------
section("the stored name is generated, never the caller's")
# ---------------------------------------------------------------------------
p1 = m._safe_upload_path(UPLOAD_DIR, "quarterly report.pdf")
p2 = m._safe_upload_path(UPLOAD_DIR, "quarterly report.pdf")
check("the same original name yields DIFFERENT stored names (no overwrite)", p1 != p2)
check("the stored name keeps the validated extension", p1.endswith(".pdf"))
check("the stored stem is hex", all(c in "0123456789abcdef"
                                   for c in os.path.splitext(os.path.basename(p1))[0]))
check("the stored name is long enough to be unguessable",
      len(os.path.splitext(os.path.basename(p1))[0]) >= 32,
      os.path.basename(p1))
check("the original name does not appear in the path",
      "quarterly" not in p1 and "report" not in p1, p1)

for tricky in ("no-extension", ".hidden", "trailing.", "spaces   .pdf",
               "unicode-日本語.pdf", "semi;colon.pdf", "pipe|.pdf", "nul\x00.pdf"):
    try:
        path = m._safe_upload_path(UPLOAD_DIR, tricky)
        check(f"{tricky!r} produced a safe path inside the dir",
              os.path.realpath(path).startswith(ROOT + os.sep), path)
    except HTTPException as e:
        check(f"{tricky!r} was rejected", e.status_code == 400, str(e.detail)[:60])

# ---------------------------------------------------------------------------
section("extension allow-list")
# ---------------------------------------------------------------------------
for ext in sorted(m.ALLOWED_UPLOAD_EXT):
    try:
        m._safe_upload_path(UPLOAD_DIR, f"doc{ext}")
        check(f"{ext} is accepted", True)
    except HTTPException as e:
        check(f"{ext} is accepted", False, str(e.detail))

# Executables and scripts must not be storable, even though we only ever read them
# back with a document parser — a writable .py or .sh in a served directory is a
# foothold, and "we don't execute it" is one refactor away from being untrue.
for ext in (".py", ".sh", ".exe", ".php", ".html", ".svg", ".zip", ".so", ".dylib",
            ".json", ".yaml", ".env", ""):
    try:
        m._safe_upload_path(UPLOAD_DIR, f"payload{ext}")
        check(f"{ext or '(no extension)'} is rejected", False, "it was accepted")
    except HTTPException as e:
        check(f"{ext or '(no extension)'} is rejected", e.status_code == 400)

try:
    m._safe_upload_path(UPLOAD_DIR, "invoice.pdf.sh")
    check("'invoice.pdf.sh' is rejected (the .sh is what counts)", False, "accepted")
except HTTPException as e:
    check("'invoice.pdf.sh' is rejected (the .sh is what counts)", e.status_code == 400)

try:
    p = m._safe_upload_path(UPLOAD_DIR, "invoice.sh.pdf")
    check("'invoice.sh.pdf' is accepted and stored as .pdf", p.endswith(".pdf"))
except HTTPException:
    check("'invoice.sh.pdf' is accepted and stored as .pdf", False, "rejected")

check("an uppercase extension is normalised",
      m._safe_upload_path(UPLOAD_DIR, "DOC.PDF").endswith(".pdf"))

# ---------------------------------------------------------------------------
section("size cap exists and is sane")
# ---------------------------------------------------------------------------
check("MAX_UPLOAD_BYTES is set", isinstance(m.MAX_UPLOAD_BYTES, int))
check("MAX_UPLOAD_BYTES is a real limit (not 0 or negative)", m.MAX_UPLOAD_BYTES > 0)
check("MAX_UPLOAD_BYTES is bounded (not effectively unlimited)",
      m.MAX_UPLOAD_BYTES <= 200 * 1024 * 1024, f"{m.MAX_UPLOAD_BYTES} bytes")

# ---------------------------------------------------------------------------
section("uploads are scoped per caller, not process-wide")
# ---------------------------------------------------------------------------
m._uploads.clear()


class FakeIndex:
    """Stand-in for a VectorStoreIndex so no embedding model is needed."""

    def __init__(self, label):
        self.label = label


tok_a = m._remember_upload(FakeIndex("caller-A's contract"), "a.pdf")
tok_b = m._remember_upload(FakeIndex("caller-B's payslip"), "b.pdf")

check("each upload gets its own token", tok_a != tok_b)
check("a token is long enough to be unguessable", len(tok_a) >= 24, f"{len(tok_a)} chars")
check("caller A's token returns caller A's index",
      m._upload_index(tok_a).label == "caller-A's contract")
check("caller B's token returns caller B's index",
      m._upload_index(tok_b).label == "caller-B's payslip")

# THE LEAK THIS PREVENTS: with no token, a query must see NO upload at all.
check("no token means no upload is folded in", m._upload_index("") is None)
check("None means no upload is folded in", m._upload_index(None) is None)
check("whitespace means no upload is folded in", m._upload_index("   ") is None)
check("an unknown token means no upload (not someone else's)",
      m._upload_index("some-other-token") is None)
check("a token prefix does not match", m._upload_index(tok_a[:10]) is None)
check("a token with junk appended does not match", m._upload_index(tok_a + "x") is None)
check("caller B cannot reach A's document by guessing a variant",
      m._upload_index(tok_a.upper()) is None or tok_a.upper() == tok_a)

# ---------------------------------------------------------------------------
section("the upload store is bounded (an upload flood cannot exhaust memory)")
# ---------------------------------------------------------------------------
m._uploads.clear()
tokens = [m._remember_upload(FakeIndex(f"doc{i}"), f"{i}.pdf")
          for i in range(m.MAX_UPLOADS + 5)]
check(f"the store holds at most MAX_UPLOADS ({m.MAX_UPLOADS})",
      len(m._uploads) == m.MAX_UPLOADS, f"holds {len(m._uploads)}")
check("the oldest uploads were evicted", m._upload_index(tokens[0]) is None)
check("the newest upload is still present",
      m._upload_index(tokens[-1]) is not None)

# LRU, not FIFO: touching an entry should keep it alive.
m._uploads.clear()
keep = m._remember_upload(FakeIndex("keep me"), "keep.pdf")
for i in range(m.MAX_UPLOADS - 1):
    m._remember_upload(FakeIndex(f"filler{i}"), f"f{i}.pdf")
m._upload_index(keep)                      # touch it
m._remember_upload(FakeIndex("newest"), "new.pdf")   # forces one eviction
check("a recently-used upload survives eviction (LRU, not FIFO)",
      m._upload_index(keep) is not None)

# ---------------------------------------------------------------------------
section("retrieval only folds in a document whose token was presented")
# ---------------------------------------------------------------------------
# Asserting the wiring, not just the store: retrieve_and_rerank must consult
# _upload_index(upload_token) rather than any module-level index.
import inspect  # noqa: E402

src = inspect.getsource(m.retrieve_and_rerank)
check("retrieve_and_rerank looks the upload up BY TOKEN", "_upload_index(upload_token)" in src)
check("retrieve_and_rerank takes an upload_token parameter",
      "upload_token" in inspect.signature(m.retrieve_and_rerank).parameters)
check("no process-wide pdf_index global survives",
      not hasattr(m, "pdf_index"),
      "a module-level pdf_index is the shared-state bug itself")

for fn in (m.answer_once, m.answer_stream):
    check(f"{fn.__name__} passes an upload_token through",
          "upload_token" in inspect.signature(fn).parameters)

finish("test_upload_safety")
