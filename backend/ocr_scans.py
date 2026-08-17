"""
OCR the scanned (image-only) PDFs sitting in knowledge_sources/ so that
pdf_to_knowledge.py can read them like any other PDF.

WHY THIS IS A SEPARATE STEP
---------------------------
A scanned PDF is a photo of a page — there is no text inside to extract. OCR
(optical character recognition) reads the text out of the image and adds an
invisible text layer, making the PDF searchable. OCR needs a system tool
(installed once with `brew install ocrmypdf`), so it's kept separate from the
core, dependency-light pipeline.

WHAT THIS DOES (safe + idempotent)
----------------------------------
- Looks only inside knowledge_sources/{public,internal,executive}/.
- For each PDF, checks whether it already has real text. If it does, it's LEFT
  UNTOUCHED (nothing to do).
- If it has no text (a scan), it runs OCR and replaces the file with a version
  that has a text layer. The page still looks identical — it's just searchable.
- Re-running is safe: already-OCR'd files are skipped.

Prereq (one-time):  brew install ocrmypdf
Run:                python ocr_scans.py
                    python ocr_scans.py --src ./acme_docs
Then:               python pdf_to_knowledge.py
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from pypdf import PdfReader

VALID_CLEARANCE = ["public", "internal", "executive"]
MIN_CHARS = 20  # same threshold pdf_to_knowledge.py uses to call a PDF "scanned"


def _has_text(path: str) -> bool:
    """True if the PDF already contains extractable text (so it doesn't need OCR)."""
    try:
        reader = PdfReader(path)
        text = "".join((p.extract_text() or "") for p in reader.pages)
        return len(text.strip()) >= MIN_CHARS
    except Exception:
        return False  # unreadable as text -> let OCR try to fix it


def _ocr_one(src_path: str, out_path: str) -> subprocess.CompletedProcess:
    """Run ocrmypdf. --rotate-pages/--deskew straighten crooked scans; --skip-text
    leaves any page that already has text alone (so a mostly-scanned file with a
    stray text page won't error)."""
    return subprocess.run(
        ["ocrmypdf", "--rotate-pages", "--deskew", "--skip-text", src_path, out_path],
        check=True, capture_output=True, text=True,
    )


def main(src_root: str) -> None:
    if not shutil.which("ocrmypdf"):
        print("ERROR: ocrmypdf is not installed. Install it once with:",
              file=sys.stderr)
        print("    brew install ocrmypdf", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(src_root):
        print(f"ERROR: source folder not found: {src_root}", file=sys.stderr)
        sys.exit(1)

    ocrd, skipped, failed = [], [], []

    for clearance in VALID_CLEARANCE:
        folder = os.path.join(src_root, clearance)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.startswith(".") or not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(folder, name)
            label = f"{clearance}/{name}"

            if _has_text(path):
                skipped.append(label)
                continue

            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                _ocr_one(path, tmp)
                shutil.move(tmp, path)   # replace the scan with the searchable copy
                ocrd.append(label)
            except subprocess.CalledProcessError as e:
                last = (e.stderr or e.stdout or "").strip().splitlines()
                failed.append((label, last[-1] if last else f"exit {e.returncode}"))
                if os.path.exists(tmp):
                    os.remove(tmp)

    print(f"\nOCR'd {len(ocrd)} scanned file(s):")
    for f in ocrd:
        print(f"  +  {f}")
    if skipped:
        print(f"\nLeft {len(skipped)} file(s) alone (already had text):")
        for f in skipped:
            print(f"  -  {f}")
    if failed:
        print(f"\nFAILED on {len(failed)} file(s):")
        for f, why in failed:
            print(f"  !  {f}  ->  {why}")

    print("\nNext:  python pdf_to_knowledge.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="OCR scanned PDFs in knowledge_sources/ so they become readable."
    )
    ap.add_argument(
        "--src", default="knowledge_sources",
        help="Root folder with public/ internal/ executive/ subfolders "
             "(default: knowledge_sources)",
    )
    args = ap.parse_args()
    main(args.src)
