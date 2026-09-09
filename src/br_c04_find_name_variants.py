#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MAX_VARIANT_SPEECHES = 2
MIN_CANONICAL_SPEECHES = 10
MAX_EDITS = 2

# Letters that never occur in German orthography but are common OCR substitutions.
#
# No re.IGNORECASE here. "ı" is U+0131 LATIN SMALL LETTER DOTLESS I, whose
# uppercase is ASCII "I" — under IGNORECASE it therefore matches an ordinary "i"
# and every name containing one gets flagged ("Dick", "Dr. Fübinger"). Both cases
# are listed explicitly instead.
_ALIEN = re.compile(r"[çıîéèñåøæłğșţÇÎÉÈÑÅØÆŁĞȘŢ]")
# A capital where a name cannot have one: after a lowercase ("HeublI",
# "WickIimayr") or doubled at the head of a token ("DBufhues", "QGriesinger").
_MIDCAP = re.compile(r"[a-zäöüß][A-ZÄÖÜ]|[A-ZÄÖÜ]{2}[a-zäöüß]")


def fold(s: str) -> str:
    s = s.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


def edits(a: str, b: str, cap: int = MAX_EDITS) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def has_ocr_marks(name: str) -> bool:
    return bool(_ALIEN.search(name) or _MIDCAP.search(name))


def find_candidates(speakers: pd.DataFrame,
                    known_variants: set[str],
                    same_person: dict) -> pd.DataFrame:
    df = speakers.reset_index(drop=True).copy()
    df["key"] = df["name"].map(fold)
    rare = df[df["n_speeches"] <= MAX_VARIANT_SPEECHES].to_dict("records")
    freq = df[df["n_speeches"] >= MIN_CANONICAL_SPEECHES].to_dict("records")

    rows = []
    for r in rare:
        if not r["most_common_land"]:
            continue                       # no Land -> no co-occurrence evidence
        if r["name"] in known_variants:
            continue                       # already merged in name_variants.csv
        for f in freq:
            if r["name"] == f["name"]:
                continue
            if f["most_common_land"] != r["most_common_land"]:
                continue
            d = edits(r["key"], f["key"])
            if d == 0 or d > MAX_EDITS:
                continue
            if not (f["first_year"] <= r["first_year"]
                    and r["last_year"] <= f["last_year"]):
                continue
            # already one person via br_04 (e.g. a title-only difference)
            if same_person.get(r["name"]) and \
               same_person.get(r["name"]) == same_person.get(f["name"]):
                continue
            rows.append(dict(
                variant=r["name"], canonical=f["name"], edit_distance=d,
                variant_speeches=int(r["n_speeches"]),
                canonical_speeches=int(f["n_speeches"]),
                land=r["most_common_land"],
                variant_year=int(r["first_year"]),
                canonical_years=f"{int(f['first_year'])}-{int(f['last_year'])}",
                ocr_marks=has_ocr_marks(r["name"]),
                signal="co-occurrence",
                check_direction=False,
                tier="HIGH" if d == 1 else "REVIEW",
                decision="",
            ))
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # keep the single best canonical per variant: fewest edits, then most speeches
    out = (out.sort_values(["variant", "edit_distance", "canonical_speeches"],
                           ascending=[True, True, False])
              .drop_duplicates("variant", keep="first"))
    return out.sort_values(["tier", "canonical_speeches"], ascending=[True, False])


def ocr_fold(s: str) -> str:
    """Aggressive fold collapsing the substitutions the scanner actually makes.

    Deliberately heavier than `fold()`: `l`/`i` are freely interchanged in these
    scans, and `rn`/`in` collapse to `m` once the strokes merge. Two names that
    agree under this fold but differ in print are nearly always one name.
    """
    s = s.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"),
                 ("1", "i"), ("0", "o")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z]", "", s)
    for a, b in (("rn", "m"), ("in", "m"), ("ii", "u")):
        s = s.replace(a, b)
    return s.replace("l", "i")


def find_fold_collisions(speakers: pd.DataFrame,
                         known_variants: set[str],
                         same_person: dict) -> pd.DataFrame:
    """Second signal: two SPELLINGS that collapse to the same OCR-fold key.

    The co-occurrence detector needs the variant to be rare, to carry a Land, and
    to sit inside the canonical's career. Real damage often fails one of those —
    "Apei" has 17 speeches, "Woiters" carries no Land — yet folds exactly onto a
    commoner spelling. This catches those.

    A fold collision is evidence of one NAME, not proof of one PERSON. The
    fold is aggressive enough to marry two real people: "Dr. Vogei" (Berlin,
    Hans-Jochen Vogel) folds onto "Dr. Vogel" (Rheinland-Pfalz, Bernhard Vogel).
    A differing Bundesland therefore forces REVIEW, never HIGH.
    """
    df = speakers.reset_index(drop=True).copy()
    df["fkey"] = df["name"].map(ocr_fold)
    rows = []
    for key, blk in df.groupby("fkey"):
        if not key or len(blk) < 2:
            continue
        blk = blk.sort_values("n_speeches", ascending=False)
        canon = blk.iloc[0]
        for _, v in blk.iloc[1:].iterrows():
            if v["name"] in known_variants:
                continue
            # "ß" vs "ss" is ORTHOGRAPHY, not scanner damage, and the fold
            # cannot tell them apart. "Dr. Strauß" (Franz Josef) and
            # "Dr. Strauss" (Walter) are two men; merging them here would rewrite
            # the name field itself, which is worse than a wrong person link.
            if (v["name"].replace("ß", "ss") == canon["name"].replace("ß", "ss")
                    and v["name"] != canon["name"]):
                continue
            if same_person.get(v["name"]) and \
               same_person.get(v["name"]) == same_person.get(canon["name"]):
                continue
            land_ok = (not v["most_common_land"] or not canon["most_common_land"]
                       or v["most_common_land"] == canon["most_common_land"])
            era_ok = not (v["last_year"] < canon["first_year"] - 6
                          or canon["last_year"] < v["first_year"] - 6)
            rows.append(dict(
                variant=v["name"], canonical=canon["name"],
                edit_distance=edits(fold(v["name"]), fold(canon["name"]), cap=9),
                variant_speeches=int(v["n_speeches"]),
                canonical_speeches=int(canon["n_speeches"]),
                land=v["most_common_land"] or "",
                variant_year=int(v["first_year"]),
                canonical_years=f"{int(canon['first_year'])}-{int(canon['last_year'])}",
                ocr_marks=has_ocr_marks(v["name"]),
                signal="fold-collision",
                tier="HIGH" if (land_ok and era_ok) else "REVIEW",
                # Frequency chooses the canonical, and for these pairs it is
                # wrong about 40 % of the time: "Ziegler" (4) is correct and
                # "Ziegier" (4) is the scan. Where neither side is clearly
                # commoner, the DIRECTION has to be read, not computed.
                check_direction=(int(canon["n_speeches"])
                                 < 3 * max(int(v["n_speeches"]), 1)),
                decision="",
            ))
    return pd.DataFrame(rows)


def read_reviewed_csv(path: Path) -> pd.DataFrame:
    """Read a CSV a human has round-tripped through Excel.

    German-locale Excel writes ';' as the list separator and prepends a UTF-8
    BOM, so the file comes back in a different dialect than it went out in.
    Sniffing the separator and stripping the BOM means the review step does not
    depend on which Excel locale the reviewer happens to run.
    """
    return pd.read_csv(path, dtype=str, sep=None, engine="python",
                       encoding="utf-8-sig").fillna("")


_OK_WORDS = {"ok", "yes", "accept", "y", "j", "ja"}


def parse_decision(text: str, variant: str, canonical: str,
                   known_names: set[str]) -> tuple[str, str, str] | None:
    """Interpret one `decision` cell.

    Accepts four forms, because reviewers naturally annotate rather than type a
    bare token:
        ""                  -> reject
        "ok"                -> accept as proposed
        "ok (Some Name)"    -> accept, but that name is the canonical
        "Some Name"         -> same, without the ok

    Naming the row's own VARIANT flips the merge. That is not a corner case: this
    detector picks the more frequent spelling as canonical, and frequency is
    sometimes wrong — an OCR ligature failure can be commoner than the true form
    ("Wickimayr" 35 vs "Wickelmayr" 1, where "-imayr" is a mis-read "-elmayr").

    Returns (variant, canonical, note) or None to reject.
    """
    t = text.strip()
    if not t:
        return None
    low = t.lower()
    named = ""
    if low in _OK_WORDS:
        return (variant, canonical, "")
    m = re.match(r"^(?:ok|yes|accept|y|j|ja)\s*[\(\[]([^\)\]]+)[\)\]]\s*$", t, re.I)
    if m:
        named = m.group(1).strip()
    elif low.startswith(tuple(w + " " for w in _OK_WORDS)):
        named = t.split(None, 1)[1].strip()
    else:
        named = t                      # a bare name, e.g. "Hemfler"

    if not named:
        return None
    if named == canonical:
        return (variant, canonical, "")
    if named == variant:
        return (canonical, variant, "FLIPPED — the rarer spelling is canonical")
    if named not in known_names:
        return (variant, named, f"WARNING: '{named}' is not an existing speaker name")
    return (variant, named, f"canonical replaced with '{named}'")


def apply_decisions(cand_path: Path, docs: Path, dry_run: bool = False,
                    speakers: pd.DataFrame | None = None) -> int:
    """Append rows marked `ok` in the candidates CSV to docs/name_variants.csv.

    Done in code rather than by hand because both files are UTF-8 and full of
    umlauts: Excel will happily write them back as cp1252, and a mangled
    canonical name silently stops matching anything in br_02.
    """
    if not cand_path.exists():
        print(f"ERROR: {cand_path} not found — run without --apply first.",
              file=sys.stderr)
        return 1
    cand = read_reviewed_csv(cand_path)
    known = set(speakers["name"]) if speakers is not None else set()

    rows, notes = [], []
    for _, r in cand.iterrows():
        got = parse_decision(r["decision"], r["variant"], r["canonical"], known)
        if got is None:
            continue
        v, c, note = got
        rows.append(dict(variant=v, canonical=c,
                         variant_n=r["variant_speeches"],
                         canonical_n=r["canonical_speeches"],
                         land=r["land"]))
        if note:
            notes.append(f"    {r['decision']!r}: {v} -> {c}  [{note}]")
        # Renaming the canonical means the ORIGINAL canonical was the wrong
        # spelling too, so it has to move as well — otherwise one person ends up
        # split across two canonicals ("Hemifiler"->"Hemfler" while
        # "Hemifler"->"Hemiler" and "Hemiler" itself stays put).
        if c != r["canonical"] and r["canonical"] != v and r["canonical"] in known:
            rows.append(dict(variant=r["canonical"], canonical=c,
                             variant_n=r["canonical_speeches"],
                             canonical_n="", land=r["land"]))
            notes.append(f"    + also redirecting '{r['canonical']}' -> '{c}' "
                         f"(it was the old canonical)")
    if not rows:
        print("No rows marked ok — nothing to apply.")
        return 0
    if notes:
        print("  non-standard decisions interpreted as:")
        for n in notes:
            print(n)

    nv_path = docs / "name_variants.csv"
    nv = (read_reviewed_csv(nv_path)
          if nv_path.exists()
          else pd.DataFrame(columns=["variant", "canonical", "variant_n",
                                     "canonical_n", "land"]))
    new = pd.DataFrame(rows)
    dup = new["variant"].isin(set(nv["variant"]))
    if dup.any():
        print(f"  skipping {int(dup.sum())} variant(s) already in name_variants.csv")
    new = new[~dup]

    if new.empty:
        print("Nothing left to add after filtering.")
        return 0

    # Promoting a name to canonical that is currently a VARIANT would make a
    # cycle: "Dr. Wicklmayr" already mapped to "Dr. Wickimayr", so choosing it as
    # the canonical gives Wicklmayr -> Wickimayr -> Wicklmayr. The reviewer's
    # decision supersedes the older row, so drop it.
    promoted = set(new["canonical"])
    stale = nv["variant"].isin(promoted)
    if stale.any():
        for _, r in nv[stale].iterrows():
            print(f"  superseded: dropping old row '{r['variant']}' -> "
                  f"'{r['canonical']}' (now chosen as canonical)")
        nv = nv[~stale]

    out = pd.concat([nv, new], ignore_index=True)

    # Collapse merge CHAINS. br_02 does a single dict lookup, so a chain
    # ("Dr. WickIimayr" -> "Dr. Wickimayr" -> "Dr. Wickelmayr") would half-apply
    # and leave one person under two names. Follow every target to its fixed
    # point instead, refusing to move if a cycle appears.
    mapping = dict(zip(out["variant"], out["canonical"]))
    resolved, chains = {}, 0
    for v, c in mapping.items():
        seen, target = {v}, c
        while target in mapping and target not in seen:
            seen.add(target)
            target = mapping[target]
        if target in seen and target != c:
            print(f"  WARNING: merge cycle involving '{v}' — left as '{c}'")
            target = c
        if target != c:
            chains += 1
        resolved[v] = target
    if chains:
        print(f"  collapsed {chains} merge chain(s) to their final canonical")
    out["canonical"] = out["variant"].map(resolved)
    out = out[out["variant"] != out["canonical"]]
    out = out.drop_duplicates("variant", keep="last").sort_values("variant")
    print(f"  {len(nv)} existing + {len(new)} new = {len(out)} merges")
    if dry_run:
        print("  (dry run — not written)")
        before = dict(zip(nv["variant"], nv["canonical"]))
        for _, r in out.iterrows():
            v, c = r["variant"], r["canonical"]
            if v not in before:
                print(f"    {v} -> {c}")
            elif before[v] != c:
                print(f"    {v} -> {c}   (was -> {before[v]})")
        return 0
    out.to_csv(nv_path, index=False, encoding="utf-8")
    print(f"wrote {nv_path}")
    print("\nNow re-run:  br_02 --force  ->  br_03  (then br_04 -> br_03)")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Find OCR name variants by co-occurrence")
    ap.add_argument("--dataset-dir", default=str(ROOT / "bundesrat_dataset_v1"))
    ap.add_argument("--docs-dir", default=str(ROOT / "docs"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "br_c04_name_variant_candidates.csv"))
    ap.add_argument("--apply", action="store_true",
                    help="Append rows marked ok in the candidates CSV to "
                         "docs/name_variants.csv (instead of re-scanning).")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --apply, show what would be added and write nothing.")
    args = ap.parse_args()

    if args.apply:
        sk = pd.read_parquet(Path(args.dataset_dir) / "speakers.parquet")
        return apply_decisions(Path(args.out), Path(args.docs_dir),
                               args.dry_run, speakers=sk)

    ds, docs = Path(args.dataset_dir), Path(args.docs_dir)
    speakers = pd.read_parquet(ds / "speakers.parquet")

    known: set[str] = set()
    nv = docs / "name_variants.csv"
    if nv.exists():
        known = set(pd.read_csv(nv)["variant"])

    same_person: dict = {}
    pl = docs / "person_links.csv"
    if pl.exists():
        lk = pd.read_csv(pl)
        same_person = dict(zip(lk["name"], lk["person_id"]))

    cands = pd.concat([find_candidates(speakers, known, same_person),
                       find_fold_collisions(speakers, known, same_person)],
                      ignore_index=True)
    if len(cands):
        # a variant found by both signals keeps the stronger (HIGH) row
        cands = (cands.sort_values(["variant", "tier", "canonical_speeches"],
                                   ascending=[True, True, False])
                      .drop_duplicates("variant", keep="first")
                      .sort_values(["tier", "signal", "canonical_speeches"],
                                   ascending=[True, True, False]))
    print(f"speaker name strings          : {len(speakers):,}")
    print(f"already merged (name_variants): {len(known):,}")
    print(f"candidates found              : {len(cands):,}")
    if len(cands):
        for tier in ("HIGH", "REVIEW"):
            t = cands[cands["tier"] == tier]
            by = t["signal"].value_counts().to_dict()
            print(f"  {tier:6} {len(t):3}  ({t['variant_speeches'].sum()} speeches)  {by}")
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        cands.to_csv(out, index=False, encoding="utf-8")
        print(f"\nwrote {out}")
        print("\nAccept a row by putting 'ok' in `decision`, then append the "
              "variant/canonical pair to docs/name_variants.csv and re-run br_02.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
