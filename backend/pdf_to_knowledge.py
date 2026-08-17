"""
Turn real business documents (PDF / text) into the clearance-tagged JSON that
ingest_knowledge.py expects. This is the "first mile" the RUNBOOK used to skip.

THE IDEA
--------
Your knowledge doesn't start as JSON — it starts as PDFs, exports, and scanned
paper. You tag each document by DROPPING IT INTO A FOLDER named for who may see
it. The folder name becomes the clearance tag, so tagging is a physical act you
can't fat-finger:

    knowledge_sources/
      public/          <- anyone may see these
        company-overview.pdf
        services.txt
      internal/        <- staff only
        employee-handbook.pdf
      executive/       <- leadership only
        q2-board-deck.pdf

Run:  python pdf_to_knowledge.py                    # reads ./knowledge_sources
      python pdf_to_knowledge.py --src ./acme_docs  # a different client's folder
      python pdf_to_knowledge.py --out acme.json    # choose the output file

Then: python ingest_knowledge.py --file knowledge_from_docs.json

One file becomes one JSON record. You do NOT need to split big documents by hand:
ingest_knowledge.py chunks them automatically at ingest time, and every chunk
keeps its document's clearance tag.

HARD COPIES / SCANNED PDFs
--------------------------
A scan is a *picture* of text, so there is no text inside to extract. This
script DETECTS that (a PDF that yields almost no characters) and lists exactly
which files need OCR first, instead of silently writing empty records. See
RUNBOOK.md, section "Scanned documents & hard copies", for the OCR step.
"""
import argparse
import json
import os
import sys

from pypdf import PdfReader

# The clearance levels the retrieval layer understands. These are ALSO the exact
# subfolder names this script looks for — anything else is ignored (and warned).
VALID_CLEARANCE = ["public", "internal", "executive"]
PDF_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md"}
# A PDF yielding fewer than this many characters is almost certainly a scanned
# image (no embedded text) rather than a real extraction failure.
MIN_CHARS = 20


def _normalize(text: str) -> str:
    """PDFs extract with ragged whitespace and blank-line runs. Collapse them so
    each chunk the RAG builds is clean. We only touch whitespace, never words."""
    out, blanks = [], 0
    for line in text.splitlines():
        line = line.rstrip()
        if line.strip():
            out.append(line)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:            # keep single blank lines, drop runs
                out.append("")
    return "\n".join(out).strip()


def _extract_pdf(path: str) -> str:
    reader = PdfReader(path)
    pages = [(page.extract_text() or "") for page in reader.pages]
    return _normalize("\n".join(pages))


def _extract_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _normalize(f.read())


def convert(src_root: str, out_path: str) -> None:
    if not os.path.isdir(src_root):
        print(f"ERROR: source folder not found: {src_root}", file=sys.stderr)
        print("Create it with one subfolder per clearance level, e.g.:", file=sys.stderr)
        print(f"  mkdir -p {src_root}/public {src_root}/internal {src_root}/executive",
              file=sys.stderr)
        sys.exit(1)

    records, needs_ocr, skipped = [], [], []
    counts = {lvl: 0 for lvl in VALID_CLEARANCE}

    for clearance in VALID_CLEARANCE:
        folder = os.path.join(src_root, clearance)
        if not os.path.isdir(folder):
            print(f"(no '{clearance}/' folder — skipping that level)")
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith(".") or not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            try:
                if ext in PDF_EXTS:
                    text = _extract_pdf(path)
                elif ext in TEXT_EXTS:
                    text = _extract_text_file(path)
                else:
                    skipped.append((clearance, name, f"unsupported type '{ext or 'none'}'"))
                    continue
            except Exception as e:  # noqa: BLE001 - report bad file, keep going
                skipped.append((clearance, name, f"read error: {e}"))
                continue

            if len(text) < MIN_CHARS:
                needs_ocr.append((clearance, name))   # scan / image PDF -> OCR
                continue

            records.append({
                "source": f"{clearance}/{name}",       # human-readable provenance
                "clearance": clearance,                # the security tag
                "text": text,
            })
            counts[clearance] += 1

    # Warn about stray subfolders that AREN'T a clearance level — a typo like
    # "internel/" would otherwise silently drop every document inside it.
    for entry in sorted(os.listdir(src_root)):
        full = os.path.join(src_root, entry)
        if os.path.isdir(full) and entry not in VALID_CLEARANCE and not entry.startswith("."):
            print(f"⚠  ignored folder '{entry}/' — not a clearance level "
                  f"({', '.join(VALID_CLEARANCE)}). Files inside it were NOT included.")

    print("\nExtracted records per clearance:")
    for lvl in VALID_CLEARANCE:
        print(f"  {lvl:<10}: {counts[lvl]}")

    if needs_ocr:
        print("\n⚠  These files produced ~no text — almost certainly SCANNED images")
        print("   (or password-protected). They need OCR before they can be used:")
        for lvl, name in needs_ocr:
            print(f"     {lvl}/{name}")
        print("   See RUNBOOK.md -> 'Scanned documents & hard copies'.")

    if skipped:
        print("\nSkipped (unsupported type or read error):")
        for lvl, name, why in skipped:
            print(f"     {lvl}/{name}  ->  {why}")

    if not records:
        print("\nERROR: no usable text extracted — nothing written.", file=sys.stderr)
        sys.exit(1)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(records)} records -> {out_path}")
    print("Open it and skim it now — this is your chance to catch a mis-filed")
    print("document BEFORE it enters the searchable index.")
    print(f"\nNext:  python ingest_knowledge.py --file {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Convert a folder of PDF/text documents into clearance-tagged JSON."
    )
    ap.add_argument(
        "--src", default="knowledge_sources",
        help="Root folder holding public/ internal/ executive/ subfolders "
             "(default: knowledge_sources)",
    )
    ap.add_argument(
        "--out", default="knowledge_from_docs.json",
        help="Output JSON path (default: knowledge_from_docs.json — deliberately "
             "NOT the shipped demo file, so this never overwrites it)",
    )
    args = ap.parse_args()
    convert(args.src, args.out)
