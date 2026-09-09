
# ============================================================
# USER SETTINGS  (override via --input-dir / --output-dir)
# ============================================================
_DEFAULT_INPUT  = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\Bundesrat\PDFs"
_DEFAULT_OUTPUT = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\Bundesrat\preprocessed"
# ============================================================

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Page-break separator written into extracted .txt files.
# Must match TXT_PAGE_SEP in br_01_text_cleaning.py exactly.
# ---------------------------------------------------------------------------
TXT_PAGE_SEP = "\n\n--- Page Break ---\n\n"

# ---------------------------------------------------------------------------
# CSV catalogue column order
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "session_id",
    "session",
    "date",
    "date_iso",
    "city",
    "year",
    "source_pdf",
    "is_schreibmaschine",
    "md5",
    "engine",
    "warnings_count",
    "warnings_detail",
]

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Matches both main and Schreibmaschinenversion filenames:
#   "Plenarprotokoll 941. Sitzung, 29.01.2016th Plenary Session, 29.01.2016.pdf"
#   "Plenarprotokoll 1. Sitzung, 07.09.1949 - Schreibmaschinenversion..."
RE_FILENAME = re.compile(
    r"Plenar(?:protokoll|sitzung)\s+(\d+)\.\s*Sitzung,?\s*(\d{2})\.(\d{2})\.(\d{4})",
    re.IGNORECASE,
)


def parse_filename(path: Path) -> dict | None:
    """
    Extract session number, date components, and Schreibmaschinenversion flag
    from a Bundesrat PDF filename.

    Returns a dict with keys: session, day, month, year, is_schreibmaschine.
    Returns None if the filename does not match the expected pattern.
    """
    m = RE_FILENAME.search(path.name)
    if not m:
        return None
    return {
        "session":             m.group(1).zfill(4),
        "day":                 m.group(2),
        "month":               m.group(3),
        "year":                m.group(4),
        "is_schreibmaschine":  "Schreibmaschinenversion" in path.name,
    }


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _run_pdftotext(pdf_path: Path, raw: bool = False) -> str:
    """Run pdftotext and return extracted text, or empty string on failure."""
    cmd = ["pdftotext", "-enc", "UTF-8"]
    if raw:
        cmd.append("-raw")
    cmd += [str(pdf_path), "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _run_pdfminer(pdf_path: Path) -> str:
    """Run pdfminer extraction and return text, or empty string on failure."""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(pdf_path))
        return text or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# OCR fallback (for image-only scanned PDFs — the 1949–1999 sessions)
#
# The pre-2000 Bundesrat protocols are pure image scans with no text layer,
# so pdftotext and pdfminer both return empty. These are rendered page-by-page
# with PyMuPDF and OCR'd with Tesseract (German, "deu").
#
# The scans are clean Antiqua (Roman) type — NOT Fraktur — so the standard
# "deu" traineddata is correct. Requires the tesseract binary + deu.traineddata
# on the system, plus the pymupdf / pytesseract / Pillow Python packages.
# ---------------------------------------------------------------------------

# Module-level worker so it can be pickled by multiprocessing.Pool.
def _ocr_one_image(args: tuple) -> str:
    """OCR a single page image (PNG bytes) → text. Runs in a worker process."""
    png_bytes, lang = args
    import io
    import pytesseract
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(png_bytes))
        return pytesseract.image_to_string(img, lang=lang)
    except Exception:
        return ""


def _run_ocr(pdf_path: Path, lang: str = "deu", dpi: int = 300,
             workers: int = 1) -> str:
    """
    Render every page with PyMuPDF and OCR it with Tesseract.
    Pages are rendered in the main process (fast) then OCR'd in a worker pool
    (the slow part). Returns text with form-feed (\\f) page separators so the
    downstream page-break replacement works uniformly with the other engines.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    images: list[bytes] = []
    for pno in range(doc.page_count):
        pix = doc[pno].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        images.append(pix.tobytes("png"))
    doc.close()

    if not images:
        return ""

    tasks = [(png, lang) for png in images]
    if workers and workers > 1 and len(tasks) > 1:
        from multiprocessing import Pool
        with Pool(processes=workers) as pool:
            page_texts = pool.map(_ocr_one_image, tasks)
    else:
        page_texts = [_ocr_one_image(t) for t in tasks]

    return "\f".join(page_texts)


def _tesseract_available() -> bool:
    """True if the tesseract binary and pytesseract wrapper are usable."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def extract_text(pdf_path: Path,
                 ocr: bool = True,
                 ocr_lang: str = "deu",
                 ocr_dpi: int = 300,
                 ocr_workers: int = 1,
                 force_ocr: bool = False) -> tuple[str, str]:
    """
    Try text-layer strategies first, then OCR, returning (text, engine_name).
    Raises RuntimeError if every strategy yields empty text.

    Engines, in order:
      1. pdftotext (layout)   — born-digital two-column PDFs
      2. pdftotext -raw       — content-stream order, older text-layer PDFs
      3. pdfminer             — pure-Python fallback
      4. ocr-tesseract        — image-only scans (only if ocr=True)

    force_ocr=True skips 1–3 and goes straight to OCR. Use for born-digital PDFs
    whose embedded font is broken so the text layer decodes to garbage but the
    page renders correctly (e.g. session 776's subsetted Candida-Bold names).
    """
    if force_ocr:
        text = _run_ocr(pdf_path, lang=ocr_lang, dpi=ocr_dpi, workers=ocr_workers)
        if text.strip():
            return text, "ocr-tesseract"
        raise RuntimeError("Forced OCR returned empty text")

    # Strategy 1: pdftotext layout mode (best for born-digital)
    text = _run_pdftotext(pdf_path, raw=False)
    if text.strip():
        return text, "pdftotext"

    # Strategy 2: pdftotext raw mode (content-stream order, older PDFs)
    text = _run_pdftotext(pdf_path, raw=True)
    if text.strip():
        return text, "pdftotext-raw"

    # Strategy 3: pdfminer
    text = _run_pdfminer(pdf_path)
    if text.strip():
        return text, "pdfminer"

    # Strategy 4: OCR (image-only scanned PDFs)
    if ocr:
        text = _run_ocr(pdf_path, lang=ocr_lang, dpi=ocr_dpi, workers=ocr_workers)
        if text.strip():
            return text, "ocr-tesseract"

    raise RuntimeError("All extraction engines returned empty text (likely image-only PDF)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_log(log_path: Path, line: str) -> None:
    """Append a single line to a log file (simple, not flock-safe)."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def city_from_year(year: str) -> str:
    """Bundesrat met in Bonn until 1999; Berlin from 2000 onward."""
    try:
        return "Bonn" if int(year) <= 1999 else "Berlin"
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Output path helper
# ---------------------------------------------------------------------------

def output_path_for(year: str, session: str, out_root: Path) -> Path:
    """
    Returns: preprocessed/{year}/Session_{NNN}.txt
    """
    return out_root / year / f"Session_{session}.txt"


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def record_to_csv_row(record: dict) -> dict:
    warnings = record.get("warnings", [])
    return {
        "session_id":         record.get("session_id", ""),
        "session":            record.get("session", ""),
        "date":               record.get("date", ""),
        "date_iso":           record.get("date_iso", ""),
        "city":               record.get("city", ""),
        "year":               record.get("year", ""),
        "source_pdf":         record.get("source_pdf", ""),
        "is_schreibmaschine": str(record.get("is_schreibmaschine", False)),
        "md5":                record.get("md5", ""),
        "engine":             record.get("engine", ""),
        "warnings_count":     len(warnings),
        "warnings_detail":    json.dumps(warnings, ensure_ascii=False),
    }


def rebuild_catalogue(out_root: Path, rows: list[dict]) -> None:
    """Write the metadata catalogue CSV, MERGING with any existing catalogue.

    Rows for sessions processed in this run overwrite their previous entries;
    rows for sessions NOT touched this run (e.g. when --session filters to a
    subset) are preserved from the existing catalogue. This keeps the full
    catalogue intact during targeted single-session re-runs.
    """
    catalogue_dir = out_root / "metadata"
    catalogue_dir.mkdir(parents=True, exist_ok=True)
    catalogue_path = catalogue_dir / "metadata_catalogue.csv"

    # Start from the existing catalogue (keyed by session_id), then overlay
    # the freshly-produced rows.
    merged: dict[str, dict] = {}
    if catalogue_path.exists():
        with open(catalogue_path, encoding="utf-8", newline="") as fh:
            for old in csv.DictReader(fh):
                merged[old.get("session_id", "")] = old
    for r in rows:
        merged[r.get("session_id", "")] = r

    rows_sorted = sorted(merged.values(), key=lambda r: r.get("session", ""))

    with open(catalogue_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_sorted)

    print(f"\nCatalogue written: {catalogue_path}  ({len(rows_sorted)} session(s), "
          f"{len(rows)} updated this run)")


# ---------------------------------------------------------------------------
# Session grouping
# ---------------------------------------------------------------------------

def collect_sessions(in_root: Path) -> dict[str, dict]:
    """
    Scan all PDFs under in_root, parse their filenames, and group by session
    number.  Returns a dict keyed by zero-padded session number:
        {
          "0001": {
              "main": Path | None,
              "schreibmaschine": Path | None,
              "day": "07", "month": "09", "year": "1949"
          },
          ...
        }
    Files that don't match the expected naming pattern are skipped with a
    warning.
    """
    sessions: dict[str, dict] = {}
    for pdf in sorted(in_root.rglob("*.pdf")):
        info = parse_filename(pdf)
        if info is None:
            print(f"  [SKIP] Unrecognised filename: {pdf.name}")
            continue
        key = info["session"]
        if key not in sessions:
            sessions[key] = {
                "main": None,
                "schreibmaschine": None,
                "day":   info["day"],
                "month": info["month"],
                "year":  info["year"],
            }
        if info["is_schreibmaschine"]:
            sessions[key]["schreibmaschine"] = pdf
        else:
            sessions[key]["main"] = pdf
    return sessions


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(force: bool = False,
        session_filter: set[str] | None = None,
        in_root=None,
        out_root=None,
        ocr: bool = True,
        ocr_lang: str = "deu",
        ocr_dpi: int = 300,
        ocr_workers: int = 1,
        force_ocr: bool = False) -> None:
    """
    Main extraction loop.

    Parameters
    ----------
    force : bool
        Re-process sessions even when output .txt already exists.
    session_filter : set[str] | None
        If given, only process sessions whose zero-padded number is in this set
        (e.g. {"0001", "0941"}).
    in_root : str | Path | None
        Root folder containing the Bundesrat PDFs (year-range subfolders).
    out_root : str | Path | None
        Root for preprocessed output.
    ocr : bool
        Enable the Tesseract OCR fallback for image-only scanned PDFs
        (the 1949–1999 sessions). When False, image-only PDFs are skipped
        with an error — useful for a fast text-layer-only pass.
    ocr_lang : str
        Tesseract language ("deu" for German).
    ocr_dpi : int
        Render resolution for OCR (300 is a good text/speed trade-off).
    ocr_workers : int
        Parallel worker processes for page OCR within a session.
    """
    in_root  = Path(in_root  or _DEFAULT_INPUT)
    out_root = Path(out_root or _DEFAULT_OUTPUT)
    out_root.mkdir(parents=True, exist_ok=True)

    if ocr and not _tesseract_available():
        print("[WARN] --ocr requested but Tesseract is not available; "
              "image-only PDFs will be skipped. Install tesseract + the 'deu' "
              "language data and the pymupdf/pytesseract/Pillow packages.")
        ocr = False

    log_path = out_root / "processed.log"
    done: set[str] = set()
    if log_path.exists() and not force:
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    print(f"Scanning PDFs under: {in_root}")
    sessions = collect_sessions(in_root)

    if session_filter:
        sessions = {k: v for k, v in sessions.items() if k in session_filter}

    print(f"Found {len(sessions)} session(s)"
          + (f"  [filter: {sorted(session_filter)}]" if session_filter else "")
          + ("  [--force]" if force else ""))

    ok_count = skip_count = error_count = 0
    catalogue_rows: list[dict] = []

    for session_num in sorted(sessions.keys()):
        info = sessions[session_num]
        year    = info["year"]
        day     = info["day"]
        month   = info["month"]

        out_path = output_path_for(year, session_num, out_root)
        session_id = f"Session_{session_num}"

        # Skip check
        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {session_id}")
            skip_count += 1
            # Still collect metadata for catalogue
            if out_path.exists():
                catalogue_rows.append({
                    "session_id":        session_id,
                    "session":           session_num,
                    "date":              f"{day}-{month}-{year}",
                    "date_iso":          f"{year}-{month}-{day}",
                    "city":              city_from_year(year),
                    "year":              year,
                    "source_pdf":        "",
                    "is_schreibmaschine": False,
                    "md5":               "",
                    "engine":            "",
                    "warnings":          [],
                })
            continue

        # Choose PDF to extract from: prefer main, fall back to Schreibmaschinenversion
        pdf_path = info["main"] or info["schreibmaschine"]
        if pdf_path is None:
            print(f"[ERROR] {session_id}: no PDF found")
            error_count += 1
            continue

        is_schreibmaschine = (info["main"] is None)

        print(f"[PROC] {session_id} ({pdf_path.name}) ...", end=" ", flush=True)

        warnings: list[str] = []

        # Extraction order of preference:
        #   1. Text layer of the main printed protocol (pdftotext / pdfminer).
        #   2. OCR of the main printed protocol (image-only scans).
        #   3. Text layer of the Schreibmaschinenversion — LAST resort only.
        #
        # The main PDF is the canonical *printed* protocol; the
        # Schreibmaschinenversion is a preliminary typescript whose text layer is
        # often badly corrupted (e.g. session 1's colons rendered as t/»/i, which
        # breaks speaker attribution). Now that OCR is available, a clean OCR of
        # the printed protocol beats the typescript, so OCR (step 2) is tried
        # before the Schreibmaschinenversion (step 3).
        text = engine = None

        # Forced OCR: born-digital PDFs with a broken embedded font whose text
        # layer decodes to garbage (e.g. session 776). Skip text-layer engines.
        if force_ocr and ocr:
            print(f"\n  [OCR] forced OCR (broken font), running Tesseract "
                  f"({ocr_workers}w)...", end=" ", flush=True)
            try:
                text, engine = extract_text(
                    pdf_path, ocr=True, force_ocr=True, ocr_lang=ocr_lang,
                    ocr_dpi=ocr_dpi, ocr_workers=ocr_workers)
                warnings.append("Forced OCR (broken embedded font)")
            except RuntimeError:
                text = None
            if text is None:
                print("ERROR\n  -> forced OCR produced no text")
                error_count += 1
                continue
            # fall through to write-out below (skip steps 1-3)

        # Step 1: main PDF, text layer only
        if text is None and not force_ocr:
            try:
                text, engine = extract_text(pdf_path, ocr=False)
            except RuntimeError:
                text = None

        # Step 2: OCR the main printed PDF (image-only session)
        if text is None and ocr:
            print(f"\n  [OCR] image-only scan, running Tesseract ({ocr_workers}w)...",
                  end=" ", flush=True)
            try:
                text, engine = extract_text(
                    pdf_path, ocr=True, ocr_lang=ocr_lang,
                    ocr_dpi=ocr_dpi, ocr_workers=ocr_workers)
                warnings.append("Image-only scan; text recovered via Tesseract OCR")
            except RuntimeError:
                text = None

        # Step 3: Schreibmaschinenversion text layer (last resort — used only if
        # the main has no text layer AND OCR is unavailable/failed).
        if text is None and not is_schreibmaschine and info["schreibmaschine"] is not None:
            fallback = info["schreibmaschine"]
            try:
                text, engine = extract_text(fallback, ocr=False)
                if text is not None:
                    print(f"\n  [WARN] Main PDF unreadable; falling back to "
                          f"Schreibmaschinenversion...", end=" ")
                    warnings.append(
                        f"Main PDF unreadable; using Schreibmaschinenversion: {fallback.name}")
                    pdf_path = fallback
                    is_schreibmaschine = True
            except RuntimeError:
                text = None

        if text is None:
            print("ERROR\n  -> no text via any engine"
                  + ("" if ocr else " (OCR disabled)"))
            error_count += 1
            continue

        # Replace form-feed page separators with human-readable marker
        text_out = text.replace("\f", TXT_PAGE_SEP)

        try:
            source_rel = str(pdf_path.relative_to(in_root))
        except ValueError:
            source_rel = pdf_path.name

        record = {
            "session_id":        session_id,
            "session":           session_num,
            "date":              f"{day}-{month}-{year}",
            "date_iso":          f"{year}-{month}-{day}",
            "city":              city_from_year(year),
            "year":              year,
            "source_pdf":        source_rel,
            "is_schreibmaschine": is_schreibmaschine,
            "md5":               md5_of_file(pdf_path),
            "engine":            engine,
            "warnings":          warnings,
        }

        if warnings:
            for w in warnings:
                print(f"\n  [WARN] {w}", end="")

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text_out, encoding="utf-8")
            _append_log(log_path, str(out_path))
            print(f"OK -> {out_path.relative_to(out_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1
            continue

        catalogue_rows.append(record)

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")

    rebuild_catalogue(out_root, [record_to_csv_row(r) for r in catalogue_rows])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # The console on Windows is cp1252, which cannot encode the arrows and
    # umlauts in this script's progress output. Every previous run was on the
    # Linux HPC, so this only surfaces when extracting locally.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Bundesrat PDF → plain-text extractor"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process sessions even if output .txt already exists.",
    )
    parser.add_argument(
        "--session",
        metavar="N[,N,...]",
        help=(
            "Comma-separated session numbers to process "
            "(e.g. '1,941,1000'). All others are skipped."
        ),
    )
    parser.add_argument(
        "--input-dir",
        default=None,
        metavar="PATH",
        help=f"Root folder containing Bundesrat PDFs. Default: {_DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help=f"Root for preprocessed output. Default: {_DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help=(
            "Disable the Tesseract OCR fallback. Only text-layer PDFs are "
            "extracted (fast); image-only scans (1949–1999) are skipped."
        ),
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Parallel worker processes for page OCR within a session. "
            "Default: all available CPUs (os.cpu_count())."
        ),
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=300,
        metavar="DPI",
        help="Render resolution for OCR. Default: 300.",
    )
    parser.add_argument(
        "--ocr-lang",
        default="deu",
        metavar="LANG",
        help="Tesseract language code. Default: deu (German).",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help=(
            "Skip text-layer engines and OCR the PDF directly. For born-digital "
            "PDFs whose embedded font is broken (text layer decodes to garbage "
            "but the page renders fine), e.g. session 776. Use with --session."
        ),
    )
    args = parser.parse_args()

    session_filter: set[str] | None = None
    if args.session:
        session_filter = {s.strip().zfill(4) for s in args.session.split(",")}

    ocr_workers = args.ocr_workers if args.ocr_workers else (os.cpu_count() or 1)

    print("Bundesrat PDF → plain-text extractor")
    print(f"Input:  {args.input_dir or _DEFAULT_INPUT}")
    print(f"Output: {args.output_dir or _DEFAULT_OUTPUT}")
    print(f"OCR:    {'disabled' if args.no_ocr else f'enabled (lang={args.ocr_lang}, dpi={args.ocr_dpi}, workers={ocr_workers})'}")
    print()

    run(
        force=args.force,
        session_filter=session_filter,
        in_root=args.input_dir,
        out_root=args.output_dir,
        ocr=not args.no_ocr,
        ocr_lang=args.ocr_lang,
        ocr_dpi=args.ocr_dpi,
        ocr_workers=ocr_workers,
        force_ocr=args.force_ocr,
    )
