#!/usr/bin/env python3
import argparse
import csv as _csv
import re
import json
import sys
import textwrap
from datetime import date
from pathlib import Path

import pandas as pd


def _load_br02():
    """br_02's name and Land helpers, imported rather than reimplemented.

    Copying _clean_name / _normalise_land here would let the two drift — the
    recurring failure mode in this project.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "br_02_helpers", Path(__file__).resolve().parent / "br_02_speaker_identifier.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Speech text fields can be very large (OCR sessions); lift the CSV field cap.
_csv.field_size_limit(10**7)

# Project root = parent of this file's directory (src/ → project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_INPUT  = _PROJECT_ROOT / "outputs_from_hpc"
_DEFAULT_OUTPUT = _PROJECT_ROOT / "bundesrat_dataset_v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_speech_id(session_id: str, sequence: int) -> str:
    """Stable composite key: 'Session_0941_0001'."""
    return f"{session_id}_{int(sequence):04d}"


def _modal(series: pd.Series):
    """Most frequent non-null value in a Series, or None if all null."""
    s = series.dropna()
    s = s[s.astype(str).str.strip() != ""]
    return s.mode().iloc[0] if not s.empty else None


def _safe_int(value, default=None):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_sessions(metadata_csv: Path, verbose: bool = True) -> list[dict]:
    """Read the metadata catalogue into session rows."""
    rows: list[dict] = []
    with open(metadata_csv, encoding="utf-8", newline="") as fh:
        for r in _csv.DictReader(fh):
            rows.append({
                "session_id":         r.get("session_id"),
                "session":            _safe_int(r.get("session")),
                "date_iso":           r.get("date_iso") or None,
                # renamed on the way out: "city" alone does not say city of what
                "meeting_city":       r.get("city") or None,
                "year":               _safe_int(r.get("year")),
                "source_pdf":         r.get("source_pdf") or None,
                "is_schreibmaschine": str(r.get("is_schreibmaschine", "")).strip().lower() == "true",
                "md5":                r.get("md5") or None,
                "engine":             r.get("engine") or None,
            })
    if verbose:
        print(f"  {len(rows):,} sessions from {metadata_csv.name}", flush=True)
    return rows


def load_speeches(input_dir: Path, verbose: bool = True) -> list[dict]:
    """Read every Session_*_speakers.csv into speech rows."""
    files = sorted(input_dir.glob("*/Session_*_speakers.csv"))
    rows: list[dict] = []
    for f in files:
        with open(f, encoding="utf-8", newline="") as fh:
            for r in _csv.DictReader(fh):
                session_id = r.get("session_id")
                seq        = _safe_int(r.get("sequence"), 0)
                rows.append({
                    "speech_id":   _make_speech_id(session_id, seq),
                    "session_id":  session_id,
                    "sequence":    seq,
                    "date_iso":    r.get("date_iso") or None,
                    "name":        r.get("name") or None,
                    "speaker_raw": r.get("speaker_raw") or None,
                    "role":        r.get("role") or None,
                    "land":        r.get("land") or None,
                    "text":        r.get("text") or None,
                    "char_offset": _safe_int(r.get("char_offset")),
                })
    if verbose:
        print(f"  {len(rows):,} speeches from {len(files):,} session CSVs", flush=True)
    return rows


# ---------------------------------------------------------------------------
# Static templates
# ---------------------------------------------------------------------------

_LICENSE_TEXT = """\
Creative Commons Attribution 4.0 International (CC BY 4.0)

You are free to:
  Share — copy and redistribute the material in any medium or format
  Adapt — remix, transform, and build upon the material for any purpose,
          even commercially.

Under the following terms:
  Attribution — You must give appropriate credit, provide a link to the
                license, and indicate if changes were made.

Full license text:
  https://creativecommons.org/licenses/by/4.0/legalcode

Note: The underlying Bundesrat Plenarprotokolle are published by the
Bundesrat. This derived dataset is released under CC BY 4.0.
"""


# --- gender, inferred from how the chamber addressed the speaker ------------
#
# German marks office nouns for gender, so the record states it directly:
# "Präsidentin", "Bundesministerin", "Berichterstatterin", plus the honorific
# "Frau". This is not a biographical claim — it records the form of address used
# in the official protocol.
#
# ⚠️ Read only the speaker's OWN office. A role reaches into somebody else's:
# "Parl. Staatssekretär bei der Bundesministerin für Umwelt" is a MAN deputising
# for a woman, and matching the whole string mislabels him. Validated against a
# first-name list, truncating at the first preposition took the disagreement rate
# from 14/576 to 3/576 — and all three residuals turned out to be bad person
# merges, not gender errors.
_ROLE_TAIL = re.compile(r"\s+(?:bei|beim|im|in|des|der|für|zur|zum|am|und)\b")
_GENDERED = r"\w*(?:pr[äa]sident|minister|senator|sekret[äa]r|meister|erstatter|f[üu]hrer|abgeordnet)"
_RE_FEM_ROLE = re.compile(_GENDERED + r"\w*in\b", re.I)
_RE_MASC_ROLE = re.compile(_GENDERED + r"(?!\w*in\b)\w*\b", re.I)
_RE_FRAU = re.compile(r"\bFrau\b")
_RE_HERR = re.compile(r"\bHerrn?\b")


def _own_office(role) -> str:
    """The speaker's own title, with any office they merely deputise for cut off."""
    r = str(role or "")
    m = _ROLE_TAIL.search(r)
    return r[:m.start()] if m else r


_NOBLE = re.compile(r"^(?:von\s+der|van\s+der|von|van|de|zu|vom|zur)\s+", re.I)
_NAME_TITLE = re.compile(
    r"^(?:Dr\.|Prof\.|Dipl\.-\S+|Dr\.-Ing\.|Ing\.|D\.|h\.\s*c\.|Frau|Herrn?)\s*", re.I)

try:
    import gender_guesser.detector as _gg
    _GUESSER = _gg.Detector(case_sensitive=False)
except ImportError:
    _GUESSER = None


def _first_name(name) -> str | None:
    """The given-name token, or None for a surname-only string.

    The noble particle has to go first, or "van Heukelum" yields "van".
    """
    n = str(name or "")
    prev = None
    while prev != n:
        prev = n
        n = _NAME_TITLE.sub("", n).strip()
    n = _NOBLE.sub("", n).strip()
    toks = n.split()
    return toks[0] if len(toks) > 1 and len(toks[0]) > 1 else None


def _guess_from_first_name(name) -> str | None:
    """Fallback: infer gender from the given name.

    ⚠️ Strictly a FALLBACK, never an override. Validated against the 550 persons
    whose gender the protocol states outright, it agrees 99.8 % of the time
    (506 covered, 1 disagreement) — and that one disagreement,
    "Dr. Cornelie Sonntag-Wolgast", is the guesser being wrong and the record
    being right. Record evidence therefore always wins.
    """
    if _GUESSER is None:
        return None
    f = _first_name(name)
    if not f:
        return None
    for candidate in (f, f.split("-")[0]):
        # German compound given names ("Karl-Josef", "Benjamin-Immanuel") are not
        # in the dictionary, but their first element is.
        if len(candidate) < 2:
            continue
        r = _GUESSER.get_gender(candidate)
        if r in ("female", "mostly_female"):
            return "female"
        if r in ("male", "mostly_male"):
            return "male"
    return None


_RE_MENTION = re.compile(
    r"\b(Frau|Herrn?)((?:\s+[A-ZÄÖÜ][\wäöüß.\-]*){1,5})")


def _gender_from_mentions(speeches_df, persons_df, unresolved) -> dict:
    """Third tier: how OTHER speakers refer to this person in the body text.

    The chair introduces speakers constantly — "Das Wort hat Herr Minister X",
    "Ich erteile Frau Senatorin Y das Wort" — so a person the attribution line
    never marks is often marked in somebody else's speech.

    ⚠️ Two gates, and both are needed. The mention carries only a SURNAME, so
    without them "Herr Beck" could be about any Beck in the corpus:
      * the surname must belong to exactly ONE person corpus-wide, and
      * the mention must fall inside that person's active years.
    Ungated, the scan returns 83 surnames including "Frau Deutschland" (a br_02
    junk string) and "Herr Adenauer" landing on the wrong Adenauer; gated, it
    returns 48, all consistent.
    """
    def _sur(n):
        toks = _NAME_TITLE.sub("", str(n or "")).split()
        return toks[-1] if toks else None
    sur_all = persons_df["person_name"].map(_sur)
    counts = sur_all.value_counts()
    want = {}
    for pid, nm, fy, ly in zip(persons_df["person_id"], sur_all,
                               persons_df["first_year"], persons_df["last_year"]):
        if pid in unresolved and nm and len(nm) >= 4 and counts.get(nm, 0) == 1:
            want[nm] = (pid, int(fy), int(ly))
    if not want:
        return {}
    tally = {}
    for txt, yr in zip(speeches_df["text"].fillna("").values,
                       speeches_df["year"].values):
        if "rau" not in txt and "Herr" not in txt:
            continue
        for m in _RE_MENTION.finditer(txt):
            # An honorific is separated from the surname by an arbitrary run of
            # titles — "Herr Minister", "Frau Parlamentarische Staatssekretärin",
            # "Herr Dr." — so capture the run and test every token in it rather
            # than assuming a fixed number of words.
            for tok in m.group(2).split():
                hit = want.get(tok.strip(".,;:!?"))
                if not hit:
                    continue
                pid, fy, ly = hit
                if not (fy - 2 <= yr <= ly + 2):
                    continue
                d = tally.setdefault(pid, {"female": 0, "male": 0})
                d["female" if m.group(1).lower() == "frau" else "male"] += 1
                break
    out = {}
    for pid, d in tally.items():
        if d["female"] and not d["male"]:
            out[pid] = "female"
        elif d["male"] and not d["female"]:
            out[pid] = "male"        # a mixed tally is refused, not majority-voted
    return out


def _gender_evidence(speeches_df: pd.DataFrame) -> pd.DataFrame:
    """Per NAME STRING: gender plus the evidence it rests on."""
    d = speeches_df[["name", "role", "speaker_raw"]].copy()
    own = d["role"].map(_own_office)
    raw = d["speaker_raw"].fillna("")
    d["fem"] = raw.str.contains(_RE_FRAU) | own.str.contains(_RE_FEM_ROLE)
    d["masc"] = raw.str.contains(_RE_HERR) | own.str.contains(_RE_MASC_ROLE)
    g = d.groupby("name").agg(fem=("fem", "any"), masc=("masc", "any"))
    # A string carrying BOTH is usually a generic-masculine role plus a "Frau"
    # elsewhere; the feminine marker is the informative one.
    g["gender"] = g.apply(
        lambda r: "female" if r.fem else ("male" if r.masc else None), axis=1)
    g["gender_source"] = g["gender"].map(lambda x: "address" if x else None)
    missing = g["gender"].isna()
    guessed = pd.Series(g.index[missing], index=g.index[missing]).map(
        _guess_from_first_name)
    g.loc[missing, "gender"] = guessed
    g.loc[missing, "gender_source"] = guessed.map(
        lambda x: "firstname" if x else None)
    return g[["gender", "gender_source"]]


# --- temporal plausibility of `land` ---------------------------------------
#
# A Bundesland cannot speak before it exists or after it is dissolved. The three
# founding Länder that formed the Südweststaat ceased on 25 April 1952, and the
# five new Länder were seated after reunification in 1990.
#
# ⚠️ This corrects an OCR fault, not the printed record. "Württemberg-Baden"
# survives twice after 1952 — RENNER in session 100 (1953) and Dr. VEIT in
# session 163 (1956) — and in BOTH cases the same speaker is attributed
# "(Baden-Württemberg)" elsewhere in the very same session (13x and 6x
# respectively). The scanner transposed the halves of the compound; the protocol
# says Baden-Württemberg.
#
# Only the dissolved-Land direction is repaired, because its successor is
# unambiguous. A new Land appearing too early has no such obvious fix and is
# left alone for the audit to report.
_LAND_DISSOLVED = {"Baden": 1952, "Württemberg-Baden": 1952,
                   "Württemberg-Hohenzollern": 1952}
_LAND_SUCCESSOR = "Baden-Württemberg"


# --- Anlagen: written statements, split out and re-attributed ---------------
#
# A member who does not speak may submit a statement IN WRITING ("zu Protokoll
# gegeben"); it is printed in the Anlagen at the back of the same volume and
# indexed with an asterisked page reference. That appendix is in the extracted
# text, and because it follows the close of the sitting it was being absorbed
# whole into whichever speech happened to be open — 896 rows across 732 sessions,
# about 19 % of all corpus text, attributed to the wrong person.
#
# Each block names its own author, so the fix ADDS data rather than discarding
# it: the appendix is cut off its host row and re-emitted as rows of its own,
# marked contribution_type="written", carrying the author, role and Bundesland
# from the header.
#
# ⚠️ This runs in br_03, not br_02, deliberately. The appendix text is already
# present in br_02's output, so splitting it here needs no re-extraction and
# leaves the HPC parity result standing. The cost is that the per-session CSVs
# still contain the unsplit form; they are the pipeline's intermediate, not the
# published artefact.
_RE_ERKL = r"Erkl[äaå]{1,2}rung"

# ⚠️ \Z, never $. Under re.M "$" matches at every LINE end, so a lazy head stops
# before a "(Land)" that wrapped onto the next line — which cost 1,131 of the
# Bundesland values on a first pass.
# ⚠️ Boundaries and headers are found SEPARATELY, in two stages.
#
# A single combined pattern (Anlage N + "Erklärung von" + head) looked right and
# was wrong in three ways, all visible only by reading the output:
#   * not every Anlage names an author — "Anlage 9 zu Punkt 24 der Tagesordnung"
#     is a statement with no "Erklärung von" line. Splitting only at NAMED
#     Anlagen silently glued those onto the previous statement: 32 rows ended up
#     over 40,000 characters, every one still containing an "Anlage N" marker.
#   * the body then began in the middle of the header — "23 der Tagesordnung\n\n
#     Nordrhein-Westfalen stimmt ..." — because the head capture ended at
#     "zu Punkt" and left the rest of the reference in the text.
#   * the agenda reference is often plural ("zu den Punkten 11 und 12"), which
#     the singular terminator missed entirely.
#
# So: _RE_ANLAGE_MARK finds every block boundary; _RE_ANLAGE_ATTRIB then parses
# the header of each block and, crucially, reports how much of it to consume.
_RE_ANLAGE_MARK = re.compile(r"Anlage\s+(?P<no>\d{1,3})\b")
# ⚠️ No \A here. This pattern is used as `.match(text, pos)`, which already
# anchors the attempt at pos — but \A still only matches at the TRUE start of
# the string, so including it made every header fail silently and the split
# produced nothing at all.
_RE_ANLAGE_ATTRIB = re.compile(
    # The header appears in three shapes: "Erklärung VON Minister X", the
    # genitive "Erklärung DES Ministers X", and "Bericht des ...". The
    # genitive was missing and put 43 plainly-named statements into the
    # unattributed `document` bucket.
    rf"\s*(?:{_RE_ERKL}|Bericht|Ausf[üu]hrungen|Rede)\s+(?:von|des|der)\s+"
    rf"(?P<head>(?:[^\n]|\n(?!\s*\n)){{3,140}}?)"
    # The agenda reference closes the header and must be consumed with it.
    rf"(?:\s+zu\s+(?:dem\s+|den\s+)?(?:Punkt|Tagesordnungspunkt)\w*"
    rf"[^\n]{{0,70}}?(?:der\s+Tagesordn\w+)?)?"
    rf"(?=\n|\Z)")

# ⚠️ The name's "(Land)" and the agenda reference routinely WRAP onto the next
# line, and no lookahead can express that cleanly: terminating on any newline
# leaves "{Schleswig-Holstein)" at the head of the statement text (Land coverage
# 65 % -> 32 %), while demanding a blank line instead rejects a third of the
# headers outright (3,750 statements -> 2,399). Both were tried. The wrap is
# therefore absorbed explicitly, one line at a time, in _split_anlagen().
_RE_ANLAGE_WRAP = re.compile(
    r"[ \t]*\n[ \t]*(?:"
    r"[({\[][^)}\]\n]{3,40}[)}\]]+"                       # wrapped "(Land)"
    r"|zu\s+(?:dem\s+|den\s+)?(?:Punkt|Tagesordnungspunkt)\w*[^\n]{0,70}"  # agenda ref
    r")[ \t]*")
_RE_ANLAGE_PAREN = re.compile(r"[({\[]([^)}\]\n]{3,40})[)}\]]+")
# The sitting's formal close; everything after it is appendix.
_RE_SCHLUSS = re.compile(r"\(\s*Schluss\s*(?:der\s+Sitzung)?\s*:", re.IGNORECASE)


def _parse_anlage_head(head: str, br02):
    """`Minister Dr. Schlegelberger {Schleswig-Holstein)` -> name/role/land."""
    head = " ".join(head.split())
    # ⚠️ The agenda reference is deliberately CONSUMED by the header match (so it
    # cannot leak into the statement text) but must not survive into the name:
    # left in, it produced 1,301 distinct "names" of the form
    # "Adorno zu Punkt 14 der Tagesordnung" and one speaker became a new person
    # per agenda item.
    head = re.sub(r"\s+zu\s+(?:dem\s+|den\s+)?(?:Punkt|Tagesordnungspunkt).*$",
                  "", head, flags=re.IGNORECASE | re.DOTALL)
    # ⚠️ The genitive header ("Erklärung DES MinisterS Meyer") leaves the role in
    # the GENITIVE, which _demote_role_prefix does not recognise — it knows
    # "Minister", not "Ministers". Left alone it produced canonical names like
    # "des Ministers Meyer", "Senators Weiß", "Ministers Kubel". Strip the
    # leftover article and fold the role back to the nominative so the existing
    # demotion can move it out of the name.
    head = re.sub(r"^\s*(?:des|der|von)\s+", "", head, flags=re.IGNORECASE)
    head = re.sub(r"^(Bundesminister|Ministerpr[äa]sident|Staatsminister"
                  r"|Staatssekret[äa]r|Senator|B[üu]rgermeister|Minister)"
                  r"(?:s|en)\b",
                  r"\1", head)
    p = _RE_ANLAGE_PAREN.search(head)
    land = br02._normalise_land(p.group(1)) if p else None
    # A doubled bracket ("{Bremen])") leaves an orphan behind after the strip.
    name = re.sub(r"[(){}\[\]]+", " ", _RE_ANLAGE_PAREN.sub(" ", head))
    parsed = {"name": br02._clean_name(br02._fix_caps_name(name.strip(" ,.:;"))),
              "role": None, "land": land}
    br02._demote_role_prefix(parsed)
    return parsed


def _split_anlagen(speeches_df, br02, verbose=True):
    """Cut absorbed appendices off their host speeches and re-emit them."""
    rows, edits = [], []
    for idx, r in speeches_df.iterrows():
        text = r["text"] or ""
        marks = list(_RE_ANLAGE_MARK.finditer(text))
        # Only an appendix that names at least one author is worth splitting; a
        # lone "Anlage 1" in prose ("aus der Anlage 1 der Drucksache") is not.
        named = [m for m in marks
                 if _RE_ANLAGE_ATTRIB.match(text, m.end())]
        if not named:
            continue
        close = _RE_SCHLUSS.search(text)
        cut = min([m.start() for m in ([close] if close else []) + named[:1]])
        if cut < 50:          # the whole row is appendix; nothing to keep
            continue
        edits.append((idx, cut))
        base = int(r["char_offset"]) if pd.notna(r.get("char_offset")) else 0
        # Walk EVERY marker, not just the named ones, so an unnamed statement
        # ends the block before it rather than being swallowed into it.
        marks = [m for m in marks if m.start() >= cut]
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            hm = _RE_ANLAGE_ATTRIB.match(text, m.end())
            if hm is None:
                # ⚠️ An appendix block with no author header is still corpus
                # text and MUST be emitted, with a null name. Using it only to
                # bound the block before it and then dropping it discarded
                # 2,395,943 words — 9.2 % of the corpus — silently. Some of
                # these are anonymous statements ("zu Punkt 25 der Tages-
                # ordnung"), others procedural documents circulated to the
                # chamber; neither has a speaker, and neither is ours to delete.
                body = text[m.end():end].strip()
                if len(body) >= 40:
                    new = dict(r)
                    new.update({
                        "name": None, "role": None, "land": None,
                        "speaker_raw": " ".join(m.group(0).split())[:200],
                        "text": body, "char_offset": base + m.start(),
                        "contribution_type": "document",
                        "person_id": None, "person_name": None,
                    })
                    rows.append(new)
                continue
            head, hend = hm.group("head"), hm.end()
            # Pull in up to two wrapped continuation lines — a "(Land)" and an
            # agenda reference can each sit on their own line under the name.
            for _ in range(2):
                wm = _RE_ANLAGE_WRAP.match(text, hend)
                if not wm:
                    break
                head += " " + wm.group(0).strip()
                hend = wm.end()
            body = text[hend:end].strip()
            if len(body) < 40:
                continue
            d = _parse_anlage_head(head, br02)
            if not d["name"]:
                continue
            # Inherit every session-level column from the host row (year,
            # meeting_city, engine …) rather than naming them: they are merged in
            # before this step, and listing them here would silently produce NA
            # columns the moment the schema gains a field.
            new = dict(r)
            new.update({
                "name": d["name"], "role": d["role"], "land": d["land"],
                "speaker_raw": " ".join(text[m.start():hend].split())[:200],
                "text": body, "char_offset": base + m.start(),
                "contribution_type": "written",
                "person_id": None, "person_name": None,
            })
            rows.append(new)
    for idx, cut in edits:
        speeches_df.at[idx, "text"] = speeches_df.at[idx, "text"][:cut].rstrip()
    if not rows:
        # The column must exist even when nothing was split — a later groupby
        # keys on it, and returning early without it raised KeyError.
        if "contribution_type" not in speeches_df.columns:
            speeches_df["contribution_type"] = "spoken"
        return speeches_df, 0
    add = pd.DataFrame(rows)
    # ⚠️ 'spoken' / 'written', not a boolean. The values are spelled out in
    # English for the same reason gender is 'male'/'female' rather than m/f:
    # these tables are read by people, and a German-named boolean would make
    # every non-German user look the meaning up.
    speeches_df["contribution_type"] = "spoken"
    out = pd.concat([speeches_df, add], ignore_index=True)
    out["contribution_type"] = out["contribution_type"].fillna("spoken")
    # Appendices follow the sitting, so they sort after every spoken turn.
    out["_written"] = (out["contribution_type"] == "written").astype(int)
    out = out.sort_values(["session_id", "_written", "char_offset"],
                          kind="stable").reset_index(drop=True)
    out = out.drop(columns="_written")
    out["sequence"] = out.groupby("session_id").cumcount() + 1
    out["speech_id"] = [_make_speech_id(s, q) for s, q
                        in zip(out["session_id"], out["sequence"])]
    if verbose:
        print(f"  written statements: split {len(add):,} out of "
              f"{len(edits):,} host speeches", flush=True)
    return out, len(add)


def _fix_anachronistic_land(speeches_df, verbose=True):
    land, year = speeches_df["land"], speeches_df["year"]
    bad = pd.Series(False, index=speeches_df.index)
    for name, ends in _LAND_DISSOLVED.items():
        bad |= (land == name) & (year > ends)
    n = int(bad.sum())
    if n:
        if verbose:
            for _, r in speeches_df.loc[bad].iterrows():
                print(f"  land: {r['session_id']} ({r['year']}) "
                      f"{r['land']!r} -> {_LAND_SUCCESSOR!r} "
                      f"[{r['name']}]", flush=True)
        speeches_df.loc[bad, "land"] = _LAND_SUCCESSOR
    return speeches_df, n


_RE_CHAIR = re.compile(r"r[äa]sident", re.I)
_RE_DEPUTY = re.compile(r"vize|amtierend|vertretend", re.I)


def _add_presiding_land(sessions_df, speeches_df, persons_df, verbose=True):
    """Attach the Bundesland holding the presidency at each sitting.

    Three passes:
      1. direct   — modal Land of the substantive President's speeches
      2. de-spike — an isolated session dissenting from its nearest known
                    neighbours is a deputy from another Land misread as the
                    President
      3. fill     — a gap takes a value only when the nearest known sessions on
                    BOTH sides agree, so nothing is invented across a changeover

    ⚠️ Both passes must compare against the nearest KNOWN sessions, not the
    adjacent slots. A spike is frequently flanked by gaps (30 March 1962 has an
    unresolved sitting either side), which hides it from an adjacency test and
    at the same time stops its neighbours being filled. Using adjacency the
    derivation yields 95 runs with 12 spurious short ones; using nearest-known
    it yields 77 runs — against ~76 actual annual terms — with a median length
    of 343 days and exactly one short run, the term still in progress at the end
    of the corpus.

    Only a run of ONE is collapsed, so a genuinely short presidency would
    survive. (Berlin's 1989/90 term under Momper is a full year and stays
    intact, ending in October 1990 with Hamburg following.)
    """
    land = dict(zip(persons_df["person_id"], persons_df["most_common_land"]))
    # ⚠️ SPOKEN rows only. Presiding is something done in the chamber, and a
    # written statement's role routinely contains "präsident" — its authors are
    # Ministerpräsidenten of their Länder, not the chair of this sitting. Leaving
    # them in put a spurious extra term into the rotation (78 runs against 77).
    src = speeches_df
    if "contribution_type" in src.columns:
        src = src[src["contribution_type"] == "spoken"]
    role = src["role"].fillna("")
    is_president = role.str.contains(_RE_CHAIR) & ~role.str.contains(_RE_DEPUTY)
    chair = src[is_president & src["person_id"].notna()].copy()
    chair["pl"] = chair["person_id"].map(land)
    chair = chair[chair["pl"].notna()]
    direct = chair.groupby("session_id")["pl"].agg(lambda s: s.mode().iloc[0])

    out = sessions_df.sort_values("session").reset_index(drop=True)
    v = [x if isinstance(x, str) and x else None
         for x in out["session_id"].map(direct)]
    n_direct = sum(x is not None for x in v)

    def neighbours(i, skip):
        """Nearest set values on either side, ignoring the indices in `skip`."""
        left = next((v[j] for j in range(i - 1, -1, -1)
                     if v[j] and j not in skip), None)
        right = next((v[j] for j in range(i + 1, len(v))
                      if v[j] and j not in skip), None)
        return left, right

    # ⚠️ Compare against the nearest KNOWN sessions, not the adjacent slots. A
    # spike is often flanked by gaps on both sides (session 1962-03-30 has an
    # unknown sitting either side of it), which makes it invisible to an
    # adjacency test and simultaneously blocks its neighbours from being filled.
    spikes = 0
    for i in range(len(v)):
        if not v[i]:
            continue
        run_alone = ((i == 0 or v[i - 1] != v[i])
                     and (i == len(v) - 1 or v[i + 1] != v[i]))
        if not run_alone:
            continue
        left, right = neighbours(i, skip={i})
        if left and left == right and left != v[i]:
            v[i] = left
            spikes += 1

    filled = 0
    for i in range(len(v)):
        if v[i]:
            continue
        left, right = neighbours(i, skip=set())
        if left and left == right:
            v[i] = left
            filled += 1

    out["presiding_land"] = v
    if verbose:
        cov = out["presiding_land"].notna().mean() * 100
        print(f"  presiding_land: {n_direct} direct, +{filled} filled, "
              f"{spikes} de-spiked -> {cov:.1f}% coverage", flush=True)
    return out


def _mirror_csv_resources(resources: list) -> list:
    """Declare a CSV twin for every Parquet resource, sharing its schema.

    Frictionless has no notion of "the same table in two formats", so each
    format is its own resource. Cloning keeps the field descriptions in one
    place — a hand-maintained second copy would drift.
    """
    out = []
    for r in resources:
        out.append(r)
        if not str(r.get("path", "")).endswith(".parquet"):
            continue
        csv_r = dict(r)
        csv_r["name"] = f"{r['name']}-csv"
        csv_r["path"] = r["path"].replace(".parquet", ".csv")
        csv_r["mediatype"] = "text/csv"
        csv_r["encoding"] = "utf-8"
        csv_r["dialect"] = {"delimiter": ",", "doubleQuote": True}
        csv_r["description"] = (r.get("description", "").rstrip(".")
                                + ". CSV copy of the Parquet resource above; "
                                  "identical contents. Text fields may contain "
                                  "embedded newlines inside quoted values.")
        out.append(csv_r)
    return out


def _make_datapackage(version: str) -> dict:
    return {
        "name":    "bundesrat-plenarprotokolle",
        "title":   "Bundesrat Plenarprotokolle Dataset",
        "version": version,
        "description": (
            "Structured, speaker-attributed speech corpus from German Bundesrat "
            "(Federal Council) plenary sessions, session 1 onward (1949–present). "
            "Members represent the 16 German Bundesländer, not parties. CC-BY 4.0."
        ),
        "licenses": [{
            "name":  "CC-BY-4.0",
            "title": "Creative Commons Attribution 4.0 International",
            "path":  "https://creativecommons.org/licenses/by/4.0/",
        }],
        "resources": [
            {
                "name": "sessions", "path": "sessions.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per plenary session.",
                "schema": {
                    "primaryKey": ["session_id"],
                    "fields": [
                        {"name": "session_id",  "type": "string",  "description": "Unique session key, e.g. 'Session_0941'."},
                        {"name": "session",     "type": "integer", "description": "Continuous Bundesrat session number (1949 = 1, no Wahlperiode)."},
                        {"name": "date_iso",    "type": "date",    "description": "Session date (YYYY-MM-DD)."},
                        {"name": "meeting_city","type": "string",  "description": "City the sitting was held in: 'Bonn' up to 1999, 'Berlin' from 2000."},
                        {"name": "year",        "type": "integer", "description": "Calendar year."},
                        {"name": "source_pdf",  "type": "string",  "description": "Source PDF path (relative to the raw-PDF root)."},
                        {"name": "is_schreibmaschine", "type": "boolean", "description": "True if text came from the typewritten Schreibmaschinenversion rather than the main PDF."},
                        {"name": "md5",         "type": "string",  "description": "MD5 of the source PDF."},
                        {"name": "presiding_land", "type": "string", "description": "Bundesland holding the Bundesrat presidency at this sitting. The presidency rotates annually among the Ministerpräsidenten (Königsteiner Vereinbarung). Derived from the corpus itself — the chair's resolved person and their modal Land — not from an external rotation calendar. Null where it could not be established."},
                        {"name": "engine",      "type": "string",  "description": "Extraction engine: 'pdftotext' (born-digital 2000+), 'pdftotext-raw', 'pdfminer', or 'ocr-tesseract' (scanned 1949–1999). Flags lower-quality (OCR) sessions."},
                        {"name": "n_speeches",  "type": "integer", "description": "Number of SPOKEN speeches in this session. Written statements are counted separately in n_written_statements, so that speeches-per-session keeps one meaning across the whole period."},
                        {"name": "n_written_statements", "type": "integer", "description": "Statements submitted in writing and printed in the Anlagen of this sitting, with a named author. Zero before the 1970s, when the practice begins."},
                        {"name": "n_documents_excluded", "type": "integer", "description": "Appendix items in this sitting that were NOT published: Umdruck sheets, Drucksachen and committee business, which have no personal author in the source and are therefore neither speech nor anyone's contribution. Recorded so the exclusion is visible per sitting rather than only in the paper."},
                    ],
                },
            },
            {
                "name": "speeches", "path": "speeches.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per attributed speech, including full text.",
                "schema": {
                    "primaryKey": ["speech_id"],
                    "foreignKeys": [{
                        "fields": ["session_id"],
                        "reference": {"resource": "sessions", "fields": ["session_id"]},
                    }],
                    "fields": [
                        {"name": "speech_id",   "type": "string",  "description": "Unique speech key '{session_id}_{sequence}', e.g. 'Session_0941_0001'."},
                        {"name": "session_id",  "type": "string",  "description": "Foreign key → sessions.session_id."},
                        {"name": "sequence",    "type": "integer", "description": "1-based order of the speech within its session."},
                        {"name": "date_iso",    "type": "date",    "description": "Session date (denormalized)."},
                        {"name": "year",        "type": "integer", "description": "Calendar year (denormalized)."},
                        {"name": "meeting_city","type": "string",  "description": "City the sitting was held in (denormalised from sessions)."},
                        {"name": "name",        "type": "string",  "description": "Speaker name, Title-cased. Pre-2000 sessions are often surname-only (e.g. 'Kopf'); born-digital sessions carry full names."},
                        {"name": "person_id",   "type": "string",  "description": "Resolved person key (foreign key to persons.person_id). Null where the name string could not be assigned to a person with confidence — see docs/person_review.csv."},
                        {"name": "person_name", "type": "string",  "description": "Canonical display name of the resolved person."},
                        {"name": "speaker_raw", "type": "string",  "description": "Raw attribution string as matched by br_02."},
                        {"name": "role",        "type": "string",  "description": "Parliamentary/official role: 'Präsident', 'Vizepräsident', 'Bundesminister der Finanzen', etc. Null for plain state representatives."},
                        {"name": "land",        "type": "string",  "description": "Bundesland the speaker represents — one of 19 values: the 16 current Länder plus Baden, Württemberg-Baden and Württemberg-Hohenzollern, which merged into Baden-Württemberg in 1952. See laender.csv. Null for the presiding officer and federal officials."},
                        {"name": "contribution_type", "type": "string", "description": "'spoken' — delivered in the chamber. 'written' — a statement submitted to the record (\"zu Protokoll gegeben\") and printed in the Anlagen, never spoken; its author is named in the appendix header. 'document' — appendix material whose header names no author: anonymous statements on an agenda item, and papers circulated to the chamber. The distinction between 'written' and 'document' records WHETHER THE RECORD NAMES AN AUTHOR, not a judgement about content. Every 'spoken' and 'written' row carries a name; 'document' rows carry none by definition. Written statements and documents exist from the 1970s onward; filter on 'spoken' to reproduce the validated series."},
                        {"name": "text",        "type": "string",  "description": "Full text (German). OCR sessions (1949–1999) may contain minor character errors."},
                        {"name": "char_offset", "type": "integer", "description": "Character offset of the speech start in the cleaned session text."},
                    ],
                },
            },
            {
                "name": "speakers", "path": "speakers.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per unique speaker name string, aggregated over BOTH spoken speeches and written statements — a member who only ever submitted in writing still appears here.",
                "schema": {
                    "primaryKey": ["name"],
                    "fields": [
                        {"name": "name",             "type": "string",  "description": "Unique speaker name (primary key). Name-string level: the same person may appear under variants across the 75-year span, especially surname-only vs full-name across the 2000 boundary."},
                        {"name": "n_speeches",       "type": "integer", "description": "Total speeches attributed to this name."},
                        {"name": "n_sessions",       "type": "integer", "description": "Distinct sessions in which this name spoke."},
                        {"name": "most_common_role", "type": "string",  "description": "Modal role across all speeches. Null if never assigned a role."},
                        {"name": "most_common_land", "type": "string",  "description": "Modal Bundesland across all speeches. Null for federal officials / presiding officers."},
                        {"name": "first_year",       "type": "integer", "description": "Earliest year this name appears."},
                        {"name": "last_year",        "type": "integer", "description": "Latest year this name appears."},
                        {"name": "person_id",        "type": "string",  "description": "Resolved person key (foreign key to persons.person_id), or null if unresolved."},
                        {"name": "gender",           "type": "string",  "description": "'female' / 'male' / null. Primary evidence is how the protocol addresses the speaker: the honorific 'Frau'/'Herr' and the gendered ending of their OWN office ('Präsidentin', 'Bundesministerin'). Where the record is silent, the given name is used instead. A record of address, not a biographical claim."},
                        {"name": "gender_source",    "type": "string",  "description": "'address' (stated in the speaker's own attribution), 'firstname' (inferred from the given name), 'mention' (another speaker refers to them as Frau/Herr in the body text), or null. Filter on 'address' for the strictest subset."},
                    ],
                },
            },
            {
                "name": "persons", "path": "persons.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": (
                    "One row per resolved PERSON. Name strings are grouped into people "
                    "by br_04 using Bundesland, first name, career era and office; only "
                    "unambiguous groupings are applied, and the residue is listed in "
                    "docs/person_review.csv for manual adjudication."
                ),
                "schema": {
                    "primaryKey": ["person_id"],
                    "fields": [
                        {"name": "person_id",        "type": "string",  "description": "Stable person key: folded surname + first year of appearance, e.g. 'rau-1970'."},
                        {"name": "person_name",      "type": "string",  "description": "Canonical display name (the fullest name string observed)."},
                        {"name": "gender",           "type": "string",  "description": "'female' / 'male' / null, aggregated over this person's name strings. Address evidence outranks a given-name inference; within each, any feminine marker makes the person female."},
                        {"name": "gender_source",    "type": "string",  "description": "'address', 'firstname' or 'mention' — see speakers.gender_source. Null where the protocols never mark it."},
                        {"name": "n_speeches",       "type": "integer", "description": "Total speeches by this person across all their name strings."},
                        {"name": "n_sessions",       "type": "integer", "description": "Distinct sessions in which this person spoke."},
                        {"name": "n_name_strings",   "type": "integer", "description": "How many raw name strings were merged into this person."},
                        {"name": "most_common_role", "type": "string",  "description": "Modal role across all their speeches."},
                        {"name": "most_common_land", "type": "string",  "description": "Modal Bundesland; null for federal officials and presiding officers."},
                        {"name": "first_year",       "type": "integer", "description": "Earliest year this person appears."},
                        {"name": "last_year",        "type": "integer", "description": "Latest year this person appears."},
                    ],
                },
            },
            # Written statements: same columns as speeches, different grain.
            # Declared by cloning the speeches schema at build time (below) so
            # the two cannot drift apart.
            {
                "name": "written_statements", "path": "written_statements.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": (
                    "One row per statement submitted to the record in writing "
                    "(\"zu Protokoll gegeben\") and printed in the Anlagen. These "
                    "are genuine contributions by named members who did not "
                    "speak — they were never delivered in the chamber, which is "
                    "why they are not in speeches.parquet. Same columns as "
                    "speeches. Exists from the 1970s onward."
                ),
                "schema": {"primaryKey": ["speech_id"], "fields": []},
            },
            # Reference table for the `land` field, published as CSV only — it is
            # 19 rows and is meant to be read by people as much as by code.
            # _mirror_csv_resources() leaves non-Parquet resources alone, so this
            # is declared once and not given a phantom Parquet twin.
            {
                "name": "laender", "path": "laender.csv",
                "mediatype": "text/csv",
                "encoding": "utf-8",
                "dialect": {"delimiter": ",", "doubleQuote": True},
                "description": (
                    "Reference table of the Bundesländer represented in the corpus: "
                    "the 16 current Länder plus the three that merged into "
                    "Baden-Württemberg in 1952. Resolves every value the `land` "
                    "field can take."
                ),
                "schema": {
                    "primaryKey": ["land"],
                    "fields": [
                        {"name": "land",       "type": "string",  "description": "Canonical Bundesland name, as used in speeches.land and sessions.presiding_land."},
                        {"name": "first_year", "type": "integer", "description": "First year this Land is OBSERVED in the corpus — not its constitutional founding date. The observed spans reproduce the territorial history unprompted, which is what makes the field self-validating."},
                        {"name": "last_year",  "type": "integer", "description": "Last year this Land is observed. 1952 for the three historical Länder; the final corpus year for the rest."},
                        {"name": "n_speeches", "type": "integer", "description": "Speeches attributed to a delegate of this Land. Sums to the number of speeches carrying a land."},
                        {"name": "n_sessions", "type": "integer", "description": "Distinct sessions in which a delegate of this Land spoke."},
                        {"name": "historic",   "type": "boolean", "description": "True for Baden, Württemberg-Baden and Württemberg-Hohenzollern, dissolved into Baden-Württemberg on 25 April 1952. They are kept under their own names rather than folded into the successor, which would rewrite the 1949-52 delegations."},
                    ],
                },
            },
        ],
    }


# Affiliation is identical for all four authors, so it is written once here rather
# than repeated in each entry.
_AFFILIATION = ("Economics of Sustainability, School of Business and Economics, "
                "RWTH Aachen University, 52072, Aachen, Germany")

_REPO_URL = "https://github.com/adama-njie/bundesrat-dataset"

# The Zenodo DOI for the DATASET.
#
# ⚠️ This is a RESERVED DOI on an unpublished draft. It is allocated but not yet
# registered with DataCite, so it does NOT resolve — anyone following it before
# the record is published gets a "DOI not found" page. It becomes live on publish
# and the string does not change.
#
# ⚠️ A reserved DOI is the VERSION DOI, not the concept DOI. Zenodo also mints a
# concept DOI that always points at the newest version, but it is only available
# once the record is published. When it is, add it as
#   identifiers:
#     - type: doi
#       value: "10.5281/zenodo.<concept>"
#       description: "Concept DOI (all versions)"
# and leave the version DOI in the top-level `doi:` field, which is what `version:`
# being present here implies.
#
# ⚠️ The code archive needs its OWN DOI. Depositing the pipeline is a separate
# Zenodo record; reusing this one for both would point a software citation at the
# data record.
_DOI = "10.5281/zenodo.22086439"

# ⚠️ CFF wants ONE list entry PER AUTHOR. Comma-joining the four names into a
# single entry is valid YAML and therefore fails silently — it parses as one
# person literally surnamed "Njie, Laperne, Torkayesh, Venghaus" with the given
# name "Adama, Leandro, Ali, Sandra", and that is what Zenodo, GitHub's "Cite
# this repository" button and cffconvert would render.
#
# ⚠️ An author with no ORCID omits the KEY. CFF has no null/empty form for it —
# `orcid: ""` fails schema validation, and Zenodo rejects a malformed identifier
# rather than ignoring it.
_AUTHORS = [
    ("Njie",       "Adama",   "0009-0008-2646-8791"),
    ("Laperne",    "Leandro", None),
    ("Torkayesh",  "Ali",     "0000-0002-1012-4213"),
    ("Venghaus",   "Sandra",  "0000-0002-9352-6604"),
]


def _make_citation_cff(version: str, year_min: int, year_max: int,
                       released: str | None = None) -> str:
    """Machine-readable citation metadata (Citation File Format 1.2.0).

    `released` pins date-released; it defaults to today, which is right for an
    actual release but means an ordinary rebuild rewrites the field.
    """
    authors = "\n".join(
        f'  - family-names: "{fam}"\n'
        f'    given-names: "{giv}"\n'
        f'    affiliation: "{_AFFILIATION}"'
        + (f'\n    orcid: "https://orcid.org/{orcid}"' if orcid else "")
        for fam, giv, orcid in _AUTHORS
    )
    return textwrap.dedent(f"""\
        cff-version: 1.2.0
        message: "If you use this dataset, please cite it using the metadata below."
        type: dataset
        title: "The Bundesrat Corpus: speaker and Bundesland-attributed proceedings of the German Federal Council, {year_min}–{year_max}"
        version: "{version}"
        date-released: "{released or date.today().isoformat()}"
        abstract: >
          Structured, speaker-attributed speech corpus from German Bundesrat
          (Federal Council) plenary sessions, {year_min}–{year_max}. Speakers are
          attributed with name, resolved person identity, gender, role, and
          represented Bundesland, plus full speech text.
        license: CC-BY-4.0
        repository-code: "{_REPO_URL}"
        doi: "{_DOI}"
        keywords:
          - Bundesrat
          - German federalism
          - parliamentary debates
          - natural language processing
          - political science
          - corpus linguistics
        authors:
        """) + authors + "\n"


def _make_readme(n_sessions, n_speeches, n_speakers, year_min, year_max,
                 engine_counts, version, n_persons=None, person_cov=None) -> str:
    _nl = chr(10)
    person_stat = (f"        | Resolved persons | {n_persons:,} |{_nl}"
                   if n_persons else "")
    person_file = (f"        | `persons.parquet` / `.csv` | {n_persons:,} | "
                   f"One row per resolved PERSON (name strings grouped into people) |{_nl}"
                   if n_persons else "")
    person_note = ("" if not n_persons else f"""
        ## Person-level identity

        `speeches.person_id` resolves name strings into people, so that
        `Dr. Voscherau` and `Dr. Henning Voscherau` are one man rather than two
        rows. {person_cov:.1f}% of speeches carry a `person_id`; the remainder are
        name strings whose assignment needs human judgement and are listed in
        `docs/person_review.csv` in the code repository.

        Grouping uses Bundesland, first name, career era and office, and is
        applied only where nothing is ambiguous. Cases that are deliberately NOT
        merged include `Dr. Ehard` / `Dr. Erhard` (Hans Ehard of Bayern versus
        Chancellor Ludwig Erhard), `Kaisen` / `Kaiser`, and the two Goppels —
        Alfons and his son Thomas, both sitting for Bayern.

        For analyses that need the raw attribution, `name` and `speaker_raw` are
        unchanged; `person_id` is purely additive.
""")
    eng_lines = "\n".join(
        f"        | {eng} | {cnt:,} |" for eng, cnt in engine_counts.items()
    )
    return textwrap.dedent(f"""\
        # Bundesrat Plenarprotokolle Dataset {version}

        Structured, speaker-attributed speech corpus from German **Bundesrat**
        (Federal Council) plenary sessions, {year_min}–{year_max}.

        The Bundesrat is a permanent body with continuous session numbering (no
        Wahlperiode). Its members represent the 16 German *Bundesländer* (states),
        not political parties — so speeches carry a `land` field, not a `party` field.

        | Statistic | Value |
        |---|---|
        | Sessions | {n_sessions:,} |
        | Speeches | {n_speeches:,} |
        | Unique speaker names | {n_speakers:,} |
{person_stat}        | Year span | {year_min}–{year_max} |

        ## Files

        Every table ships in **two formats with identical contents** — Parquet
        for analysis pipelines, CSV for Stata, SPSS, R and Excel.

        | Table | Rows | Description |
        |---|---|---|
        | `speeches.parquet` / `.csv` | {n_speeches:,} | One row per speech: name, person, role, land, full text |
        | `sessions.parquet` / `.csv` | {n_sessions:,} | One row per plenary session |
        | `speakers.parquet` / `.csv` | {n_speakers:,} | One row per unique speaker name string, with aggregates |
{person_file}        | `datapackage.json` | — | Frictionless Data schema (types, descriptions, FK constraints) |
        | `CITATION.cff` | — | Machine-readable citation metadata |

        ## Reading the CSVs

        UTF-8, comma-separated, **no byte-order mark**. `speeches.csv` is ~200 MB.

        ```python
        import pandas as pd
        speeches = pd.read_csv("speeches.csv")      # or read_parquet
        ```

        ```r
        speeches <- readr::read_csv("speeches.csv")
        ```

        Three things to know before opening these in a spreadsheet:

        - **Excel truncates long speeches.** A cell holds at most 32,767
          characters, and 538 speeches (1.2%) are longer — the longest is
          355,780. Excel will silently cut them. Use Parquet, R, Stata or
          pandas if you need the full text.
        - **`text` contains newlines** inside quoted fields, which is valid CSV
          but trips naive parsers. In Stata use
          `import delimited using speeches.csv, bindquote(strict) maxquotedrows(unlimited)`.
        - **Excel needs UTF-8 declared on import.** Double-clicking may render
          umlauts as `BÃ¤rbel`. Use Data → From Text/CSV and set File Origin to
          `65001: Unicode (UTF-8)`. The small tables open fine this way; for
          `speeches.csv`, prefer another tool.

        ## Extraction engines

        | Engine | Sessions |
        |---|---|
{eng_lines}

        `ocr-tesseract` marks the 1949–1999 scanned sessions recovered via OCR.
        For quality-sensitive NLP work, filter on `engine` (join via `session_id`).
{person_note}

        ## Quickstart (Python)

        ```python
        import pandas as pd
        speeches = pd.read_parquet("speeches.parquet")
        sessions = pd.read_parquet("sessions.parquet")

        # Speeches by a given Bundesland
        nrw = speeches[speeches["land"] == "Nordrhein-Westfalen"]

        # Presiding-officer (chair) speeches over time
        chairs = speeches[speeches["role"].isin(["Präsident", "Vizepräsident"])]

        # Restrict to born-digital (higher-quality) text
        born = speeches.merge(sessions[["session_id", "engine"]], on="session_id")
        born = born[born["engine"] == "pdftotext"]
        ```

        ## Data notes

        - **`land = null`** means the speaker did not represent a state in that
          attribution (presiding officers and federal ministers), not missing data.
        - **Speaker identity is name-string level.** Pre-2000 sessions attribute by
          surname only (e.g. "Kopf"); the same person may appear under different
          strings across the 75-year span. `speakers.parquet` does not resolve these
          into person-level identities.
        - **OCR text** (1949–1999, `engine = ocr-tesseract`) is readable but may
          contain occasional character errors inherent to scanning 75-year-old print.

        ## License

        CC-BY 4.0. See LICENSE. The underlying Plenarprotokolle are official
        documents of the Bundesrat.
        """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Package the Bundesrat corpus into a 3-table dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input-dir", default=str(_DEFAULT_INPUT), metavar="PATH",
                    help=f"Folder with the year subdirs + metadata/ (default: {_DEFAULT_INPUT})")
    ap.add_argument("--metadata-csv", default=None, metavar="PATH",
                    help="Path to metadata_catalogue.csv (default: <input-dir>/metadata/metadata_catalogue.csv)")
    ap.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT), metavar="PATH",
                    help=f"Destination for the packaged dataset (default: {_DEFAULT_OUTPUT})")
    ap.add_argument("--version", default="v1", help="Dataset version string (default: v1)")
    ap.add_argument("--release-date", default=None, metavar="YYYY-MM-DD",
                    help="date-released for CITATION.cff (default: today). Pin it "
                         "when cutting a release, so an ordinary rebuild does not "
                         "silently re-date the citation.")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress output")
    ap.add_argument("--person-links", default=None, metavar="PATH",
                    help="CSV mapping name -> person_id "
                         "(default: docs/person_links.csv, from br_04).")
    args = ap.parse_args()

    verbose    = not args.quiet
    input_dir  = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    metadata_csv = (Path(args.metadata_csv) if args.metadata_csv
                    else input_dir / "metadata" / "metadata_catalogue.csv")

    for p, label in [(input_dir, "--input-dir"), (metadata_csv, "metadata catalogue")]:
        if not p.exists():
            sys.exit(f"ERROR: {label} not found: {p}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load
    if verbose:
        print("Loading corpus …", flush=True)
    sessions_rows = load_sessions(metadata_csv, verbose)
    speeches_rows = load_speeches(input_dir, verbose)

    # 2. DataFrames
    sessions_df = pd.DataFrame(sessions_rows)
    speeches_df = pd.DataFrame(speeches_rows)

    # speech counts per session → sessions.n_speeches
    counts = speeches_df.groupby("session_id").size().rename("n_speeches")
    sessions_df = sessions_df.merge(counts, on="session_id", how="left")
    sessions_df["n_speeches"] = sessions_df["n_speeches"].fillna(0).astype("Int64")

    for col in ("session", "year"):
        sessions_df[col] = sessions_df[col].astype("Int64")
    for col in ("sequence", "char_offset"):
        speeches_df[col] = speeches_df[col].astype("Int64")

    # denormalized year + city onto speeches (from sessions)
    speeches_df["year"] = pd.to_datetime(
        speeches_df["date_iso"], errors="coerce").dt.year.astype("Int64")
    speeches_df = speeches_df.merge(
        sessions_df[["session_id", "meeting_city"]], on="session_id", how="left")

    # 3. Deduplicate (defensive — should be none)
    n_dup_sessions = int(sessions_df.duplicated("session_id").sum())
    if n_dup_sessions:
        sessions_df = sessions_df.drop_duplicates("session_id", keep="first")
        if verbose:
            print(f"  NOTE: dropped {n_dup_sessions} duplicate session_id rows.", flush=True)
    n_dup_speeches = int(speeches_df.duplicated("speech_id").sum())
    if n_dup_speeches:
        speeches_df = speeches_df.drop_duplicates("speech_id", keep="first")
        if verbose:
            print(f"  NOTE: dropped {n_dup_speeches} duplicate speech_id rows.", flush=True)

    # 3b. Split absorbed Anlagen (written statements) off their host speeches and
    # re-attribute them to their authors. Runs BEFORE the speaker aggregates, so
    # members who only ever submitted in writing appear in speakers/persons.
    speeches_df, _n_anl = _split_anlagen(speeches_df, _load_br02(), verbose=verbose)

    # Recount per session now that written statements are their own rows.
    #
    # ⚠️ `n_speeches` counts SPOKEN turns only, and written statements are
    # counted separately. Folding them together would silently change what
    # "speeches per session" means, and would do so only from the 1970s — when
    # the practice of submitting to the record begins — producing a step change
    # that looks historical and is not.
    _ct = (speeches_df.groupby(["session_id", "contribution_type"]).size()
           .unstack(fill_value=0))
    for col, src in (("n_speeches", "spoken"), ("n_written_statements", "written"),
                     ("n_documents_excluded", "document")):
        vals = _ct[src] if src in _ct.columns else 0
        sessions_df[col] = (sessions_df["session_id"].map(vals)
                            .fillna(0).astype("Int64"))

    # 4. speakers table (name-string level)
    if verbose:
        print("Building speakers table …", flush=True)
    grouped = speeches_df.groupby("name", as_index=False).agg(
        n_speeches      =("speech_id",  "count"),
        n_sessions      =("session_id", "nunique"),
        most_common_role=("role",       _modal),
        most_common_land=("land",       _modal),
        first_year      =("year",       "min"),
        last_year       =("year",       "max"),
    )
    for col in ("n_speeches", "n_sessions", "first_year", "last_year"):
        grouped[col] = grouped[col].astype("Int64")
    speakers_df = grouped
    _gev = _gender_evidence(speeches_df)
    speakers_df["gender"] = speakers_df["name"].map(_gev["gender"])
    speakers_df["gender_source"] = speakers_df["name"].map(_gev["gender_source"])

    # 4a2. repair anachronistic Land values (OCR transposition; see above)
    speeches_df, _n_land = _fix_anachronistic_land(speeches_df, verbose=verbose)

    # 4b. person layer (optional) — docs/person_links.csv, produced by br_04.
    #
    # Speaker identity is at the NAME-STRING level above; this resolves those
    # strings into people. The mapping is a reviewed artefact on disk rather
    # than code, exactly like docs/name_variants.csv: deciding that two strings
    # are one person is a research judgement. Delete the file and the dataset
    # falls back to name-string identity with person_id left null.
    persons_df = None
    links_path = Path(args.person_links) if args.person_links else (
        Path(__file__).resolve().parent.parent / "docs" / "person_links.csv")
    if links_path.exists():
        if verbose:
            print(f"Applying person links from {links_path.name} …", flush=True)
        links = pd.read_csv(links_path, dtype=str).fillna("")
        name2pid = dict(zip(links["name"], links["person_id"]))
        name2canon = dict(zip(links["name"], links["canonical_name"]))
        speeches_df["person_id"] = speeches_df["name"].map(name2pid)
        speeches_df["person_name"] = speeches_df["name"].map(name2canon)
        speakers_df["person_id"] = speakers_df["name"].map(name2pid)

        resolved = speeches_df[speeches_df["person_id"].notna()]
        # Address evidence outranks a first-name guess, so aggregate them
        # separately and let the stated form of address win.
        addr = dict(zip(speakers_df["name"],
                        speakers_df["gender"].where(
                            speakers_df["gender_source"] == "address")))
        anyg = dict(zip(speakers_df["name"], speakers_df["gender"]))
        resolved = resolved.assign(_a=resolved["name"].map(addr),
                                   _g=resolved["name"].map(anyg))
        def _pick(s):
            return ("female" if (s == "female").any()
                    else ("male" if (s == "male").any() else None))
        persons_df = resolved.groupby("person_id", as_index=False).agg(
            person_name     =("person_name", "first"),
            _addr           =("_a", _pick),
            _any            =("_g", _pick),
            n_speeches      =("speech_id",   "count"),
            n_sessions      =("session_id",  "nunique"),
            n_name_strings  =("name",        "nunique"),
            most_common_role=("role",        _modal),
            most_common_land=("land",        _modal),
            first_year      =("year",        "min"),
            last_year       =("year",        "max"),
        )
        persons_df["gender"] = persons_df["_addr"].fillna(persons_df["_any"])
        persons_df["gender_source"] = persons_df.apply(
            lambda r: "address" if r["_addr"] else
                      ("firstname" if r["_any"] else None), axis=1)
        persons_df = persons_df.drop(columns=["_addr", "_any"])
        still = set(persons_df.loc[persons_df["gender"].isna(), "person_id"])
        if still:
            mentions = _gender_from_mentions(speeches_df, persons_df, still)
            if mentions:
                fill = persons_df["person_id"].map(mentions)
                persons_df["gender"] = persons_df["gender"].fillna(fill)
                persons_df.loc[fill.notna() & persons_df["gender_source"].isna(),
                               "gender_source"] = "mention"
                if verbose:
                    print(f"  gender: +{len(mentions)} from third-person "
                          f"mentions in speech text", flush=True)
        for col in ("n_speeches", "n_sessions", "n_name_strings",
                    "first_year", "last_year"):
            persons_df[col] = persons_df[col].astype("Int64")
        persons_df = persons_df.sort_values(
            "n_speeches", ascending=False).reset_index(drop=True)
        if verbose:
            cov = speeches_df["person_id"].notna().mean() * 100
            print(f"  {len(persons_df):,} persons; {cov:.1f}% of speeches carry a person_id",
                  flush=True)
    else:
        speeches_df["person_id"] = pd.NA
        speeches_df["person_name"] = pd.NA
        speakers_df["person_id"] = pd.NA
        if verbose:
            print(f"  NOTE: {links_path.name} not found — person layer skipped.",
                  flush=True)

    # 4c. presiding_land — which Bundesland held the presidency at each sitting.
    #
    # The Bundesrat presidency rotates annually among the Ministerpräsidenten
    # (Königsteiner Vereinbarung), so the chair's Land is a session-level
    # attribute of real research value: it attributes the ~49 % of speeches made
    # from the chair, which carry no `land` of their own.
    #
    # It is derived from the corpus rather than from an external rotation
    # calendar, which makes it self-validating — and the derivation reproduces
    # the historical sequence exactly (Arnold/NRW → Ehard/Bayern → Kopf/
    # Niedersachsen → Maier/Württemberg-Baden → Zinn/Hessen → Altmeier/
    # Rheinland-Pfalz), with a median run of 329 days.
    #
    # ⚠️ Do NOT key this to a fixed 1-November term boundary. The changeover
    # date differed in the early years, and a fixed boundary mislabels every
    # term from 1949 to the mid-1950s — the give-away is that the minority Land
    # of each mis-cut term is the majority of the next one.
    if persons_df is not None:
        sessions_df = _add_presiding_land(sessions_df, speeches_df, persons_df,
                                          verbose=verbose)
    else:
        sessions_df["presiding_land"] = pd.NA

    # 5. Column order
    sessions_df = sessions_df[[
        "session_id", "session", "date_iso", "meeting_city", "year",
        "source_pdf", "is_schreibmaschine", "md5", "engine", "presiding_land",
        "n_speeches", "n_written_statements", "n_documents_excluded",
    ]].sort_values("session").reset_index(drop=True)

    speeches_df = speeches_df[[
        "speech_id", "session_id", "sequence", "date_iso", "year", "meeting_city",
        "name", "person_id", "person_name", "speaker_raw", "role", "land",
        "contribution_type", "text", "char_offset",
    ]].sort_values(["session_id", "sequence"]).reset_index(drop=True)
    # NOTE: contribution_type is retained on both tables. It is constant within
    # each ('spoken' / 'written'), which is redundant but makes a concatenation
    # of the two self-describing — the common way users will want them.

    # 6. Integrity checks
    if not speeches_df["speech_id"].is_unique:
        n = int(speeches_df.duplicated("speech_id").sum())
        sys.exit(f"FATAL: speech_id not unique ({n} duplicates).")
    if not speeches_df["session_id"].isin(sessions_df["session_id"]).all():
        n_bad = int((~speeches_df["session_id"].isin(sessions_df["session_id"])).sum())
        sys.exit(f"FATAL: {n_bad} speeches reference unknown session_id.")

    if persons_df is not None:
        known = set(persons_df["person_id"])
        bad = speeches_df.loc[speeches_df["person_id"].notna()
                              & ~speeches_df["person_id"].isin(known)]
        if len(bad):
            sys.exit(f"FATAL: {len(bad)} speeches reference unknown person_id.")
        if not persons_df["person_id"].is_unique:
            sys.exit("FATAL: person_id not unique in persons table.")

    # 7. Write
    if verbose:
        print("Writing output files …", flush=True)

    def _write(df, name, **kw):
        path = output_dir / name
        if name.endswith(".parquet"):
            df.to_parquet(path, index=False)
        else:
            df.to_csv(path, index=False, **kw)
        if verbose:
            print(f"  {name:<22} {path.stat().st_size/1e6:6.1f} MB", flush=True)

    # Every table is written twice: Parquet for analysis pipelines, CSV for
    # Stata / SPSS / R users and anyone without a Parquet reader. The CSV is the
    # same data, UTF-8, comma-separated, with no byte-order mark.
    #
    # ⚠️ No BOM, deliberately. A BOM makes Excel auto-detect UTF-8 on
    # double-click, but pandas then reads the first column name as
    # "﻿session_id" unless the caller passes encoding="utf-8-sig". A
    # published table should read cleanly in code by default; the Excel import
    # route is documented in the README instead.
    # ⚠️ `speeches` MEANS SPOKEN. Written statements go to their own table and
    # institutional papers are dropped entirely.
    #
    # A statement submitted to the record ("zu Protokoll gegeben") is a genuine
    # contribution by a named member who could not attend — it belongs in the
    # dataset. It is NOT a speech: it was never delivered, which is precisely why
    # the printed index marks it with an asterisk. Keeping both in one table
    # called `speeches` meant 15 % of its rows contradicted its name, and a
    # reader who counted rows got a different number from the one in the paper.
    #
    # The `document` rows — Umdruck, Drucksache, committee business — have no
    # personal author even in the source. They are neither speech nor anyone's
    # contribution, so they are not published as either.
    written_df = speeches_df[speeches_df["contribution_type"] == "written"].copy()
    n_dropped = int((speeches_df["contribution_type"] == "document").sum())
    speeches_df = speeches_df[speeches_df["contribution_type"] == "spoken"].copy()
    for _d in (speeches_df, written_df):
        _d["sequence"] = _d.groupby("session_id").cumcount() + 1
        _d["speech_id"] = [_make_speech_id(s, q) for s, q
                           in zip(_d["session_id"], _d["sequence"])]
    if verbose:
        print(f"  speeches: {len(speeches_df):,} spoken | "
              f"written_statements: {len(written_df):,} | "
              f"dropped {n_dropped:,} institutional documents", flush=True)

    tables = [(sessions_df, "sessions"), (speeches_df, "speeches"),
              (written_df, "written_statements"), (speakers_df, "speakers")]
    if persons_df is not None:
        tables.append((persons_df, "persons"))

    for df, name in tables:
        _write(df, f"{name}.parquet")
    for df, name in tables:
        if verbose and name == "speeches":
            print("  speeches.csv           (writing, ~195 MB) …", flush=True)
        _write(df, f"{name}.csv", encoding="utf-8")

    # Reference table for the `land` field — the Bundesrat analogue of a party
    # reference table. Lets a user resolve the 19 values, and see which three are
    # historical, without reading the paper.
    #
    # ⚠️ first_year / last_year are OBSERVED in the corpus, not constitutional
    # dates. That is the point: they reproduce the territorial history unprompted
    # (the three founding Länder ending in 1952, Baden-Württemberg beginning in
    # 1952, the Saarland acceding in 1957, the five new Länder in 1990-91), which
    # is what makes the field self-validating. Hard-coding the statutory dates
    # would destroy that property.
    laender_df = (
        speeches_df[speeches_df["land"].notna()]
        .groupby("land")
        .agg(first_year=("year", "min"), last_year=("year", "max"),
             n_speeches=("speech_id", "size"),
             n_sessions=("session_id", "nunique"))
        .reset_index()
        .sort_values(["first_year", "land"])
    )
    # Derived from _LAND_DISSOLVED rather than a second hand-written list, which
    # would be free to drift away from it.
    laender_df["historic"] = laender_df["land"].isin(_LAND_DISSOLVED)
    _write(laender_df, "laender.csv", encoding="utf-8")

    dp = _make_datapackage(args.version)
    # written_statements shares the speeches schema; clone it rather than
    # maintaining a second copy that would drift.
    _by = {r["name"]: r for r in dp["resources"]}
    if "written_statements" in _by and "speeches" in _by:
        _by["written_statements"]["schema"]["fields"] = list(
            _by["speeches"]["schema"]["fields"])
    dp["resources"] = _mirror_csv_resources(dp["resources"])
    with open(output_dir / "datapackage.json", "w", encoding="utf-8") as fh:
        json.dump(dp, fh, indent=2, ensure_ascii=False)
    n_sessions = len(sessions_df)
    n_speeches = len(speeches_df)
    n_speakers = len(speakers_df)
    year_min   = int(sessions_df["year"].min())
    year_max   = int(sessions_df["year"].max())
    engine_counts = sessions_df["engine"].value_counts().to_dict()

    # Written after the span is known: the year range in the title and abstract is
    # DERIVED, not typed. A hard-coded "1949–present" is the same defect that put
    # a stale "1949–2025" on a figure — it survives the corpus being extended.
    with open(output_dir / "CITATION.cff", "w", encoding="utf-8") as fh:
        fh.write(_make_citation_cff(args.version, year_min, year_max,
                                    released=args.release_date))
    with open(output_dir / "LICENSE", "w", encoding="utf-8") as fh:
        fh.write(_LICENSE_TEXT)

    with open(output_dir / "README.md", "w", encoding="utf-8") as fh:
        fh.write(_make_readme(n_sessions, n_speeches, n_speakers,
                              year_min, year_max, engine_counts, args.version,
                              n_persons=(len(persons_df) if persons_df is not None else None),
                              person_cov=(speeches_df["person_id"].notna().mean() * 100
                                          if persons_df is not None else None)))
    if verbose:
        for extra in ("datapackage.json", "CITATION.cff", "LICENSE", "README.md"):
            print(f"  {extra:<22} (written)", flush=True)

    # 8. Summary
    if verbose:
        print("\n" + "=" * 56)
        print("DATASET SUMMARY")
        print("=" * 56)
        print(f"  Sessions:              {n_sessions:>10,}")
        print(f"  Speeches:              {n_speeches:>10,}")
        print(f"  Unique speaker names:  {n_speakers:>10,}")
        print(f"  Year span:             {year_min}–{year_max}")
        print(f"  Null role:             {speeches_df['role'].isna().mean()*100:>9.1f}%")
        print(f"  Null land:             {speeches_df['land'].isna().mean()*100:>9.1f}%")
        print("\n  Engine breakdown (sessions):")
        for eng, cnt in engine_counts.items():
            print(f"    {str(eng):<15} {cnt:>6,}")
        print(f"\n  Output: {output_dir.resolve()}")
        print("=" * 56)
        print("All integrity checks passed.")


if __name__ == "__main__":
    main()
