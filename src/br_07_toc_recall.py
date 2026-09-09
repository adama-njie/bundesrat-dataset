#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv as _csv
import importlib.util
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PRE = _PROJECT_ROOT / "outputs_from_hpc"
_DEFAULT_OUT = _PROJECT_ROOT / "paper" / "tables" / "table_11_toc_recall.csv"

_csv.field_size_limit(10 ** 7)


def _load_br02():
    """Import br_02 for its LIVE surname fold and Land matcher.

    Re-implementing either here would let this script drift away from the parser
    it is supposed to be auditing — the recurring failure mode in this project.
    """
    spec = importlib.util.spec_from_file_location(
        "br_02", _PROJECT_ROOT / "src" / "br_02_speaker_identifier.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BR02 = _load_br02()


# ---------------------------------------------------------------------------
# Locating the index
# ---------------------------------------------------------------------------

# The index sits between the masthead and the opening of the sitting. Its end is
# the first thing that looks like the sitting starting; failing that, a fraction
# of the document (the index is never more than a third of a protocol).
_RE_TOC_END = re.compile(
    r"(?:Beginn[:.,]\s*\d)"                       # "Beginn: 9.32 Uhr"
    r"|(?:[Ss][iı1l][tuü]z[ue]ng\s+wird\s+um)"    # "Die Sitzung wird um ..."
    r"|(?:er[oö]f+[nm]e\s+d)",                    # "Ich eröffne die N. Sitzung"
    re.IGNORECASE)
_TOC_MAX_FRACTION = 0.35

# A ToC line: some head text, then dot leaders and/or a page reference. The page
# reference is 1-5 digits, an OPTIONAL asterisk, and a column letter A-D. OCR
# splits the letter off with a space often enough that the gap must be allowed.
_RE_PAGE_REF = re.compile(r"(\d{1,5})\s*(\*)?\s*([A-D])\b")
# Dot leaders survive OCR as any run of periods, commas and spaces.
_RE_LEADERS = re.compile(r"[.,…‚’\s]{4,}$")

# Heads that are not speakers: agenda items, procedural lines, publisher block.
_RE_NOT_A_SPEAKER = re.compile(
    r"^\s*(?:\d+\s*[.)]|[a-z]\s*[)\]])"           # "1.", "a)" agenda numbering
    r"|Beschlu|Zur Tagesordnung|Amtliche Mitteil|Geschäftliche Mitteil"
    r"|Inhalt|Tagesordnung|Drucksache|Anlage|Gesamtherstellung|Vertrieb"
    r"|Druck[:.]|Alleinvertr|ISSN|Mitteilung|Verordnung|Gesetz|Antrag"
    r"|Entschließung|Bericht\s+(?:der|des|über)|Wahl\s|Benennung"
    # Procedural lines that DO name a person but record an appointment or a
    # ballot, not a contribution to the debate.
    r"|GO\s*BR|Beauftragte|wird\s+gew[äa]hlt|zum\s+Beauftragten|§\s*\d",
    re.IGNORECASE)

# Role words that are the WHOLE head on a wrapped continuation line
# ("Hasselmann (Niedersachsen)," / "Berichterstatter"). A head that reduces to
# one of these names nobody.
_RE_ROLE_ONLY = re.compile(
    r"^(?:[Bb]erichterstatter(?:in)?|[Ss]chriftf[üu]hrer(?:in)?"
    r"|[Mm]inisterpr[äa]sident\w*|[Ss]enator\w*|[Mm]inister\w*"
    r"|[Ss]taatssekret[äa]r\w*|[Bb]evollm[äa]chtigt\w*)"
    r"(?:\s+(?:des|der|f[üu]r|und|im|beim|von)\b.*)?$")

# ⚠️ THE DISCRIMINATOR THAT MAKES THIS MEASURABLE. An index entry always begins
# with the speaker's NAME; a wrapped continuation of the previous entry's role
# begins with a preposition, an article, a role noun, or a lowercase fragment of
# a hyphenated ministry:
#     Dr. Weber, Bundesminister der Justiz . . . 147 C   <- entry
#     beim Bundesminister der Justiz . . . . . . 148 A   <- continuation
#     desministerium für Ernährung, Land-               <- continuation
# Without this, continuation lines are counted as separate speakers who then
# "go missing", which inflated the denominator 2.4x and reported 74.4 % recall
# where the real figure is far higher. Every one of the commonest "misses" was
# a fragment of this kind.
_RE_LEADING_NOT_NAME = re.compile(
    r"^(?:[a-zäöüß]"                                     # lowercase fragment
    r"|(?:bei|beim|von|vom|zur|zum|im|in|an|auf|als|des|der|die|das|dem|den|und|für)\b"
    r"|(?:Bundes\w*minister|Bundesminister|Bundeskanzler|Minister|Senator"
    r"|Staatssekret|Berichterstatter|Schriftf|Parl\b|Parlamentarisch"
    r"|Ansprache|Eidesleistung|Begr[üu][ßs]ung|Gl[üu]ckwunsch|Nachruf"
    r"|Erkl[äa]rung|Ernennung|Verabschiedung|Gedenken)\w*\b)")


def _toc_region(raw: str) -> str:
    """The slice of the raw text that holds the printed index."""
    m = _RE_TOC_END.search(raw)
    cap = int(len(raw) * _TOC_MAX_FRACTION)
    end = min(m.start(), cap) if m else cap
    return raw[:end]


# ---------------------------------------------------------------------------
# Parsing index entries
# ---------------------------------------------------------------------------

def _looks_like_person(head: str) -> bool:
    """Is this index head a speaker, rather than an agenda item?

    Requires a (Bundesland) that validates, OR a role word, OR a title. A bare
    capitalised phrase is refused — the index is full of them and they are bill
    titles.
    """
    if _RE_NOT_A_SPEAKER.search(head) or _RE_LEADING_NOT_NAME.match(head.strip()):
        return False
    inner = re.search(r"[({\[]([^)}\]]+)[)}\]]", head)
    if inner and BR02._normalise_land(inner.group(1)):
        return True
    if re.search(r"\b(?:Dr|Prof|Dipl)[.,]|^\s*(?:Frau|Herrn?)\b", head):
        return True
    if re.search(r"minister|sekret[äa]r|senator|b[üu]rgermeister|pr[äa]sident"
                 r"|berichterstatter|schriftf[üu]hrer|kanzler",
                 head, re.IGNORECASE):
        return True
    return False


def _entry_surname(head: str) -> str | None:
    """The surname from an index head, folded with br_02's own key.

    The head arrives already stripped of its page reference and leaders (see
    _toc_entries), so the last remaining token is the name — not, as an earlier
    version produced, the column letter of the page reference.
    """
    if _RE_ROLE_ONLY.match(head.strip()):
        return None                                         # continuation line
    # ⚠️ Cut at the (Land) bracket, do not merely delete it. An index entry is
    # "<name> (<Land>), <role>", and in the scans the role is routinely broken
    # across a hyphen with the comma mis-scanned as a low quote:
    #     Dr. Beyerle (Wurttemberg-]3aden) Be-. ; richterstaiter
    #     Dr. Dudek (Hambu1°g)‚ Bemchterstatter
    # Deleting the bracket and taking the LAST token then returns the mangled
    # role ("richterstaiter", "bemchterstatter") as the surname, and the speaker
    # — who was extracted correctly all along — reads as a miss. The bracket's
    # POSITION is reliable even when its contents are damaged beyond matching.
    cut = re.search(r"[({\[]", head)
    s = head[:cut.start()] if cut and cut.start() > 2 else head
    s = re.sub(r"[({\[][^)}\]]*[)}\]]", " ", s)             # any further bracket
    # ⚠️ Titles come off BEFORE the comma split. OCR prints "Dr." as "Dr,", so
    # splitting first cut "Dr, Dahlgrün, Bundesminister" down to "Dr" and lost
    # the name entirely.
    # ⚠️ The trailing \b is load-bearing. Without it "\bDr" matches the first two
    # letters of "Dreyer" and the surname comes back as "eyer" — silently, and
    # every such speaker then reads as a miss. Same for "Ing" in "Ingrid".
    s = re.sub(r"\b(?:Dr|Prof|Dipl|Ing)\b[.,]?", " ", s)    # drop titles
    s = re.sub(r"^\s*(?:Frau|Herrn?)\b", " ", s)
    s = re.split(r",", s)[0]                                # drop ", Minister ..."
    s = re.sub(r"[.,;:_·‘’‚„'\"\-\s]+$", "", s)
    toks = [t for t in re.split(r"[\s.]+", s) if len(t) > 2]
    if not toks:
        return None
    key = BR02._surname_key(toks[-1])
    # A residual that is itself a Bundesland is a wrapped role line
    # ("Ministerpräsident des Landes Niedersachsen"), not a speaker.
    if not key or len(key) < 3 or BR02._normalise_land(toks[-1]):
        return None
    return key


def _toc_entries(region: str) -> list[dict]:
    """Index entries as {surname, anlage, paired}.

    ⚠️ Two typographic regimes, and the difference decides what is measurable:

      * 1949-1999 (scans) put the entry on ONE line —
        `Hartinger (Bayern), Berichterstatter , . 151B` — so name and page
        reference pair exactly and the asterisk is attributable.

      * 2000+ (born-digital) is set in two columns that `pdftotext` scrambles;
        the leaders even land inside a wrapped word
        (`Hannelore Kraft (Nordrhein-West. . . . .` / `falen)`), and the page
        references arrive in a separate block. Those entries are marked
        paired=False: their asterisk CANNOT be recovered, so they are excluded
        from the spoken denominator rather than guessed at.

    Reporting the unpaired count is the honest form of the caveat the
    project notes record — a speech-level recall is not automatable for
    exactly this reason.
    """
    out: list[dict] = []
    lines = region.splitlines()
    for i, line in enumerate(lines):
        if not line.strip() or len(line.strip()) < 4:
            continue
        # ⚠️ Split on the PAGE REFERENCE first. An earlier version matched the
        # head with a greedy token pattern, which swallowed the reference and
        # left _entry_surname() returning the column letter ("a", "b", "d") as
        # the surname. The entry's structure is <head><leaders><ref>, so the
        # reference is the reliable landmark, not the head.
        ref = _RE_PAGE_REF.search(line)
        head = line[:ref.start()] if ref else line
        # Strip the dot leaders that run between the head and the reference.
        head = re.sub(r"[.,…‚’\s]{3,}$", "", head).strip()
        # A wrapped name continues on the next line ("(Nordrhein-West" / "falen)").
        if head.count("(") > head.count(")") and i + 1 < len(lines):
            head = (head + lines[i + 1].strip()).strip()

        # An entry's ROLE frequently wraps, leaving the page reference on a line
        # that carries no name at all:
        #     Dr, Dahlgrün, Bundesminister
        #     der Finanzen . .. .. .. „ 145 C
        # So when the head on the reference line yields no surname, retry with
        # up to two preceding lines prepended. Anchoring on the reference and
        # looking BACK is what makes a dot-leader index parseable at all.
        candidates = [head]
        if ref:
            for back in (1, 2):
                if i - back >= 0 and lines[i - back].strip():
                    candidates.append((lines[i - back].strip() + " " + head).strip())
        surname = None
        for cand in candidates:
            if len(cand) < 4 or not _looks_like_person(cand):
                continue
            surname = _entry_surname(cand)
            if surname:
                head = cand
                break
        if not surname:
            continue
        has_leaders = bool(re.search(r"[.,…‚’]\s*[.,…‚’]\s*[.,…‚’]", line))
        if ref:
            out.append({"surname": surname, "anlage": bool(ref.group(2)),
                        "paired": True, "head": head})
        elif has_leaders:
            out.append({"surname": surname, "anlage": False,
                        "paired": False, "head": head})
    # One speaker may be listed several times; recall is per speaker.
    best: dict[str, dict] = {}
    for e in out:
        prev = best.get(e["surname"])
        # A speaker listed once spoken and once in the Anlagen counts as spoken.
        if prev is None or (prev["anlage"] and not e["anlage"]) or \
           (not prev["paired"] and e["paired"]):
            best[e["surname"]] = e
    return list(best.values())


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _edits(a: str, b: str) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _matches(toc_key: str, found: set[str]) -> bool:
    """Fuzzy surname match — BOTH sides carry OCR damage.

    Exact first, then one edit for names long enough that a single edit cannot
    reach a different surname. Short keys are matched exactly, since at length
    <= 4 one edit spans genuinely different people (Katz/Kahn, Kopf/Kohl).
    """
    if toc_key in found:
        return True
    # Graduated, because the candidate set is ONE SESSION's speakers (a few
    # dozen), not the 2,405-name corpus vocabulary. Distance 2 across the whole
    # corpus is unsafe — the project notes record it merging distinct
    # politicians — but against a handful of within-session names it recovers
    # OCR damage like "H11pert" -> "Hilpert" without reaching a different
    # person. Short keys stay exact: at <= 4 characters one edit already
    # spans Katz/Kahn, Kopf/Kohl.
    if len(toc_key) <= 4:
        return False
    limit = 2 if len(toc_key) >= 7 else 1
    return any(_edits(toc_key, f) <= limit for f in found)


def _decade(year: int) -> str:
    return "1949–59" if year < 1960 else f"{year // 10 * 10}s"


def run(preprocessed: Path, sessions: list[str] | None,
        include_anlagen: bool, verbose: bool) -> list[dict]:
    import pandas as pd
    speeches = pd.read_parquet(_PROJECT_ROOT / "bundesrat_dataset_v1" /
                               "speeches.parquet")
    # ⚠️ SPOKEN rows only. The index entries counted here are the unasterisked
    # ones — speakers who spoke — so the set they are matched against must also
    # be speakers who spoke. Leaving written statements in would let a member
    # who only submitted to the record count as "recovered", inflating recall.
    if "contribution_type" in speeches.columns:
        speeches = speeches[speeches["contribution_type"] == "spoken"].copy()
    by_session = {sid: g for sid, g in speeches.groupby("session_id")}

    rows = []
    raw_files = sorted(preprocessed.glob("*/Session_*.txt"))
    raw_files = [f for f in raw_files if not f.stem.endswith("_cleaned")]
    for f in raw_files:
        sid = f.stem
        if sessions and sid.split("_")[-1].lstrip("0") not in sessions:
            continue
        g = by_session.get(sid)
        if g is None or not len(g):
            continue                      # the 4 closed sittings have no speech
        raw = f.read_text(encoding="utf-8", errors="replace")
        entries = _toc_entries(_toc_region(raw))
        if not entries:
            continue
        found = {BR02._surname_key(n.split()[-1])
                 for n in g["name"].dropna().astype(str) if n.split()}
        found |= {BR02._surname_key(t) for n in g["name"].dropna().astype(str)
                  for t in n.split() if len(t) > 3}
        found.discard("")

        listed = [e for e in entries
                  if e["paired"] and (include_anlagen or not e["anlage"])]
        hits = [e for e in listed if _matches(e["surname"], found)]
        misses = [e for e in listed if e not in hits]
        rows.append({
            "session_id": sid,
            "year": int(g["year"].iloc[0]),
            "decade": _decade(int(g["year"].iloc[0])),
            "toc_listed": len(listed),
            "toc_found": len(hits),
            "toc_missing": len(misses),
            "toc_anlagen": sum(1 for e in entries if e["paired"] and e["anlage"]),
            "toc_unpaired": sum(1 for e in entries if not e["paired"]),
            "missing_names": "; ".join(e["head"] for e in misses)[:400],
        })
        if verbose and misses:
            print(f"  {sid}: {len(hits)}/{len(listed)} — missing "
                  f"{[e['head'] for e in misses]}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preprocessed-dir", type=Path, default=_DEFAULT_PRE)
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    ap.add_argument("--session", default=None,
                    help="comma-separated session numbers")
    ap.add_argument("--include-anlagen", action="store_true",
                    help="count written statements as misses — reproduces the "
                         "WRONG number on purpose, for the paper's asterisk finding")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sessions = args.session.split(",") if args.session else None

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rows = run(args.preprocessed_dir, sessions, args.include_anlagen, args.verbose)
    if not rows:
        print("No sessions with a parseable index found.")
        return 1

    import pandas as pd
    df = pd.DataFrame(rows)
    order = ["1949–59", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]
    agg = (df.groupby("decade")[["toc_listed", "toc_found", "toc_missing",
                                 "toc_anlagen", "toc_unpaired"]].sum()
             .reindex([d for d in order if d in set(df.decade)]))
    agg["recall_pct"] = (agg.toc_found / agg.toc_listed * 100).round(1)
    total = agg[["toc_listed", "toc_found", "toc_missing",
                 "toc_anlagen", "toc_unpaired"]].sum()
    total["recall_pct"] = round(total.toc_found / total.toc_listed * 100, 1)
    agg.loc["ALL"] = total

    print(f"\nSessions with a parseable index: {len(df):,} of {df.session_id.nunique():,}")
    print(f"Mode: {'INCLUDING Anlagen (the wrong number)' if args.include_anlagen else 'spoken only'}\n")
    print(agg.to_string())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    agg.reset_index().to_csv(args.out, index=False, encoding="utf-8")
    df.to_csv(args.out.with_name(args.out.stem + "_per_session.csv"),
              index=False, encoding="utf-8")
    print(f"\nwrote {args.out}")
    print(f"wrote {args.out.with_name(args.out.stem + '_per_session.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
