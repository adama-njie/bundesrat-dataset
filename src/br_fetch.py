#!/usr/bin/env python3
"""Stage 0 — fetch Bundesrat Plenarprotokoll PDFs and track revisions.

Runs BEFORE br_00. Not numbered, because br_00..br_08 are the corpus-transforming
stages and renumbering them would break every reference in CLAUDE.md, the HPC
scripts and the data paper.

    python src/br_fetch.py --dry-run          # report, download nothing
    python src/br_fetch.py                    # fetch new + revised
    python src/br_fetch.py --all-periods      # re-check the whole archive

⚠️ NO SELENIUM. The archive's period links and PDF hrefs are both present in the
server-rendered HTML, so plain HTTP suffices. The previous downloader drove
Chrome, which is unusable from a scheduled task: it needs a display, a matching
chromedriver, and it breaks silently whenever Chrome updates.
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

ARCHIVE_URL = ("https://www.bundesrat.de/DE/service/archiv/pl-protokoll-archiv/"
               "pl-protokoll-archiv-node.html")
_DEFAULT_PDF_DIR = os.environ.get(
    "BUNDESRAT_PDF_DIR", str(_PROJECT_ROOT / "data" / "raw"))
_DEFAULT_MANIFEST = _PROJECT_ROOT / "outputs" / "fetch_manifest.json"

# Identify the client honestly. A bare urllib UA is refused by some federal
# sites, and impersonating a browser to a government archive is not something to
# do silently.
_UA = ("Mozilla/5.0 (compatible; bundesrat-corpus/1.0; "
       "+https://github.com/adama-njie/bundesrat-dataset)")

_PAUSE = 1.0          # seconds between requests: this is a public archive
_RETRIES = 3

# "Plenarprotokoll 1069. Sitzung, 16.10.2026" — exactly the shape br_00's
# filename regex parses, so the row title IS the filename stem.
_RE_TITLE = re.compile(
    r"Plenar(?:protokoll|sitzung)\s+\d+\.\s*Sitzung,?\s*\d{2}\.\d{2}\.\d{4}")
_RE_PERIOD_LINK = re.compile(
    r'href="([^"]*pl-protokoll-archiv[^"]*)"[^>]*>\s*(\d{4}-\d{4})\s*<')
_RE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_RE_PDF = re.compile(r'href="([^"]+Plenarprotokoll-(\d+)\.pdf[^"]*)"')
# Session number as br_00 reads it off an EXISTING filename, used to seed the
# manifest on a first run so we do not re-download 1,000+ PDFs already on disk.
_RE_SESSION_IN_NAME = re.compile(r"Plenar(?:protokoll|sitzung)\s+(\d+)\.\s*Sitzung")


@dataclass
class FetchReport:
    """Consumed by br_update.py — this is a contract, not logging."""
    new_sessions: list[int] = field(default_factory=list)
    revised_sessions: list[int] = field(default_factory=list)
    checked_for_revision: int = 0
    seeded_from_disk: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[int]:
        return sorted(set(self.new_sessions) | set(self.revised_sessions))

    def summary(self) -> str:
        return (f"new={len(self.new_sessions)} revised={len(self.revised_sessions)} "
                f"checked={self.checked_for_revision} errors={len(self.errors)}")


def _get(url: str, *, etag: str | None = None) -> tuple[bytes | None, dict]:
    """Fetch with retries. Returns (body, headers); body is None on 304.

    ⚠️ Conditional GET, never HEAD. The Bundesrat's download endpoint answers
    HEAD with 400 Bad Request, so the obvious cheap probe does not work. An
    `If-None-Match` GET is better regardless: unchanged costs a 304 with no
    body, and a revision arrives with its bytes already in hand, so detecting
    and downloading are one request rather than two.
    """
    last = None
    for attempt in range(1, _RETRIES + 1):
        try:
            headers = {"User-Agent": _UA}
            if etag:
                headers["If-None-Match"] = etag
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, dict(exc.headers)
            last = exc
            if exc.code < 500 or attempt == _RETRIES:
                break
            time.sleep(2 * attempt)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Sciebo/OneDrive placeholder reads surface here as OSError errno 22.
            last = exc
            if attempt < _RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{url}: {last}")


def discover_periods() -> dict[str, str]:
    """{'2026-2030': url, ...}, newest first as the page lists them."""
    body, _ = _get(ARCHIVE_URL)
    page = body.decode("utf-8", "replace")
    out: dict[str, str] = {}
    for href, label in _RE_PERIOD_LINK.findall(page):
        out.setdefault(label, _html.unescape(href))
    if not out:
        raise RuntimeError("no period links found — the archive layout changed")
    return out


def list_period(url: str) -> list[dict]:
    """[{session, title, pdf_url}] for one five-year period page."""
    body, _ = _get(url)
    page = body.decode("utf-8", "replace")
    rows = []
    for tr in _RE_ROW.findall(page):
        m_pdf = _RE_PDF.search(tr)
        if not m_pdf:
            continue
        text = _html.unescape(re.sub(r"<[^>]+>", " ", tr))
        m_title = _RE_TITLE.search(re.sub(r"\s+", " ", text))
        if not m_title:
            # ⚠️ Do not fall back to building a filename from the session number
            # alone. br_00 parses the DATE out of the filename too, and a wrong
            # date silently mislabels the sitting everywhere downstream.
            continue
        rows.append({"session": int(m_pdf.group(2)),
                     "title": m_title.group(0).strip(),
                     "pdf_url": _html.unescape(m_pdf.group(1))})
    return rows


def _seed_manifest_from_disk(pdf_dir: Path, manifest: dict) -> int:
    """Record PDFs already downloaded, so a first run fetches only what is new.

    ⚠️ Without this the first run re-downloads the entire archive. The legacy
    Selenium script wrote names like
    'Plenarprotokoll 1061. Sitzung, 30.01.2026th Plenary Session, 30.01.2026.pdf'
    — the session number is still parseable, which is all we need here.
    """
    seeded = 0
    for pdf in pdf_dir.rglob("*.pdf"):
        m = _RE_SESSION_IN_NAME.search(pdf.name)
        if not m:
            continue
        key = str(int(m.group(1)))
        if key not in manifest:
            manifest[key] = {"filename": pdf.name,
                             "period": pdf.parent.name,
                             "etag": None, "last_modified": None,
                             "md5": None, "seeded": True}
            seeded += 1
    return seeded


def fetch(pdf_dir: Path, manifest_path: Path, *, all_periods: bool = False,
          revision_window: int = 12, dry_run: bool = False,
          verbose: bool = True) -> FetchReport:
    rep = FetchReport()
    pdf_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else {})

    rep.seeded_from_disk = _seed_manifest_from_disk(pdf_dir, manifest)
    if rep.seeded_from_disk and verbose:
        print(f"  seeded {rep.seeded_from_disk} PDFs already on disk")

    periods = discover_periods()
    labels = sorted(periods, reverse=True)
    if not all_periods:
        # The newest period only. Older protocols do not change, and re-listing
        # seventeen pages every month is both slow and impolite.
        labels = labels[:1]
    if verbose:
        print(f"  periods: {', '.join(labels)}")

    entries: list[dict] = []
    for label in labels:
        try:
            got = list_period(periods[label])
        except Exception as exc:                       # noqa: BLE001
            rep.errors.append(f"period {label}: {exc}")
            continue
        for e in got:
            e["period"] = label
        entries.extend(got)
        time.sleep(_PAUSE)

    entries.sort(key=lambda e: e["session"], reverse=True)
    newest = {e["session"] for e in entries[:revision_window]}

    for e in entries:
        key = str(e["session"])
        known = manifest.get(key)
        target = pdf_dir / e["period"] / f"{e['title']}.pdf"

        body: bytes | None = None
        hdrs: dict = {}

        if known is None:
            reason = "new"
            if dry_run:
                if verbose:
                    print(f"  {reason:>7}: session {e['session']}  {e['title']}")
                rep.new_sessions.append(e["session"])
                continue
            try:
                body, hdrs = _get(e["pdf_url"])
            except Exception as exc:                   # noqa: BLE001
                rep.errors.append(f"download {e['session']}: {exc}")
                continue

        elif e["session"] in newest:
            # ⚠️ Only the newest N are re-checked; older protocols do not change
            # and re-requesting the whole archive monthly is rude and slow.
            rep.checked_for_revision += 1
            if dry_run:
                continue
            try:
                body, hdrs = _get(e["pdf_url"], etag=known.get("etag"))
            except Exception as exc:                   # noqa: BLE001
                rep.errors.append(f"check {e['session']}: {exc}")
                continue
            if body is None:                            # 304, unchanged
                time.sleep(_PAUSE)
                continue
            # ⚠️ A seeded entry has no stored ETag, so the conditional GET is
            # unconditional and always returns 200. That is NOT a revision.
            # Compare content against the file already on disk and decide on the
            # bytes; a seeded session also gets its ETag recorded here, so this
            # full download happens at most once per session, ever.
            digest = hashlib.md5(body).hexdigest()
            on_disk = pdf_dir / known.get("period", e["period"]) / known["filename"]
            if known.get("etag") is None:
                same = (on_disk.exists()
                        and hashlib.md5(on_disk.read_bytes()).hexdigest() == digest)
                if same:
                    known["etag"] = hdrs.get("ETag")
                    known["last_modified"] = hdrs.get("Last-Modified")
                    known["md5"] = digest
                    time.sleep(_PAUSE)
                    continue
            elif digest == known.get("md5"):
                known["etag"] = hdrs.get("ETag")
                time.sleep(_PAUSE)
                continue
            reason = "revised"
            # Write over the name already on disk, so a revision does not leave
            # two PDFs of one sitting for br_00 to find.
            target = on_disk if on_disk.exists() else target
        else:
            continue

        if verbose:
            print(f"  {reason:>7}: session {e['session']}  {e['title']}")
        (rep.new_sessions if reason == "new" else rep.revised_sessions).append(
            e["session"])

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            manifest[key] = {
                "filename": target.name, "period": e["period"],
                "etag": hdrs.get("ETag"),
                "last_modified": hdrs.get("Last-Modified"),
                "md5": hashlib.md5(body).hexdigest(),
                "bytes": len(body), "url": e["pdf_url"], "seeded": False,
            }
        except OSError as exc:
            rep.errors.append(f"write {e['session']}: {exc}")
        time.sleep(_PAUSE)

    if not dry_run:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch Bundesrat Plenarprotokoll PDFs; track new and revised.")
    ap.add_argument("--pdf-dir", default=_DEFAULT_PDF_DIR, metavar="PATH",
                    help=f"PDF root (default: {_DEFAULT_PDF_DIR})")
    ap.add_argument("--manifest", default=str(_DEFAULT_MANIFEST), metavar="PATH")
    ap.add_argument("--all-periods", action="store_true",
                    help="list every period, not just the newest")
    ap.add_argument("--revision-window", type=int, default=12, metavar="N",
                    help="re-check the newest N sittings for revision (default 12)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be fetched; download nothing")
    args = ap.parse_args()

    print(f"Archive: {ARCHIVE_URL}")
    print(f"PDFs:    {args.pdf_dir}")
    if args.dry_run:
        print("DRY RUN — nothing will be downloaded")
    rep = fetch(Path(args.pdf_dir), Path(args.manifest),
                all_periods=args.all_periods,
                revision_window=args.revision_window, dry_run=args.dry_run)
    print(f"\n{rep.summary()}")
    if rep.changed:
        print(f"changed sessions: {', '.join(str(s) for s in rep.changed)}")
    for e in rep.errors:
        print(f"  ERROR {e}", file=sys.stderr)
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
