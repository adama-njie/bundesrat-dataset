#!/usr/bin/env python3

from __future__ import annotations

import argparse
import bisect
import importlib.util
import re
import sys
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _PROJECT_ROOT / "bundesrat_dataset_v1"
_DEFAULT_PRE = _PROJECT_ROOT / "outputs_from_hpc"
_DEFAULT_OUT = _PROJECT_ROOT / "paper" / "tables" / "table_12_error_budget.csv"


def _load_br02():
    """Import br_02 so detector C runs the LIVE patterns, not a copy of them."""
    spec = importlib.util.spec_from_file_location(
        "br_02", _PROJECT_ROOT / "src" / "br_02_speaker_identifier.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BR02 = _load_br02()

# The chair's role always contains "präsident"; everyone else is a floor speaker.
_RE_CHAIR = re.compile(r"pr[äa]sident", re.IGNORECASE)

# A floor speaker opens by addressing the chair. Inside a speech, past the first
# few hundred characters, that vocative is somebody ELSE starting to talk.
_RE_VOCATIVE = re.compile(
    r"(?:Herr|Frau)\s+Pr[äa]sident(?:in)?\s*[!,]", re.IGNORECASE)
_VOCATIVE_MIN_OFFSET = 200

# The chair handing the floor over. If a speech ends with this and the NEXT
# speech is a different floor speaker, the chair's own turn was swallowed.
_RE_HANDOVER = re.compile(
    r"(?:[Dd]as\s+Wort\s+hat|[Ii]ch\s+erteile\s+(?:das\s+Wort|Herrn|Frau)"
    r"|erteile\s+ich\s+das\s+Wort|hat\s+das\s+Wort"
    r"|[Ii]ch\s+darf\s+.{0,40}\s+das\s+Wort)", re.DOTALL)
_HANDOVER_TAIL = 400


def _is_chair(role) -> bool:
    return bool(role) and bool(_RE_CHAIR.search(str(role)))


# ---------------------------------------------------------------------------
# Detector A — a floor turn swallowed into the speech before it
# ---------------------------------------------------------------------------

def detector_a(sp: pd.DataFrame) -> pd.DataFrame:
    """Same speaker on both sides of a boundary, with a late in-body vocative.

    In a chamber transcript the chair alternates with floor speakers, so two
    CONSECUTIVE speeches by the same speaker mean a turn was probably dropped
    between them. The vocative is what turns "probably" into evidence: the
    swallowed speaker's opening address is still sitting inside the first body.
    """
    hits = []
    for sid, g in sp.groupby("session_id", sort=False):
        g = g.sort_values("sequence")
        raw = g["speaker_raw"].fillna("").tolist()
        txt = g["text"].fillna("").tolist()
        ids = g["speech_id"].tolist()
        for i in range(len(g) - 1):
            if not raw[i] or raw[i] != raw[i + 1]:
                continue
            m = _RE_VOCATIVE.search(txt[i])
            if m and m.start() >= _VOCATIVE_MIN_OFFSET:
                hits.append({"detector": "A", "session_id": sid,
                             "speech_id": ids[i], "evidence": txt[i][
                                 max(0, m.start() - 60):m.start() + 40
                             ].replace("\n", " ")})
    return pd.DataFrame(hits)


# ---------------------------------------------------------------------------
# Detector B — a CHAIR turn swallowed
# ---------------------------------------------------------------------------

def detector_b(sp: pd.DataFrame) -> pd.DataFrame:
    """Floor -> different floor with no chair between, the first ending in
    hand-over language.

    This is the complement of A, and it exists because A is nearly blind here:
    a missing chair turn does not leave two same-speaker turns adjacent, since
    the speakers on either side of the chair are usually different people.
    """
    hits = []
    for sid, g in sp.groupby("session_id", sort=False):
        g = g.sort_values("sequence")
        rec = g.to_dict("records")
        for i in range(len(rec) - 1):
            a, b = rec[i], rec[i + 1]
            if _is_chair(a.get("role")) or _is_chair(b.get("role")):
                continue
            if not a.get("speaker_raw") or a["speaker_raw"] == b.get("speaker_raw"):
                continue
            tail = str(a.get("text") or "")[-_HANDOVER_TAIL:]
            m = _RE_HANDOVER.search(tail)
            if m:
                hits.append({"detector": "B", "session_id": sid,
                             "speech_id": a["speech_id"],
                             "evidence": tail[m.start():m.start() + 90
                                              ].replace("\n", " ")})
    return pd.DataFrame(hits)


# ---------------------------------------------------------------------------
# Detector C — an attribution-shaped line no pattern matched
# ---------------------------------------------------------------------------

def _candidate_re():
    """`<name> (<Land>)` at the START of a line, built from br_02's own pieces.

    ⚠️ "any line whose parenthesis is a Bundesland" is FAR too loose — it returns
    9,264 lines (19.6 % of the corpus), because it also matches every running
    column header and every prose sentence that mentions a Land in brackets. The
    candidate must have the SHAPE of an attribution: line-start junk, a parsable
    full name, then the qualifier. What it deliberately drops, relative to br_02,
    is the terminator — that is the whole point, since a missing colon is one of
    the ways an attribution goes unmatched.
    """
    return re.compile(
        rf"^{BR02._LINE_JUNK}{BR02._FULL_NAME}(?:{BR02._MEMBER_PAREN})")


def detector_c(preprocessed: Path, sessions: set[str] | None = None,
               speeches: pd.DataFrame | None = None) -> pd.DataFrame:
    """A line whose parenthesis validates as a real Bundesland, unmatched.

    Runs br_02's OWN patterns over the cleaned text and subtracts every matched
    SPAN (not start offset — attributions run over two or three lines, and
    keying on starts reports every continuation line as missed).

    ⚠️ Interjections must be excluded or half the output is noise. A line whose
    head starts with "(" or "[" is a Zwischenruf recorded inside another speech
    — "(Dr. Vogel [Rheinland-Pfalz]: ...)" — and correctly not a speech.
    """
    # ⚠️ An ALLOWLIST, not every RE_* in br_02. Sweeping up all of them silently
    # drove this detector to zero: RE_PAREN_QUALIFIER is `\(([^)]+)\)`, which
    # matches EVERY parenthesis in the text, so every candidate line fell inside
    # a "matched" span. The helpers that split an already-matched attribution
    # (RE_*_SPLIT, RE_*_PREFIX, RE_PAREN_QUALIFIER) are not attribution matchers
    # and must not be counted as coverage.
    _ATTRIBUTION_PATTERNS = ("RE_SPEAKER_LINE", "RE_INLINE_SPEAKER",
                             "RE_CAPS_BARE", "RE_CHAIR_FUZZY",
                             "RE_CHAIR_NO_COLON", "RE_MEMBER_NO_COLON")
    pats = [getattr(BR02, n) for n in _ATTRIBUTION_PATTERNS if hasattr(BR02, n)]
    if not pats:
        raise RuntimeError("no attribution patterns found in br_02 — renamed?")
    cand_re = _candidate_re()
    hits = []
    files = sorted(preprocessed.glob("*/Session_*_cleaned.txt"))
    for f in files:
        sid = f.stem.replace("_cleaned", "")
        if sessions and sid not in sessions:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        covered = []
        for p in pats:
            for m in p.finditer(text):
                covered.append((m.start(), m.end()))
        covered.sort()
        merged: list[list[int]] = []
        for s, e in covered:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        # Speech boundaries in this session, for the running-header test below.
        offs, names = [], []
        if speeches is not None:
            g = speeches[speeches.session_id == sid].sort_values("char_offset")
            offs = g["char_offset"].fillna(-1).astype(int).tolist()
            names = g["name"].fillna("").astype(str).tolist()

        pos = 0
        for line in text.splitlines(keepends=True):
            start, pos = pos, pos + len(line)
            stripped = line.strip()
            if len(stripped) < 8 or stripped[0] in "([{":
                continue
            m = cand_re.match(stripped)
            if not m:
                continue
            # ⚠️ A surviving TABLE-OF-CONTENTS entry has exactly the shape this
            # detector looks for — "Dr. Hilpert (Hessen).. ‚ 111A",
            # "Wolters (Bremen), Berichterstatter 111B,116C" — and the index is
            # still present wherever br_01's remove_toc anchors were OCR-damaged.
            # Without this guard the detector reports 2,431 hits, essentially all
            # of them index lines rather than missing speeches. br_02 applies the
            # same test to RE_MEMBER_NO_COLON for the same reason; reusing its
            # pattern keeps the two from drifting apart.
            if BR02._RE_TOC_TAIL.search(stripped):
                continue
            paren = BR02.RE_PAREN_QUALIFIER.search(m.group(0))
            if not paren or not BR02._normalise_land(paren.group(1)):
                continue
            if any(s < pos and e > start for s, e in merged):
                continue
            # ⚠️ A `Name (Land)` line with no colon is AMBIGUOUS: it is a running
            # column header if that person is already speaking, and a colon-less
            # attribution otherwise. Without this test the detector reports every
            # reprinted header as a missing speech.
            if offs:
                i = bisect.bisect_right(offs, start) - 1
                if 0 <= i < len(names) and names[i]:
                    head_key = BR02._surname_key(
                        re.split(r"[({\[]", stripped)[0].split()[-1]
                        if re.split(r"[({\[]", stripped)[0].split() else "")
                    cur_key = BR02._surname_key(names[i].split()[-1]
                                                if names[i].split() else "")
                    if head_key and head_key == cur_key:
                        continue
            hits.append({"detector": "C", "session_id": sid,
                         "speech_id": "", "evidence": stripped[:110]})
    return pd.DataFrame(hits)


# ---------------------------------------------------------------------------
# Sensitivity — how much does each detector actually see?
# ---------------------------------------------------------------------------

def sensitivity(sp: pd.DataFrame) -> dict:
    """Simulate deleting each interior turn; would detector A notice?

    Detector A fires when the neighbours of the deleted turn share a speaker.
    Running that over every real turn gives the detection rate directly, split
    by whether the deleted turn was the chair or a floor speaker — which is the
    correction the raw union needs.
    """
    det_chair = tot_chair = det_floor = tot_floor = 0
    for _, g in sp.groupby("session_id", sort=False):
        g = g.sort_values("sequence")
        raw = g["speaker_raw"].fillna("").tolist()
        role = g["role"].tolist()
        for i in range(1, len(raw) - 1):
            would_detect = bool(raw[i - 1]) and raw[i - 1] == raw[i + 1]
            if _is_chair(role[i]):
                tot_chair += 1
                det_chair += would_detect
            else:
                tot_floor += 1
                det_floor += would_detect
    tot = tot_chair + tot_floor
    return {
        "chair_rate": det_chair / tot_chair * 100 if tot_chair else 0.0,
        "floor_rate": det_floor / tot_floor * 100 if tot_floor else 0.0,
        "any_rate": (det_chair + det_floor) / tot * 100 if tot else 0.0,
        "n_chair": tot_chair, "n_floor": tot_floor,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA)
    ap.add_argument("--preprocessed-dir", type=Path, default=_DEFAULT_PRE)
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    ap.add_argument("--with-detector-c", action="store_true",
                    help="run detector C. OFF by default: it is not calibrated "
                         "(see the module docstring) and its count is dominated "
                         "by prose that lists people rather than missing speeches.")
    ap.add_argument("--detector", default=None, choices=list("ABC"),
                    help="print the hit list for one detector and stop")
    ap.add_argument("--list", type=int, default=0,
                    help="print this many example hits")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sp = pd.read_parquet(args.data_dir / "speeches.parquet")
    # ⚠️ Spoken turns only. Written statements are appended after the sitting
    # closes, so a run of them by one author looks to detector A exactly like a
    # swallowed turn, and detector B's hand-over language cannot apply to text
    # that was never spoken. The whole error budget is a claim about the SPOKEN
    # series, which is also what br_07's recall is measured against.
    if "contribution_type" in sp.columns:
        sp = sp[sp["contribution_type"] == "spoken"].copy()
    n_speeches = len(sp)
    print(f"Corpus: {n_speeches:,} speeches in "
          f"{sp.session_id.nunique():,} sessions\n")

    frames = {}
    print("detector A (swallowed floor turn) …", flush=True)
    frames["A"] = detector_a(sp)
    print("detector B (swallowed chair turn) …", flush=True)
    frames["B"] = detector_b(sp)
    if args.with_detector_c:
        print("detector C (unmatched attribution) …", flush=True)
        frames["C"] = detector_c(args.preprocessed_dir, speeches=sp)

    if args.detector:
        d = frames.get(args.detector)
        if d is None or d.empty:
            print("no hits"); return 0
        for _, r in d.head(args.list or 25).iterrows():
            print(f"  {r.session_id}  {r.evidence}")
        return 0

    print("\n=== detector counts ===")
    rows = []
    for k, d in frames.items():
        n = len(d)
        rows.append({"detector": k, "hits": n, "pct_of_corpus": round(n / n_speeches * 100, 2)})
        print(f"  {k}: {n:>5,}   {n / n_speeches * 100:.2f}% of the corpus")

    # Overlap — the union is only meaningful if the detectors are near-disjoint.
    print("\n=== overlap (by speech_id, where both detectors carry one) ===")
    for a in frames:
        for b in frames:
            if a < b:
                sa = set(frames[a].get("speech_id", pd.Series(dtype=str))) - {""}
                sbs = set(frames[b].get("speech_id", pd.Series(dtype=str))) - {""}
                print(f"  {a} ∩ {b}: {len(sa & sbs)}")

    union = sum(len(d) for d in frames.values())
    print(f"\nUnion (upper bound, detectors near-disjoint): {union:,} "
          f"= {union / n_speeches * 100:.2f}% of the corpus")

    s = sensitivity(sp)
    print("\n=== detector-A sensitivity, by simulation ===")
    print(f"  a FLOOR turn is detectable : {s['floor_rate']:.1f}%  (n={s['n_floor']:,})")
    print(f"  a CHAIR turn is detectable : {s['chair_rate']:.1f}%  (n={s['n_chair']:,})")
    print(f"  any turn                   : {s['any_rate']:.1f}%")

    # Correct A by its measured rate; allow B roughly half sensitivity (the
    # hand-over phrase must survive OCR *and* sit near the end of the speech).
    a_corr = len(frames["A"]) / (s["floor_rate"] / 100) if s["floor_rate"] else 0
    b_corr_lo, b_corr_hi = len(frames["B"]) * 1.5, len(frames["B"]) * 2.5
    c_n = len(frames.get("C", []))
    lo = a_corr + b_corr_lo + c_n
    hi = a_corr + b_corr_hi + c_n
    print("\n=== corrected estimate ===")
    print(f"  A corrected by its {s['floor_rate']:.1f}% rate : {a_corr:,.0f}")
    print(f"  B corrected (×1.5–2.5)                : {b_corr_lo:,.0f}–{b_corr_hi:,.0f}")
    print(f"  C (a direct count, no correction)     : {c_n:,}")
    print(f"  TOTAL  ≈ {lo:,.0f}–{hi:,.0f} speeches "
          f"= {lo / n_speeches * 100:.1f}–{hi / n_speeches * 100:.1f}%")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(rows)
    summary.loc[len(summary)] = {"detector": "union", "hits": union,
                                 "pct_of_corpus": round(union / n_speeches * 100, 2)}
    summary["floor_sensitivity_pct"] = round(s["floor_rate"], 1)
    summary["chair_sensitivity_pct"] = round(s["chair_rate"], 1)
    summary["corrected_low"] = int(lo)
    summary["corrected_high"] = int(hi)
    summary["corpus_speeches"] = n_speeches
    summary.to_csv(args.out, index=False, encoding="utf-8")
    hits = pd.concat([d for d in frames.values() if not d.empty], ignore_index=True)
    hits.to_csv(args.out.with_name(args.out.stem + "_hits.csv"),
                index=False, encoding="utf-8")
    print(f"\nwrote {args.out}")
    print(f"wrote {args.out.with_name(args.out.stem + '_hits.csv')}  ({len(hits):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
