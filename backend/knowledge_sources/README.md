# knowledge_sources — drop your documents here

This is where a company's real documents go **before** they become the RAG's
searchable knowledge. You tag each document by **which subfolder you put it in**.
The subfolder name is the clearance level, so tagging is a physical act you
can't mistype.

```
knowledge_sources/
├── public/        # anyone may see these (marketing, public docs, FAQs)
├── internal/      # staff only (handbooks, processes, internal wikis)
└── executive/     # leadership only (financials, strategy, board material)
```

Supported file types today: **`.pdf`, `.txt`, `.md`**.
One file becomes one knowledge record — you do **not** need to split big
documents by hand; they're chunked automatically at ingest time and every chunk
keeps its folder's clearance tag.

## Turn these documents into the knowledge JSON

```bash
python pdf_to_knowledge.py            # reads this folder -> knowledge_from_docs.json
python ingest_knowledge.py --file knowledge_from_docs.json
```

## A few rules that keep you safe

- **Folder name = who can see it.** A file in `executive/` is executive-only.
  When unsure, put it in the *more* restricted folder — under-sharing is safe,
  over-sharing leaks.
- **One clearance per file.** If a single document mixes sensitivities (e.g. a
  report with a public summary and confidential figures), split it into two
  files and file each in the right folder.
- **Scanned PDFs / photos of paper need OCR first.** A scan is a picture of
  text, so there's nothing to extract. `pdf_to_knowledge.py` will tell you which
  files look scanned. See the "Scanned documents & hard copies" section in
  [`../RUNBOOK.md`](../RUNBOOK.md).
- **Your real documents in here are git-ignored** (except this README), so
  proprietary files don't get committed by accident.
