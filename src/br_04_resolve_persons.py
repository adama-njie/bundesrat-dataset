#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "bundesrat_dataset_v1"
DEFAULT_DOCS = ROOT / "docs"

# A career gap wider than this means a different person (father/son, successor).
MAX_GAP = 6
# A single NAME STRING spanning longer than this is probably a mixed bucket:
# "Dr. Goppel" runs 1964-1991 and covers both Alfons and Thomas.
MAX_STRING_SPAN = 25
# A resolved PERSON spanning longer than this is worth a human glance. Real
# Bundesrat careers do reach 29 years (Johannes Rau, 1970-1999).
MAX_PERSON_SPAN = 30
# Maximum edit distance between two folded first names still called the same.
MAX_FIRST_EDITS = 2

_TITLE = re.compile(
    r"^(?:Dr\.|Prof\.|Dipl\.-[\wäöü]+\.?|Dr\.-Ing\.|Ing\.|D\.|h\.\s*c\.|"
    r"Frau|FYrau|Herrn?)\s*", re.I)
_ROLE = re.compile(
    r"^(?:Vize|Amtierender?|Amtierende|Alters|Stellv\.?)?\s*"
    r"(?:Pr[äa]sident(?:in)?|Ministerpr[äa]sident(?:in)?|Minister(?:in)?|"
    r"Senator(?:in)?|Staatsminister(?:in)?|B[üu]rgermeister(?:in)?|"
    r"Staatssekret[äa]r(?:in)?|Landesminister|Schriftf[üu]hrer(?:in)?|"
    r"Berichterstatter(?:in)?)\s+", re.I)


def read_reviewed_csv(path: Path) -> pd.DataFrame:
    """Read a CSV a human has round-tripped through Excel.

    ⚠️ German-locale Excel writes ';' as the list separator and prepends a UTF-8
    BOM. Read with the default comma parser, the whole line becomes a single
    column, `person_id` is not found, and every decision is silently discarded —
    no error, the run just reports nothing was decided. Sniff the separator.
    """
    return pd.read_csv(path, dtype=str, sep=None, engine="python",
                       encoding="utf-8-sig").fillna("")


def strip_affixes(name: str) -> str:
    """Remove academic titles and role prefixes, leaving the name proper."""
    prev = None
    while prev != name:
        prev = name
        name = _TITLE.sub("", name).strip()
        name = _ROLE.sub("", name).strip()
    return name


def fold(s: str) -> str:
    """Lowercase, fold umlauts, drop everything that is not a letter."""
    s = s.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


def edits(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, short-circuited above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def blocked_because(anchor, other) -> str | None:
    """Why `other` cannot be the same person as `anchor` — or None."""
    # Gender is read off the protocol's own forms of address ("Frau",
    # "Präsidentin"), computed per name string by br_03. It is an INDEPENDENT
    # signal: it caught three merges that land, firstname, era and office all
    # let through — "Frau Prof. Dr. Funke" absorbed into "Rainer Funke",
    # "Vizepräsident Peter" into "Dr. Simone Peter", and "Frau Schäfer" into
    # "Harald B. Schäfer".
    ag, og = getattr(anchor, "gender", None), getattr(other, "gender", None)
    if ag and og and isinstance(ag, str) and isinstance(og, str) and ag != og:
        return "gender"
    if (anchor.land and other.land) and anchor.land != other.land:
        return "land"
    if (anchor.fkey and other.fkey
            and edits(anchor.fkey, other.fkey) > MAX_FIRST_EDITS):
        return "firstname"
    lo, hi = (anchor, other) if anchor.last_year <= other.last_year else (other, anchor)
    if max(0, hi.first_year - lo.last_year) > MAX_GAP:
        return "era"
    return None


_CHAIR_ROLE = re.compile(r"pr[äa]sident", re.I)


def is_chair_role(role) -> bool:
    """Chair offices rotate, so they carry no identity information.

    Johannes Rau appears as "Präsident" and "Amtierender Präsident" in
    consecutive, non-overlapping periods; that is one man moving through the
    presidency, not two people. A SUBSTANTIVE office is different — see
    _role_conflict().
    """
    return bool(role) and bool(_CHAIR_ROLE.search(str(role)))


def _role_conflict(a, b) -> bool:
    """True when two strings hold different substantive offices, disjointly.

    ⚠️ This is the Strauß case, and nothing else in the rule set catches it.
    "Dr. Strauss" (Staatssekretär im Bundesministerium der Justiz, 1950-58) and
    "Dr. Strauß" (Bundesminister der Finanzen, 1960-69) are Walter Strauß and
    Franz Josef Strauß. Land is null for both (federal officials represent no
    Land), neither carries a first name, and the career gap is only two years —
    so land, firstname and era all pass and the two men merge. Their offices are
    the one thing that separates them.

    Requiring NO year overlap keeps this off ordinary career progression: a man
    who is Staatssekretär and Minister in overlapping years is one man.
    """
    if not (a.most_common_role and b.most_common_role):
        return False
    if is_chair_role(a.most_common_role) or is_chair_role(b.most_common_role):
        return False
    if not (a.last_year < b.first_year or b.last_year < a.first_year):
        return False                      # careers overlap -> same person
    return fold(str(a.most_common_role)) != fold(str(b.most_common_role))


def prepare(speakers: pd.DataFrame) -> pd.DataFrame:
    df = speakers.copy()
    df["land"] = df["most_common_land"]
    bare = df["name"].map(strip_affixes)
    toks = bare.str.split()
    df["skey"] = toks.map(lambda t: fold(t[-1]) if t else "")
    df["fkey"] = toks.map(lambda t: fold(" ".join(t[:-1])) if t and len(t) > 1 else None)
    df["fkey"] = df["fkey"].map(lambda x: x or None)
    df["span"] = df["last_year"] - df["first_year"]
    if "gender" not in df.columns:
        df["gender"] = None
    df["gender"] = df["gender"].map(lambda x: x if isinstance(x, str) and x else None)
    return df


def resolve_block(blk: pd.DataFrame) -> list[dict]:
    """Iterative anchoring within one surname block -> list of components."""
    remaining = list(blk.sort_values("n_speeches", ascending=False).index)
    comps: list[dict] = []
    while remaining:
        anchor = remaining.pop(0)
        members, rejects = [anchor], []
        for i in list(remaining):
            why = blocked_because(blk.loc[anchor], blk.loc[i])
            if why is None:
                members.append(i)
                remaining.remove(i)
            else:
                rejects.append((i, why))
        comps.append({"anchor": anchor, "members": members, "rejects": rejects})
    return comps


def _flags(blk: pd.DataFrame, comp: dict, comps: list[dict]) -> list[str]:
    """Reasons a component should not be auto-applied.

    ⚠️ Not every rejection is a doubt. Separating two people on `land` or
    `firstname` is strong evidence — "Dr. Albrecht" (Niedersachsen) and
    "Jan Philipp Albrecht" (Schleswig-Holstein) are plainly different men, and
    flagging that would bury the real cases. Two situations are NOT strong:

      * an `era` rejection, which is the father/son shape (Alfons and Thomas
        Goppel, both Bayern) but also the ordinary career interruption; and
      * a PARTIAL anchor (no first name) rejecting a FULL name on `land`, which
        is how a politician who changed Bundesland looks — "Dr. Vogel"
        (Rheinland-Pfalz) versus "Dr. Bernhard Vogel" (Thüringen) is one man.
    """
    d = blk.loc[comp["members"]]
    out = []
    anchor_is_partial = blk.loc[comp["anchor"], "fkey"] is None
    for i, why in comp["rejects"]:
        if why == "era":
            out.append("era-split")
        elif why == "land" and anchor_is_partial and blk.loc[i, "fkey"] is not None:
            out.append("possible-land-change")
    for a in range(len(comp["members"])):
        for b in range(a + 1, len(comp["members"])):
            if _role_conflict(blk.loc[comp["members"][a]],
                              blk.loc[comp["members"][b]]):
                out.append("role-change")
                break
        if "role-change" in out:
            break
    if int(d["span"].max()) > MAX_STRING_SPAN:
        out.append("string-span")
    if int(d["last_year"].max() - d["first_year"].min()) > MAX_PERSON_SPAN:
        out.append("person-span")
    # a partial (no first name) that could equally belong to another component
    for i in comp["members"]:
        if blk.loc[i, "fkey"] is not None:
            continue
        for other in comps:
            if other is comp:
                continue
            if blocked_because(blk.loc[other["anchor"]], blk.loc[i]) is None:
                out.append("ambiguous-partial")
                break
        if "ambiguous-partial" in out:
            break
    return out


def person_id_for(blk: pd.DataFrame, members: list, used: set[str]) -> str:
    d = blk.loc[members]
    anchor = d["n_speeches"].idxmax()
    base = f"{blk.loc[anchor, 'skey']}-{int(d['first_year'].min())}"
    pid, n = base, 2
    while pid in used:
        pid, n = f"{base}-{n}", n + 1
    used.add(pid)
    return pid


def canonical_name(blk: pd.DataFrame, members: list) -> str:
    """Prefer the fullest name, breaking ties by volume."""
    d = blk.loc[members].copy()
    d["ntok"] = d["name"].map(lambda n: len(strip_affixes(n).split()))
    d = d.sort_values(["ntok", "n_speeches"], ascending=[False, False])
    return str(d.iloc[0]["name"])


def resolve(speakers: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = prepare(speakers)
    used: set[str] = set()
    links, review = [], []

    for skey, blk in df.groupby("skey"):
        if not skey:
            continue
        comps = resolve_block(blk)
        for comp in comps:
            members = comp["members"]
            flags = _flags(blk, comp, comps)
            pid = person_id_for(blk, members, used)
            canon = canonical_name(blk, members)
            d = blk.loc[members]
            rec = dict(
                person_id=pid,
                canonical_name=canon,
                n_strings=len(members),
                n_speeches=int(d["n_speeches"].sum()),
                first_year=int(d["first_year"].min()),
                last_year=int(d["last_year"].max()),
                land=(d["land"].dropna().iloc[0] if d["land"].notna().any() else ""),
            )
            if flags and len(members) > 1:
                rec["flags"] = ";".join(sorted(set(flags)))
                rec["members"] = " | ".join(
                    f"{r['name']} (n={r.n_speeches}, {r.first_year}-{r.last_year},"
                    f" {r.land or '—'})" for _, r in
                    d.sort_values("n_speeches", ascending=False).iterrows())
                rec["rejected"] = " | ".join(
                    f"{blk.loc[i, 'name']}:{w}" for i, w in comp["rejects"])
                rec["decision"] = ""
                review.append(rec)
            else:
                for _, r in d.iterrows():
                    links.append(dict(name=r["name"], person_id=pid,
                                      canonical_name=canon))
    return pd.DataFrame(links), pd.DataFrame(review)


def apply_overrides(links: pd.DataFrame, docs: Path) -> pd.DataFrame:
    """Fold in reviewed decisions: person_review.csv then person_overrides.csv."""
    for fname, col in (("person_review.csv", "decision"),
                       ("person_overrides.csv", "person_id")):
        path = docs / fname
        if not path.exists():
            continue
        extra = read_reviewed_csv(path)
        if col not in extra.columns:
            continue
        rows = []
        for _, r in extra.iterrows():
            decided = r[col].strip()
            if not decided or decided.lower() in {"skip", "pending"}:
                continue
            if fname == "person_overrides.csv":
                rows.append(dict(name=r.get("name", "").strip(),
                                 person_id=decided,
                                 canonical_name=r.get("canonical_name", "").strip()))
            elif decided.lower() in {"ok", "accept", "yes"}:
                for m in str(r.get("members", "")).split("|"):
                    nm = m.split(" (n=")[0].strip()
                    if nm:
                        rows.append(dict(name=nm,
                                         person_id=r["person_id"],
                                         canonical_name=r["canonical_name"]))
        if rows:
            add = pd.DataFrame(rows)
            add = add[add["name"] != ""]
            links = pd.concat([links[~links["name"].isin(add["name"])], add],
                              ignore_index=True)
            print(f"  applied {len(add)} link(s) from {fname}")
    return links


def refresh_links(links: pd.DataFrame, speakers: pd.DataFrame) -> pd.DataFrame:
    """Drop dead name strings and recompute every display name from live data.

    ⚠️ Reviewed decisions are keyed by person_id and carry the `canonical_name`
    that was current when the review file was written. If a later name-variant
    round dissolves that string, applying the decision reinstates a name the
    corpus no longer contains — "vogel-1971" kept displaying "Dr. Bemhard Vogel"
    (OCR for Bernhard) long after the string had been merged away. The display
    name must therefore always be derived from the CURRENT speakers table, never
    carried over from the CSV.
    """
    known = set(speakers["name"])
    dead = ~links["name"].isin(known)
    if dead.any():
        print(f"  dropped {int(dead.sum())} link(s) to name strings that no "
              f"longer exist")
        links = links[~dead].copy()

    n_speeches = dict(zip(speakers["name"], speakers["n_speeches"]))
    ntok = {n: len(strip_affixes(n).split()) for n in known}
    fresh = {}
    for pid, grp in links.groupby("person_id"):
        best = max(grp["name"], key=lambda n: (ntok.get(n, 0),
                                               int(n_speeches.get(n, 0) or 0)))
        fresh[pid] = best
    changed = (links["person_id"].map(fresh) != links["canonical_name"]).sum()
    if changed:
        print(f"  refreshed {int(changed)} display name(s) from the current data")
    links["canonical_name"] = links["person_id"].map(fresh)
    return links


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Bundesrat person resolution")
    ap.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    ap.add_argument("--docs-dir", default=str(DEFAULT_DOCS))
    ap.add_argument("--report", action="store_true",
                    help="print a summary and write nothing")
    args = ap.parse_args()

    ds, docs = Path(args.dataset_dir), Path(args.docs_dir)
    sk_path = ds / "speakers.parquet"
    if not sk_path.exists():
        print(f"ERROR: {sk_path} not found — run br_03 first.", file=sys.stderr)
        return 1

    speakers = pd.read_parquet(sk_path)
    links, review = resolve(speakers)

    singles = len(speakers) - links["name"].nunique() - sum(
        len(str(r).split("|")) for r in review.get("members", []))
    print(f"Name strings              : {len(speakers):,}")
    print(f"Resolved into persons     : {links['person_id'].nunique():,}"
          f"  ({links['name'].nunique():,} strings)")
    print(f"Flagged for review        : {len(review):,} components")
    if len(review):
        print(f"  speeches involved       : {review['n_speeches'].sum():,}")
    merged = links.groupby("person_id").size()
    print(f"Persons built from >1 name: {(merged > 1).sum():,}")

    if args.report:
        if len(review):
            print("\nFlagged components (largest first):")
            for _, r in review.nlargest(min(15, len(review)), "n_speeches").iterrows():
                print(f"  [{r['flags']}] {r['canonical_name']} (n={r['n_speeches']})")
                print(f"      {r['members']}")
                if r["rejected"]:
                    print(f"      rejected: {r['rejected']}")
        return 0

    docs.mkdir(parents=True, exist_ok=True)
    links = apply_overrides(links, docs)
    links = refresh_links(links, speakers)
    links.sort_values(["person_id", "name"]).to_csv(
        docs / "person_links.csv", index=False, encoding="utf-8",
        quoting=csv.QUOTE_MINIMAL)
    print(f"\nwrote {docs / 'person_links.csv'}  ({len(links):,} rows)")

    if len(review):
        cols = ["person_id", "canonical_name", "flags", "n_speeches",
                "first_year", "last_year", "land", "members", "rejected",
                "decision"]
        rp = docs / "person_review.csv"
        if rp.exists():
            old = read_reviewed_csv(rp)
            keep = dict(zip(old.get("person_id", []), old.get("decision", [])))
            review["decision"] = review["person_id"].map(
                lambda p: keep.get(p, ""))
        review[cols].sort_values("n_speeches", ascending=False).to_csv(
            rp, index=False, encoding="utf-8")
        print(f"wrote {rp}  ({len(review):,} rows to review)")

    ov = docs / "person_overrides.csv"
    if not ov.exists():
        ov.write_text("name,person_id,canonical_name\n", encoding="utf-8")
        print(f"created empty {ov}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
