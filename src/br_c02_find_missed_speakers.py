
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Always stay in sync with the live patterns.
sys.path.insert(0, str(Path(__file__).parent))
from br_02_speaker_identifier import RE_SPEAKER_LINE, RE_INLINE_SPEAKER  # noqa: E402

csv.field_size_limit(10 ** 7)

# ---------------------------------------------------------------------------
# Loose "candidate" regex — deliberately broad; precision comes from the
# already-matched check and the suppression list below.
#
# A candidate line must:
#   1. start a line
#   2. begin with something name-like (title, noble particle, Frau/Herr, or an
#      uppercase word — including the ALL-CAPS surnames of the pre-2000 scans)
#   3. be 5–160 characters long
#   4. end with a bare colon, or a colon followed by speech text (inline)
# ---------------------------------------------------------------------------
RE_CANDIDATE = re.compile(
    r"^"
    r"(?:"
    r"(?:Frau|Herr)\s+[A-ZÄÖÜ\w]"              # honorific
    r"|(?:Dr|Prof|Dipl|D)[.,]\s*\S"            # academic title (OCR comma tolerated)
    r"|(?:von|van|de|zu|vom|zur)\s+[A-ZÄÖÜ]"   # noble particle
    r"|[A-ZÄÖÜ][A-ZÄÖÜa-zäöüß\-]{2,}"          # plain or ALL-CAPS surname
    r")"
    r"[^\n]{4,160}"
    r"(?::\s*$|:\s+\S)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Structural headings / procedural lines that end in a colon but are not
# speaker attributions. Tuned for the Bundesrat protocols.
# ---------------------------------------------------------------------------
_SUPPRESS = [
    # agenda + document machinery
    re.compile(r"^(?:Tagesordnung|Tagesordnungspunkt|Punkt\s+\d|Zu\s+Punkt)", re.I),
    re.compile(r"^(?:Drucksache|Anlage|Anmerkung|Übersicht|Tabelle|Umdruck)", re.I),
    re.compile(r"\bDrucksachen?\s+\d"),
    # procedural narration by the chair
    re.compile(r"^(?:Wir kommen|Es folgt|Ich rufe|Ich erteile|Ich bitte|Ich stelle)", re.I),
    re.compile(r"^(?:Die Sitzung|Der Bundesrat|Das Wort|Wortmeldung|Zur Abstimmung)", re.I),
    re.compile(r"^(?:Ergebnis|Beschluss|Beschluß|Abstimmung|Namentliche|Zustimmung)", re.I),
    re.compile(r"^(?:Gesetz|Entwurf|Verordnung|Vorschlag|Bericht des|Mitteilung)", re.I),
    # layout / OCR artefacts
    re.compile(r"^\d+\.\s"),                 # numbered list item
    re.compile(r"^[—–-]"),                   # dash continuation
    re.compile(r"^\("),                      # interjection line
    re.compile(r"^(?:a|b|c|d|e)\)"),         # sub-list item
    re.compile(r"^[A-D]\)"),                 # column marker
    re.compile(r"\b\d{1,5}\*?\s*[A-D]\b"),   # TOC page reference
    re.compile(r"^(?:Beginn|Schluss|Schluß|Ende)\s*:", re.I),
    re.compile(r"^Bundesrat\s*[–—-]"),       # running page header
]


def _suppress(line: str) -> bool:
    s = line.strip()
    return any(p.search(s) for p in _SUPPRESS)


# A speaker attribution is a NAME (plus optional title/role/Land) — never a
# sentence. Bundesrat prose is full of "…:" constructions ("Das heißt:",
# "Den Vorlagen ohne Änderung zuzustimmen:"), so anything containing German
# function words or a clause-final verb is dropped.
_PROSE_WORD = re.compile(
    r"\b(?:der|die|das|den|dem|des|ein|eine|einen|einem|und|oder|zu|zur|zum|"
    r"ohne|für|mit|nach|über|auf|aus|bei|ist|sind|war|hat|habe|haben|wird|"
    r"werden|ich|wir|sie|es|nicht|folgende[nrs]?|dieser|diese|dieses)\b"
)
_CLAUSE_END = re.compile(r"(?:en|zustimmen|stellen|rufe ich auf|abzusehen|heißt|betont)\s*$", re.I)


def _is_prose(norm: str) -> bool:
    """True if the pre-colon text reads as a sentence rather than a name."""
    if _PROSE_WORD.search(norm) or _CLAUSE_END.search(norm):
        return True
    # a single lowercase-ish token, or no capitalised name token at all
    return not re.search(r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]{1,}", norm)


def _normalise(line: str) -> str:
    """Drop the colon + inline speech, collapse whitespace, mask digits."""
    colon = line.find(":")
    if colon != -1:
        line = line[:colon]
    return re.sub(r"\s+", " ", line).strip()


def _shape(norm: str) -> str:
    """Structural signature: names → <N>, digits → N. Groups variants together."""
    s = re.sub(r"\d+", "N", norm)
    return re.sub(r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]+", "<N>", s)


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan_session(cleaned_path: Path) -> list[dict]:
    try:
        text = cleaned_path.read_text(encoding="utf-8")
    except Exception:
        return []

    # Spans already captured by the live patterns. Whole spans matter, not just
    # start offsets: an attribution can run over two or three lines
    # ("Leicht, Parlamentarischer Staatssekretär beim\nBundesminister der
    # Finanzen:"), and the continuation line would otherwise look unmatched.
    spans: list[tuple[int, int]] = []
    for rx in (RE_SPEAKER_LINE, RE_INLINE_SPEAKER):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    spans.sort()

    def _covered(pos: int) -> bool:
        lo, hi = 0, len(spans) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s, e = spans[mid]
            if pos < s:
                hi = mid - 1
            elif pos > e:
                lo = mid + 1
            else:
                return True
        return False

    stem = cleaned_path.stem.replace("_cleaned", "")        # Session_0941
    session = stem.replace("Session_", "")
    year = cleaned_path.parent.name

    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    def _line_no(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    out = []
    for m in RE_CANDIDATE.finditer(text):
        if _covered(m.start()):
            continue
        raw = m.group(0)
        if _suppress(raw):
            continue
        norm = _normalise(raw)
        if len(norm) < 5 or _is_prose(norm):
            continue
        out.append({
            "session": session,
            "year":    year,
            "line_no": _line_no(m.start()),
            "raw":     raw.replace("\n", " <NL> ")[:200],
            "normalised": norm,
            "shape":   _shape(norm),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Find potential missed Bundesrat speaker attributions.")
    ap.add_argument("--preprocessed-dir", default="outputs_from_hpc",
                    help="Root containing the year subfolders (default: outputs_from_hpc).")
    ap.add_argument("--output-dir", default="outputs", help="Directory for the CSV report.")
    ap.add_argument("--top", type=int, default=40, help="How many top patterns to display.")
    ap.add_argument("--min-freq", type=int, default=2, help="Ignore patterns rarer than this.")
    ap.add_argument("--era", choices=["pre2000", "post2000"], default=None,
                    help="Restrict the scan to one era.")
    args = ap.parse_args()

    pre_dir = Path(args.preprocessed_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(pre_dir.glob("*/Session_*_cleaned.txt"))
    if args.era:
        def keep(p: Path) -> bool:
            y = p.parent.name
            return y.isdigit() and ((int(y) < 2000) if args.era == "pre2000" else (int(y) >= 2000))
        files = [f for f in files if keep(f)]

    print(f"Scanning {len(files)} cleaned session files …\n")

    all_c: list[dict] = []
    per_decade: dict[str, int] = defaultdict(int)
    for i, f in enumerate(files, 1):
        cands = scan_session(f)
        all_c.extend(cands)
        for c in cands:
            y = c["year"]
            per_decade[f"{int(y)//10*10}s" if y.isdigit() else "?"] += 1
        if i % 250 == 0:
            print(f"  … {i}/{len(files)} scanned, {len(all_c):,} candidates so far")

    print(f"\nTotal candidate missed-attribution lines: {len(all_c):,}")
    print("\nBy decade:")
    for d in sorted(per_decade):
        print(f"  {d:>7}  {per_decade[d]:>7,}")

    # Rank by structural shape — groups OCR variants of one format together.
    shapes = Counter(c["shape"] for c in all_c)
    print(f"\nTop {args.top} unmatched SHAPES (min freq {args.min_freq}):")
    print(f"  {'Count':>6}  Shape / example")
    for shape, cnt in shapes.most_common(args.top):
        if cnt < args.min_freq:
            break
        ex = next(c for c in all_c if c["shape"] == shape)
        print(f"  {cnt:>6}  {shape[:60]!r}")
        print(f"          e.g. s{ex['session']}: {ex['normalised'][:80]!r}")

    csv_path = out_dir / "br_c02_missed_speakers.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["session", "year", "line_no", "raw", "normalised", "shape"])
        w.writeheader()
        w.writerows(all_c)
    print(f"\nFull candidate list -> {csv_path}  ({len(all_c):,} rows)")


if __name__ == "__main__":
    main()
