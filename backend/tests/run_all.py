"""Run every offline suite and exit non-zero if any of them fails.

Each file runs in its own PROCESS, deliberately: the suites patch module globals
(main.Settings, database's import-time DB path, the client registry cache), and
sharing one interpreter would let one suite's setup decide another's result.

    venv/bin/python tests/run_all.py

No network, no keys, no Pinecone. Every suite sets RAG_OFFLINE=1 via harness.py.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE.parent / "venv" / "bin" / "python"

SUITES = [
    ("test_auth.py", "the authentication boundary (the hole that had to be closed)"),
    ("test_clearance.py", "clearance filtering + the confidence gate"),
    ("test_upload_safety.py", "path traversal + per-caller upload scoping"),
    ("test_stream.py", "the duplicate-delta and prefix-leak stream bugs"),
    ("test_tenancy.py", "per-tenant data isolation (MULTITENANCY.md)"),
]

results = []
started = time.time()

for name, blurb in SUITES:
    print(f"\n{'=' * 70}\n  {name} — {blurb}\n{'=' * 70}")
    proc = subprocess.run([str(PY), str(HERE / name)], cwd=str(HERE.parent))
    results.append((name, proc.returncode))

elapsed = time.time() - started
failed = [n for n, rc in results if rc != 0]

print(f"\n{'=' * 70}")
print(f"  OFFLINE SUITE SUMMARY  ({elapsed:.1f}s)")
print(f"{'=' * 70}")
for name, rc in results:
    print(f"  {'PASS' if rc == 0 else 'FAIL'}  {name}")
if failed:
    print(f"\n  {len(failed)} suite(s) FAILED: {', '.join(failed)}")
else:
    print("\n  All suites passed.")
print(f"{'=' * 70}")

sys.exit(1 if failed else 0)
