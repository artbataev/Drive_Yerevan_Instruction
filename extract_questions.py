#!/usr/bin/env python3
"""Extract road-exam questions from PDFs: text + per-question PNG clips.

Strategy: identify individual question *cells* on each page first (using
PDF drawing rectangles anchored by 'отв' answer markers), then extract
and parse text within each cell independently.  This guarantees that the
question text and the clipped image always come from the same cell.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent
PDF_GLOB = "*.pdf"
OUT_JSON = ROOT / "questions.json"
MEDIA_DIR = ROOT / "media"
ANS_LINE = re.compile(r"отв\s*[^\d\n]*(\d+)\s*$", re.UNICODE)
OPT_LINE = re.compile(r"^\s*(\d+)\.(.+)$")


# ---------------------------------------------------------------------------
# Text parsing (works on text from a single cell OR a full page)
# ---------------------------------------------------------------------------

def expand_merged_option_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^\d+\.", s):
            chunks = re.split(r"\s+(?=\d+\.)", s)
            out.extend(chunks)
        else:
            out.append(s)
    return out


def parse_cell_text(text: str) -> dict | None:
    """Parse a single question from *one cell's* text.  Returns the first
    valid question dict or ``None``."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    buf: list[str] = []
    for line in lines:
        m = ANS_LINE.search(line)
        if m:
            ans = int(m.group(1))
            pre = line[: m.start()].rstrip()
            if pre:
                buf.append(pre)
            block = "\n".join(buf).strip()
            buf = []
            if not block:
                continue
            raw_lines = [ln for ln in block.split("\n") if ln.strip()]
            raw_lines = expand_merged_option_lines(raw_lines)
            stem_lines: list[str] = []
            options: list[str] = []
            for ln in raw_lines:
                om = OPT_LINE.match(ln)
                if om:
                    options.append(om.group(2).strip())
                else:
                    if options:
                        cont = ln.strip()
                        if re.search(r"\s+\d+\.", cont):
                            parts = re.split(r"\s+(?=\d+\.)", cont)
                            options[-1] = (options[-1] + " " + parts[0].strip()).strip()
                            for p in parts[1:]:
                                om2 = OPT_LINE.match(p.strip())
                                if om2:
                                    options.append(om2.group(2).strip())
                        else:
                            options[-1] = (options[-1] + " " + cont).strip()
                    else:
                        stem_lines.append(ln)
            stem = "\n".join(x for x in stem_lines if x.strip()).strip()
            if options and 1 <= ans <= len(options):
                return {"stem": stem, "options": options, "correctIndex": ans - 1}
        else:
            buf.append(line)
    return None


def parse_page_text(text: str) -> list[dict]:
    """Legacy: parse *all* questions from a full page's text (fallback)."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    buf: list[str] = []
    questions: list[dict] = []
    for line in lines:
        m = ANS_LINE.search(line)
        if m:
            ans = int(m.group(1))
            pre = line[: m.start()].rstrip()
            if pre:
                buf.append(pre)
            block = "\n".join(buf).strip()
            buf = []
            if not block:
                continue
            raw_lines = [ln for ln in block.split("\n") if ln.strip()]
            raw_lines = expand_merged_option_lines(raw_lines)
            stem_lines: list[str] = []
            options: list[str] = []
            for ln in raw_lines:
                om = OPT_LINE.match(ln)
                if om:
                    options.append(om.group(2).strip())
                else:
                    if options:
                        cont = ln.strip()
                        if re.search(r"\s+\d+\.", cont):
                            parts = re.split(r"\s+(?=\d+\.)", cont)
                            options[-1] = (options[-1] + " " + parts[0].strip()).strip()
                            for p in parts[1:]:
                                om2 = OPT_LINE.match(p.strip())
                                if om2:
                                    options.append(om2.group(2).strip())
                        else:
                            options[-1] = (options[-1] + " " + cont).strip()
                    else:
                        stem_lines.append(ln)
            stem = "\n".join(x for x in stem_lines if x.strip()).strip()
            if options and 1 <= ans <= len(options):
                questions.append(
                    {"stem": stem, "options": options, "correctIndex": ans - 1}
                )
        else:
            buf.append(line)
    return questions


# ---------------------------------------------------------------------------
# Cell detection
# ---------------------------------------------------------------------------

def find_question_cells(page: fitz.Page) -> list[fitz.Rect]:
    """Return one ``fitz.Rect`` per question cell, sorted top→bottom,
    left→right.  Each cell is the *smallest* drawing rectangle that
    contains an 'отв' answer marker."""
    pr = page.rect
    drawings = page.get_drawings()
    all_rects: list[fitz.Rect] = []
    for d in drawings:
        r = fitz.Rect(d["rect"])
        if r.width < 60 or r.height < 60:
            continue
        if r.width > pr.width * 0.95 and r.height > pr.height * 0.95:
            continue
        all_rects.append(r)

    if not all_rects:
        return []

    otv_hits = page.search_for("отв")
    if not otv_hits:
        return []

    seen: set[tuple[int, int, int, int]] = set()
    cells: list[fitz.Rect] = []

    for otv in otv_hits:
        ox = (otv.x0 + otv.x1) / 2
        oy = (otv.y0 + otv.y1) / 2
        best: fitz.Rect | None = None
        best_area = float("inf")
        for r in all_rects:
            if r.x0 <= ox <= r.x1 and r.y0 <= oy <= r.y1:
                area = r.width * r.height
                if area < best_area:
                    best = r
                    best_area = area
        if best is not None:
            key = (round(best.x0), round(best.y0), round(best.x1), round(best.y1))
            if key not in seen:
                seen.add(key)
                cells.append(best)

    if cells:
        row_h = max(min(c.height for c in cells) * 0.3, 10)
        cells.sort(key=lambda r: (round(r.y0 / row_h) * row_h, r.x0))

    return cells


# ---------------------------------------------------------------------------
# Image clipping
# ---------------------------------------------------------------------------

def cell_clip(page: fitz.Page, cell: fitz.Rect) -> fitz.Rect:
    """Inset the cell border and cut off the 'отв' answer line."""
    pr = page.rect
    inset = 3
    clip = fitz.Rect(
        cell.x0 + inset, cell.y0 + inset, cell.x1 - inset, cell.y1 - inset
    )
    for m in sorted(page.search_for("отв"), key=lambda r: r.y0):
        if cell.x0 <= m.x0 <= cell.x1 and cell.y0 <= m.y0 <= cell.y1:
            cut = m.y0 - 2
            if cut - clip.y0 > 20:
                clip.y1 = min(clip.y1, cut)
            break
    clip = clip & pr
    if clip.width < 10 or clip.height < 10:
        return fitz.Rect(
            cell.x0 + inset, cell.y0 + inset, cell.x1 - inset, cell.y1 - inset
        ) & pr
    return clip


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def extract_from_pdf(pdf_path: Path, dpi: int = 144) -> list[dict]:
    out: list[dict] = []
    pdf_stem = pdf_path.stem
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(dpi / 72, dpi / 72)

    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        cells = find_question_cells(page)

        if cells:
            for cell_idx, cell in enumerate(cells):
                cell_text = page.get_text("text", clip=cell)
                q = parse_cell_text(cell_text)
                if q is None:
                    continue
                clip = cell_clip(page, cell)
                qid = f"{pdf_stem}-p{page_index}-q{cell_idx}"
                MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                img_name = f"{qid}.png"
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                pix.save(str(MEDIA_DIR / img_name))
                out.append(
                    {
                        "id": qid,
                        "source": pdf_path.name,
                        "page": page_index,
                        "image": f"media/{img_name}",
                        "text": q["stem"],
                        "options": q["options"],
                        "correctIndex": q["correctIndex"],
                    }
                )
        else:
            qs = parse_page_text(page.get_text())
            for q_idx, q in enumerate(qs):
                qid = f"{pdf_stem}-p{page_index}-q{q_idx}"
                clip = page.rect
                MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                img_name = f"{qid}.png"
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                pix.save(str(MEDIA_DIR / img_name))
                out.append(
                    {
                        "id": qid,
                        "source": pdf_path.name,
                        "page": page_index,
                        "image": f"media/{img_name}",
                        "text": q["stem"],
                        "options": q["options"],
                        "correctIndex": q["correctIndex"],
                    }
                )

    doc.close()
    return out


def main() -> None:
    pdfs = sorted(ROOT.glob(PDF_GLOB))
    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        sys.exit(1)
    all_q: list[dict] = []
    for pdf in pdfs:
        if pdf.name.startswith("."):
            continue
        print("Extracting", pdf.name, "...", flush=True)
        all_q.extend(extract_from_pdf(pdf))
    payload = {"version": 1, "count": len(all_q), "questions": all_q}
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Wrote", OUT_JSON, "with", len(all_q), "questions")


if __name__ == "__main__":
    main()
