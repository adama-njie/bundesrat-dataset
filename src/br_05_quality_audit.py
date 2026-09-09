#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "bundesrat_dataset_v1"
DEFAULT_OUT = ROOT / "outputs"

# The 19 Bundesländer the corpus may legitimately carry — 16 current plus the
# three that merged into Baden-Württemberg in 1952.
VALID_LAENDER = frozenset({
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen", "Hamburg",
    "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen", "Nordrhein-Westfalen",
    "Rheinland-Pfalz", "Saarland", "Sachsen", "Sachsen-Anhalt",
    "Schleswig-Holstein", "Thüringen",
    "Baden", "Württemberg-Baden", "Württemberg-Hohenzollern",
})

# --- CONTENT patterns -------------------------------------------------------
# Running page header. Keyed on the trailing DATE rather than on prefix junk —
# "Der Bundesrat hat in seiner 9. Sitzung beschlossen" must not match.
RE_PAGE_HEADER = re.compile(
    r"Bundesrat[^\w\n]{0,4}\s*\d{1,4}\s*[.,]?\s*Sitzung[^\w\n]{0,4}\s*"
    r"\d{1,2}\s*[.,]\s*[A-Za-zäöü]{3,10}\s*\d{4}")

# A column/margin marker of the two-column layout, in any bracket combination.
RE_COLUMN_MARKER = re.compile(r"[({\[][ABCD][)}\]]")

# The same marker splitting a word — unambiguous column bleed.
RE_MARKER_INWORD = re.compile(r"[a-zäöüß][({\[][ABCD][)}\]][ \t]*[a-zäöüß]")

RE_CID = re.compile(r"\(cid:\d+\)")

# A word broken at a line end whose hyphen was never rejoined.
RE_HYPHEN_ARTEFACT = re.compile(r"\w[-­]\n+[a-zäöüß]")

# An attribution-shaped line sitting inside a body: "Name (Bundesland):".
RE_ATTRIBUTION_IN_BODY = re.compile(
    r"\n[ \t]*[A-ZÄÖÜ][^\n:(]{1,60}\((?:" + "|".join(
        re.escape(x) for x in sorted(VALID_LAENDER)) + r")\)[^\n:]{0,30}:")

# Chair roles used to build the running-header candidates.
_ROLE_PREFIXES = (
    "", "Präsident ", "Präsidentin ", "Vizepräsident ", "Vizepräsidentin ",
    "Amtierender Präsident ", "Amtierende Präsidentin ", "Alterspräsident ",
    "Bundespräsident ", "Bundeskanzler ", "Ministerpräsident ",
)

# A Bundesland cannot speak before it exists or after it is dissolved. The three
# founding Länder formed the Südweststaat on 25 April 1952; the five new Länder
# were seated after reunification in 1990.
LAND_DISSOLVED = {"Baden": 1952, "Württemberg-Baden": 1952,
                  "Württemberg-Hohenzollern": 1952}
LAND_CREATED = {"Baden-Württemberg": 1952, "Saarland": 1957,
                "Sachsen": 1990, "Mecklenburg-Vorpommern": 1990,
                "Sachsen-Anhalt": 1990, "Brandenburg": 1990, "Thüringen": 1990}

OVERLENGTH_CHARS = 80_000
SHORT_BODY_CHARS = 20

# A ratcheting gate: each check may not EXCEED its baseline. Most are 0 and must
# stay there. Two carry a known, documented backlog instead.
#
# ⚠️ A gate that is permanently red gets ignored, so a check that has never been
# zero does not belong at zero. `attribution_in_body` started at 70 genuine missed
# attributions the audit itself found — single-character OCR damage inside the
# name, between name and paren, or in the role word. They are a work list, not a
# regression. Lower the baseline as they are fixed; never raise it to make a
# failure go away.
#
# ⚠️ 31 is close to the FLOOR, not a backlog of 31 fixable rows. 15 of the
# remaining matches are things the check is *supposed* to catch and br_02 is
# right to reject — the chair naming somebody rather than that person speaking:
#   "Das Wort hat Minister Kaesler (Sachsen-Anhalt):"
#   "Ich erkläre für die Staatsministerin Iris Blaul (Hessen):"   (a declaration
#      read out ON BEHALF of Blaul — the speaker is the reader, not Blaul)
#   "Ich darf Frau Ministerin Brunn (Nordrhein-Westfalen) das Wort erteilen:"
# They match because the shape "Name (validated Land):" is genuinely present in
# the line. Do NOT chase them to zero; a fix that swallowed them would attribute
# speeches to the wrong person. The ~16 that are real are specks INSIDE an
# ALL-CAPS surname ("VOIG'I‘", "Dr. SPIE”KER", "SC’H'WER'I‘NER", "Dr. NEVERMA.NN")
# plus a handful of lines where column reconstruction glued prose onto the
# attribution ("Wir set-Dr. VEIT (Baden-Württemberg), Berichterstatter:").
#
# ⚠️ Re-based 31 -> 34 in Sep 2026, and the reason matters: the corpus gained
# 3,725 rows when written statements were split out of their host speeches,
# so the DENOMINATOR changed structurally — the parser did not regress. The
# composition was checked before re-basing: 25 matches sit in spoken rows and
# 9 in written ones, and all 9 are the "Ich erkläre für die Staatsministerin
# Iris Blaul (Hessen):" class, i.e. the correct rejections described above,
# which simply moved into the written rows where they belong. Re-basing is
# only ever legitimate with that kind of check; never do it to clear a red.
REGRESSION_BASELINE = {
    "page_header": 0,
    "marker_inword": 0,
    "cid": 0,
    "null_name": 0,
    "invalid_land": 0,
    "anachronistic_land": 0,
    "field_whitespace": 0,
    "empty_body": 0,
    "hyphen_artefact": 1,        # one residual, documented
    "attribution_in_body": 25,   # see above (70 -> 31 Aug 2026; 34 after the Anlagen
                                 # split; 25 once br_01 stopped deleting appendix
                                 # author lines, which removed 9 of the matches)
}
REGRESSION_CHECKS = list(REGRESSION_BASELINE)


def _standalone_name(name: str, text: str) -> bool:
    """True when the speaker's own name sits as its own line inside the body.

    ⚠️ The blank-line form is required deliberately. A single-newline test
    produces mass false positives, because OCR wraps lines at arbitrary widths
    and a speaker's surname routinely lands at a line boundary inside a
    paragraph.
    """
    if not name or not text:
        return False
    for p in _ROLE_PREFIXES:
        cand = p + name
        if ("\n\n" + cand + "\n") in text or text.startswith(cand + "\n"):
            return True
    return False


CHECKS: list[tuple[str, str, str]] = [
    ("page_header",         "CONTENT",   "Page header inside a speech"),
    ("running_header",      "CONTENT",   "Speaker's own name as a standalone line"),
    ("column_marker",       "CONTENT",   "Column marker (A)-(D) anywhere"),
    ("marker_inword",       "CONTENT",   "Column marker splitting a word"),
    ("cid",                 "CONTENT",   "(cid:N) font artefact"),
    ("hyphen_artefact",     "CONTENT",   "Unrejoined end-of-line hyphen"),
    ("attribution_in_body", "CONTENT",   "Attribution line inside a body"),
    ("null_name",           "STRUCTURE", "Null or empty speaker name"),
    ("field_whitespace",    "STRUCTURE", "Leading/trailing whitespace in a field"),
    ("invalid_land",        "STRUCTURE", "land value outside the 19 Bundesländer"),
    ("anachronistic_land",  "STRUCTURE", "land speaking before it existed / after it was dissolved"),
    ("empty_body",          "STRUCTURE", "Empty speech body"),
    ("short_body",          "STRUCTURE", f"Body under {SHORT_BODY_CHARS} chars"),
    ("overlength",          "INFO",      f"Speech over {OVERLENGTH_CHARS:,} chars"),
    ("starts_with_name",    "INFO",      "Body opens with the speaker's own name"),
]


def run_audit(df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    text = df["text"].fillna("")
    name = df["name"].fillna("")

    df["page_header"] = text.str.contains(RE_PAGE_HEADER, na=False)
    df["column_marker"] = text.str.contains(RE_COLUMN_MARKER, na=False)
    df["marker_inword"] = text.str.contains(RE_MARKER_INWORD, na=False)
    df["cid"] = text.str.contains(RE_CID, na=False)
    df["hyphen_artefact"] = text.str.contains(RE_HYPHEN_ARTEFACT, na=False)
    df["attribution_in_body"] = text.str.contains(RE_ATTRIBUTION_IN_BODY, na=False)

    # ⚠️ A 'document' row has no speaker BY DEFINITION — it is appendix material
    # whose header names no author. Only spoken turns and named written
    # statements are required to carry a name, so the check is scoped to them;
    # counting documents here would put a permanent 4,550 against a zero
    # baseline and the gate would stop meaning anything.
    _named_expected = (df["contribution_type"] != "document"
                       if "contribution_type" in df.columns else True)
    df["null_name"] = (name.str.strip() == "") & _named_expected
    ws = pd.Series(False, index=df.index)
    for col in ("name", "role", "land", "speaker_raw"):
        if col in df.columns:
            v = df[col].fillna("")
            ws |= (v != v.str.strip()) & (v != "")
    df["field_whitespace"] = ws
    land = df["land"].fillna("")
    df["invalid_land"] = (land != "") & ~land.isin(VALID_LAENDER)
    anach = pd.Series(False, index=df.index)
    for nm, ends in LAND_DISSOLVED.items():
        anach |= (land == nm) & (df["year"] > ends)
    for nm, starts in LAND_CREATED.items():
        anach |= (land == nm) & (df["year"] < starts)
    df["anachronistic_land"] = anach

    df["empty_body"] = text.str.strip() == ""
    df["short_body"] = (~df["empty_body"]) & (text.str.strip().str.len() < SHORT_BODY_CHARS)
    df["overlength"] = text.str.len() > OVERLENGTH_CHARS

    if verbose:
        print("  row-wise checks (running header, starts-with-name) …", flush=True)
    df["running_header"] = [_standalone_name(n, t) for n, t in zip(name, text)]
    df["starts_with_name"] = [
        bool(n) and t.lstrip().startswith(n) for n, t in zip(name, text)]
    return df


def print_report(df: pd.DataFrame, sessions: pd.DataFrame | None) -> int:
    total = len(df)
    print()
    print("=" * 72)
    print("BUNDESRAT QUALITY AUDIT")
    print("=" * 72)
    print(f"Speeches examined: {total:,}")
    print()
    print(f"{'Check':<46}{'Count':>9}{'% ':>9}  Group")
    print("-" * 72)
    regressions = 0
    for col, group, label in CHECKS:
        n = int(df[col].sum())
        pct = n / total * 100 if total else 0.0
        mark = ""
        if col in REGRESSION_BASELINE:
            base = REGRESSION_BASELINE[col]
            if n > base:
                mark = f"  <-- OVER BASELINE ({base})"
                regressions += 1
            elif n < base:
                mark = f"  OK (under baseline {base} — lower it)"
            else:
                mark = "  OK" if base == 0 else f"  OK (baseline {base})"
        print(f"{label:<46}{n:>9,}{pct:>8.2f}%  {group}{mark}")

    if sessions is not None and "engine" in sessions.columns:
        m = df.merge(sessions[["session_id", "engine"]], on="session_id", how="left")
        print()
        print("By extraction engine (share of speeches flagged):")
        print(f"  {'check':<28}" + "".join(f"{e:>16}" for e in sorted(m.engine.dropna().unique())))
        for col, group, _lbl in CHECKS:
            if group == "INFO":
                continue
            row = m.groupby("engine")[col].mean() * 100
            if row.sum() == 0:
                continue
            print(f"  {col:<28}" + "".join(
                f"{row.get(e, 0):>15.2f}%" for e in sorted(m.engine.dropna().unique())))
    return regressions


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Bundesrat corpus quality audit")
    ap.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    ap.add_argument("--output-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any regression check is non-zero")
    args = ap.parse_args()

    ds = Path(args.dataset_dir)
    sp_path = ds / "speeches.parquet"
    if not sp_path.exists():
        print(f"ERROR: {sp_path} not found — run br_03 first.", file=sys.stderr)
        return 1
    print(f"Loading {sp_path} …", flush=True)
    # contribution_type is REQUIRED here: the null_name check is scoped by it,
    # and its absence makes that guard fall through to True, flagging all 4,550
    # document rows against a zero baseline.
    cols = ["speech_id", "session_id", "sequence", "year", "name",
            "speaker_raw", "role", "land", "text", "contribution_type"]
    df = pd.read_parquet(sp_path, columns=cols)
    sessions = (pd.read_parquet(ds / "sessions.parquet")
                if (ds / "sessions.parquet").exists() else None)
    print(f"  {len(df):,} speeches")

    df = run_audit(df)
    regressions = print_report(df, sessions)

    flag_cols = [c for c, _g, _l in CHECKS]
    flagged = df[df[flag_cols].any(axis=1)].copy()
    flagged["text_preview"] = flagged["text"].map(
        lambda t: str(t or "").strip()[:300].replace("\n", "↵"))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "br_05_quality_audit.csv"
    flagged[["speech_id", "session_id", "year", "name", "role", "land",
             "text_preview"] + flag_cols].to_csv(out, index=False, encoding="utf-8")
    print(f"\n{len(flagged):,} flagged rows -> {out}")

    if args.strict and regressions:
        print(f"\nFAIL: {regressions} check(s) above baseline.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
