"""Parse Turnitin Similarity and AI Writing report PDFs (text layer only, no OCR).

analyse_report(pdf_bytes) -> {
    "kind":  "similarity" | "ai" | None,
    "cover": {title, author, submission_id, submission_date, download_date,
              file_name, file_size, pages, words, characters},
    "similarity": {overall, overall_lt1, match_groups, matches_total, sources,
                   integrity_flags, top_sources},                      # similarity reports
    "ai": {pct, below_threshold, segments},                            # AI reports
}
apply_to_document(doc, analysis) copies the numbers onto a Document row safely.

Every value that can't be found is None/missing, so a layout change degrades
gracefully instead of raising or saving a wrong number.
"""
import io
import logging
import re

log = logging.getLogger(__name__)

MAX_PAGES = 15          # overview + top-sources pages are always at the front

MATCH_GROUPS = {
    "not cited or quoted": "not_cited",
    "missing quotations": "missing_quotations",
    "missing citation": "missing_citation",
    "cited and quoted": "cited_quoted",
}
SOURCE_TYPES = {
    "internet sources": "internet",
    "publications": "publications",
    "submitted works (student papers)": "student_papers",
}

_NOISE = re.compile(r"^(Page \d+ of \d+|Submission ID|trn:oid)", re.I)


def _read(data: bytes):
    """Return (reader, [page texts]).  reader is a pypdf reader or None (fallback engine).
    Failures are LOGGED (never silently swallowed) so a missing library is obvious."""
    errors = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        texts = [(p.extract_text() or "") for p in reader.pages[:MAX_PAGES]]
        if any(t.strip() for t in texts):
            return reader, texts
        errors.append("pypdf extracted no text")
    except Exception as e:                       # ImportError, bad PDF, ...
        errors.append(f"pypdf: {type(e).__name__}: {e}")

    try:                                         # fallback engine: pip install pypdfium2
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        texts = []
        for i in range(min(len(pdf), MAX_PAGES)):
            tp = pdf[i].get_textpage()
            texts.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
        if any(t.strip() for t in texts):
            return None, texts
        errors.append("pypdfium2 extracted no text")
    except Exception as e:
        errors.append(f"pypdfium2: {type(e).__name__}: {e}")

    log.error("Could not read report PDF (%d bytes): %s", len(data), "; ".join(errors))
    return None, []


def _norm(s: str) -> str:
    """Collapse all whitespace (newlines included) so regexes don't depend on layout."""
    return re.sub(r"\s+", " ", s or "").strip()


def _clean_lines(text: str) -> str:
    """Drop the running page header/footer lines (Page X of Y, Submission ID, trn:oid...)."""
    return "\n".join(l for l in text.splitlines() if l.strip() and not _NOISE.match(l.strip()))


def _pct(s):
    """'25' -> (25, False) ; '<1' -> (0, True) ; garbage -> (None, False)."""
    s = (s or "").strip().rstrip("%").strip()
    lt1 = s.startswith("<")
    digits = s.lstrip("<").strip()
    if not digits.isdigit():
        return None, False
    v = int(digits)
    if v > 100:
        return None, False
    return (0 if lt1 else v), lt1


def _num(s):
    s = (s or "").replace(",", "").strip()
    return int(s) if s.isdigit() else None


# ---------------------------------------------------------------- cover page

def parse_cover(reader, text: str) -> dict:
    def grab(label):
        m = re.search(rf"{label}\s*\n\s*(.+)", text)
        return m.group(1).strip() if m else ""

    cover = {
        "submission_id": grab("Submission ID"),
        "submission_date": grab("Submission Date"),
        "download_date": grab("Download Date"),
        "file_name": grab("File Name"),
        "file_size": grab("File Size"),
    }
    for key, pat in (("pages", r"([\d,]+)\s+Pages"),
                     ("words", r"([\d,]+)\s+Words"),
                     ("characters", r"([\d,]+)\s+Characters")):
        m = re.search(pat, text)
        cover[key] = _num(m.group(1)) if m else None

    # Author (top) and title (below) are the two big lines above "Document Details".
    # Plain text order is not reliable, so read their vertical positions.
    cover["author"], cover["title"] = "", ""
    try:
        if reader is None:
            raise RuntimeError("no pypdf reader")
        parts = []

        def visitor(t, cm, tm, font_dict, font_size):
            if t.strip():
                parts.append((cm[5] + tm[5], t.strip()))

        reader.pages[0].extract_text(visitor_text=visitor)
        dd = next((y for y, t in parts if t == "Document Details"), None)
        if dd is not None:
            head = [t for y, t in sorted(parts, key=lambda p: -p[0]) if y > dd]
            head = [t for t in head
                    if not t.startswith(("Page ", "Submission ID", "trn:"))][:2]
            if len(head) >= 2:
                cover["author"], cover["title"] = head[0], head[1]
            elif head:
                cover["title"] = head[0]
    except Exception:
        pass
    return cover


# ---------------------------------------------------------- similarity report

def _find_overview(texts) -> str:
    """The Integrity Overview page is normally page 2, but find it instead of assuming."""
    for t in texts[:4]:
        if re.search(r"Overall\s+Similarity", t, re.I):
            return t
    return texts[1] if len(texts) > 1 else ""


def parse_similarity(texts) -> dict:
    overview = _norm(_find_overview(texts))
    out = {}

    # "39% Overall Similarity" - Turnitin may print "<1%" or "*%" for very low scores.
    out["overall"], out["overall_lt1"] = None, False
    m = re.search(r"(<?\s*\d{1,3}|\*)\s*%\s*Overall\s+Similarity", overview, re.I)
    if m:
        raw = m.group(1).replace(" ", "")
        if raw == "*":
            out["overall"], out["overall_lt1"] = 0, True
        else:
            out["overall"], out["overall_lt1"] = _pct(raw)

    # "62 Not Cited or Quoted 39%"
    groups = {}
    for m in re.finditer(r"(\d+)\s+(Not Cited or Quoted|Missing Quotations|Missing Citation|"
                         r"Cited and Quoted)\s+(\d{1,3})\s*%", overview, re.I):
        groups[MATCH_GROUPS[m.group(2).lower()]] = {"count": int(m.group(1)),
                                                    "pct": int(m.group(3))}
    out["match_groups"] = groups
    out["matches_total"] = sum(g["count"] for g in groups.values()) if groups else None

    # "25% Internet sources"
    sources = {}
    for m in re.finditer(r"(<?\s*\d{1,3})\s*%\s*(Internet sources|Publications|"
                         r"Submitted works \(Student Papers\))", overview, re.I):
        v, lt1 = _pct(m.group(1).replace(" ", ""))
        sources[SOURCE_TYPES[m.group(2).lower()]] = {"pct": v, "lt1": lt1}
    out["sources"] = sources

    m = re.search(r"(\d+)\s+Integrity Flags?\s+for Review", overview, re.I)
    out["integrity_flags"] = int(m.group(1)) if m else None

    out["top_sources"] = _parse_top_sources(texts)
    return out


def _parse_top_sources(texts):
    """'Top Sources' list: rank / type / name / pct (newline or space separated)."""
    # Start at the detailed list (the heading followed by the explanatory sentence).
    chunks, started = [], False
    for page in texts[1:]:
        if not started:
            i = page.find("The sources with the highest number of matches")
            if i < 0:
                continue
            started, page = True, page[i:]
        elif "Integrity Overview" not in page:
            break                      # first page of the actual submission body
        chunks.append(_clean_lines(page))
    text = _norm(" ".join(chunks))
    if not text:
        return []

    pat = re.compile(r"(?<!\d)(\d{1,3})\s+(Student papers?|Internet|Publications?|Journals?|"
                     r"Crossref[\w ]*?)\s+(.+?)\s+(<?\d{1,3}%)", re.I)
    top, expected = [], 1
    for m in pat.finditer(text):
        if int(m.group(1)) != expected:
            continue
        top.append({"rank": expected, "type": m.group(2), "name": m.group(3).strip(),
                    "pct": m.group(4)})            # keep as text: "2%", "<1%"
        expected += 1
    return top


# ------------------------------------------------------------------ AI report

def parse_ai(texts) -> dict:
    """Turnitin prints '37% detected as AI'; under 20% it prints '*% detected as AI'
    and deliberately hides the number. The '20%' in the explanatory text is NOT a score.
    'segments' is the count shown beside the 'AI-generated' legend ('15 AI-generated 29%')."""
    t = _norm(" ".join(texts[:3]))
    out = {"pct": None, "below_threshold": False, "segments": None}

    m = re.search(r"(\d{1,3})\s*%\s*detected\s+as\s+AI", t, re.I)
    if m and int(m.group(1)) <= 100:
        out["pct"] = int(m.group(1))
    elif re.search(r"\*\s*%\s*detected\s+as\s+AI", t, re.I):
        out["below_threshold"] = True

    m = re.search(r"(\d+)\s+AI-generated\s+(\d{1,3})\s*%", t, re.I)
    if m:
        out["segments"] = int(m.group(1))
        # fallback / cross-check: legend percentage
        if out["pct"] is None and not out["below_threshold"]:
            out["pct"] = int(m.group(2))
    return out


# ---------------------------------------------------------------- entry point

def analyse_report(data: bytes) -> dict:
    reader, texts = _read(data)
    result = {"kind": None, "cover": {}, "similarity": None, "ai": None}
    if not texts:
        return result

    joined = " ".join(texts[:3])
    if re.search(r"Overall\s+Similarity", joined, re.I):
        result["kind"] = "similarity"
    elif re.search(r"detected\s+as\s+AI", joined, re.I):
        result["kind"] = "ai"

    result["cover"] = parse_cover(reader, texts[0])
    if result["kind"] == "similarity":
        result["similarity"] = parse_similarity(texts)
    elif result["kind"] == "ai":
        result["ai"] = parse_ai(texts)
        
    print(result,"----------------------------")
    return result


# ------------------------------------------------------------------- saving

def apply_to_document(doc, analysis: dict, save=True) -> bool:
    """Copy one parsed report onto a Document row. Returns True if anything was set.

    - Similarity and AI reports arrive separately (maybe from different uploads), so
      report_data is MERGED, never replaced, and one report never wipes the other's numbers.
    - A value that failed to parse (None) never overwrites a previously saved good value.
    - Call this AFTER you have set doc.similarity_report / doc.ai_report paths, so that
      refresh_status() sees the files.
    """
    kind = analysis.get("kind")
    if kind not in ("similarity", "ai"):
        return False

    data = dict(doc.report_data or {})
    data["cover"] = {**(data.get("cover") or {}),
                     **{k: v for k, v in (analysis.get("cover") or {}).items() if v not in ("", None)}}
    fields = ["report_data"]
    changed = False

    if kind == "similarity":
        sim = analysis.get("similarity") or {}
        data["similarity"] = sim
        if sim.get("overall") is not None:
            doc.similarity_pct = sim["overall"]
            fields.append("similarity_pct")
            changed = True
    else:
        ai = analysis.get("ai") or {}
        data["ai"] = ai
        if ai.get("pct") is not None:
            doc.ai_pct, doc.ai_below_threshold = ai["pct"], False
            fields += ["ai_pct", "ai_below_threshold"]
            changed = True
        elif ai.get("below_threshold"):
            doc.ai_pct, doc.ai_below_threshold = None, True
            fields += ["ai_pct", "ai_below_threshold"]
            changed = True

    doc.report_data = data
    doc.refresh_status()
    fields.append("status")
    if save:
        doc.save(update_fields=sorted(set(fields)))
    return changed