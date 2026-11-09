#!/usr/bin/env python3
"""Incremental corpus update — fetch new sittings and rebuild the working release.

    python src/br_update.py --dry-run     # report only
    python src/br_update.py               # fetch, process, rebuild, report

Brings the corpus up to date without reprocessing 1,000+ sessions. Writes to
`bundesrat_dataset_current/`, never to the published `bundesrat_dataset_v1/`.

⚠️ THE PUBLISHED RELEASE IS FROZEN. `bundesrat_dataset_v1/` is the deposit
behind DOI 10.5281/zenodo.22086439 and the Data Descriptor under review. This
driver refuses to write to any directory whose name ends in `_v1` unless
--allow-v1 is passed. Do not pass it to "just refresh" the published release:
the paper quotes its row counts.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import br_fetch                                                    # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_DEFAULT_PREPROCESSED = _ROOT / "outputs_from_hpc"
_DEFAULT_DATASET = _ROOT / "bundesrat_dataset_current"
_STATE = _ROOT / "outputs" / "_update_state.json"
_REPORTS = _ROOT / "outputs" / "update_reports"

# The three resume logs. ⚠️ br_00/01/02 skip a session if its output file exists
# OR its path appears in one of these logs, so invalidating a revised sitting
# means clearing BOTH. Delete only the files and the stage still skips; delete
# only the log lines and the stage still sees the stale output.
_LOGS = ("processed.log", "cleaning.log", "speakers.log")


def _run(args: list[str], *, label: str, dry: bool) -> None:
    printable = " ".join(a if " " not in a else f'"{a}"' for a in args)
    print(f"\n--- {label}\n    {printable}")
    if dry:
        print("    (dry run, not executed)")
        return
    r = subprocess.run([sys.executable, *args], cwd=_ROOT)
    if r.returncode != 0:
        sys.exit(f"FATAL: {label} failed with exit code {r.returncode}")


def invalidate(sessions: list[int], preprocessed: Path, *, dry: bool) -> int:
    """Delete derived files and matching log lines for revised sittings."""
    if not sessions:
        return 0
    stems = {f"Session_{s:04d}" for s in sessions}
    removed = 0
    for stem in stems:
        for path in preprocessed.rglob(f"{stem}*"):
            if path.is_file():
                print(f"    rm {path.relative_to(preprocessed)}")
                removed += 1
                if not dry:
                    path.unlink()
    for name in _LOGS:
        log = preprocessed / name
        if not log.exists():
            continue
        lines = log.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if not any(s in ln for s in stems)]
        if len(kept) != len(lines):
            print(f"    {name}: dropped {len(lines) - len(kept)} line(s)")
            if not dry:
                log.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return removed


def snapshot(dataset: Path) -> dict:
    """Facts the change report diffs. Keep it cheap and comparable."""
    import pandas as pd
    sp = pd.read_parquet(dataset / "speeches.parquet")
    se = pd.read_parquet(dataset / "sessions.parquet")
    sk = pd.read_parquet(dataset / "speakers.parquet")
    spoken = sp[sp["contribution_type"] == "spoken"]
    return {
        "generated": date.today().isoformat(),
        "sessions": int(len(se)),
        "last_session": int(se["session"].max()),
        "last_date": str(se.loc[se["session"].idxmax(), "date_iso"]),
        "contributions": int(len(sp)),
        "spoken": int(len(spoken)),
        "written": int((sp["contribution_type"] == "written").sum()),
        "name_strings": int(len(sk)),
        "persons": int(sk["person_id"].nunique()),
        "unresolved_name_strings": int(sk["person_id"].isna().sum()),
        # Sorted lists, so the diff is a set difference and order never matters.
        "lands": sorted(sp["land"].dropna().unique().tolist()),
        "roles": sorted(sp["role"].dropna().unique().tolist()),
        # ⚠️ Per-session counts, so the next report can say whether a REVISED
        # sitting actually changed anything. The archive re-issues PDFs with new
        # bytes and identical text, so "revised" on its own alarms the reader
        # without informing them.
        "per_session": {str(int(r.session)): [int(r.n_speeches),
                                              int(r.n_written_statements)]
                        for r in se.itertuples()},
    }


def change_report(old: dict | None, new: dict, fetched: br_fetch.FetchReport,
                  dataset: Path) -> str:
    L = [f"# Corpus update — {new['generated']}", ""]
    if fetched.new_sessions:
        L.append(f"**New sittings:** {', '.join(map(str, sorted(fetched.new_sessions)))}")
    if fetched.revised_sessions:
        rev = sorted(fetched.revised_sessions)
        L.append(f"**Re-fetched sittings:** {', '.join(map(str, rev))}")
        # Distinguish "the PDF bytes changed" from "the data changed". The
        # archive re-issues documents with new bytes and identical text; saying
        # only "revised" makes a routine re-issue look like a correction.
        if old and old.get("per_session"):
            moved = [s for s in rev
                     if old["per_session"].get(str(s)) != new["per_session"].get(str(s))]
            if moved:
                L.append(f"  ⚠️ of these, **{len(moved)} changed the extracted "
                         f"data**: {', '.join(map(str, moved))}")
            else:
                L.append(f"  All {len(rev)} reprocessed to identical counts: "
                         f"the re-issues carry the same text.")
    L += ["", f"Release: `{dataset.name}`", "", "| | before | after | change |",
          "|---|---:|---:|---:|"]
    for key, label in (("sessions", "Sessions"),
                       ("contributions", "Contributions"),
                       ("spoken", "— spoken"), ("written", "— written"),
                       ("name_strings", "Name strings"), ("persons", "Persons"),
                       ("unresolved_name_strings", "Unresolved name strings")):
        b = old.get(key) if old else None
        a = new[key]
        d = "" if b is None else f"{a - b:+,}"
        L.append(f"| {label} | {'' if b is None else f'{b:,}'} | {a:,} | {d} |")
    L += ["", f"Corpus now runs to session {new['last_session']} "
              f"({new['last_date']})."]

    # ⚠️ A NEW land or role string is a QUESTION, not a fact. `land` is validated
    # against a closed set of 19, so a new value means the validator changed or
    # something slipped through; a new role string is usually an OCR variant that
    # needs folding in _normalise_official_role().
    if old:
        for key, label in (("lands", "Bundesland values"), ("roles", "role strings")):
            added = sorted(set(new[key]) - set(old[key]))
            gone = sorted(set(old[key]) - set(new[key]))
            if added:
                L += ["", f"⚠️ **New {label} ({len(added)}) — review before release:**"]
                L += [f"- `{v}`" for v in added[:25]]
                if len(added) > 25:
                    L.append(f"- …and {len(added) - 25} more")
            if gone:
                L += ["", f"**{label} no longer present ({len(gone)}):** "
                          + ", ".join(f"`{v}`" for v in gone[:10])]

    review = _ROOT / "docs" / "person_review.csv"
    if review.exists():
        import pandas as pd
        rv = pd.read_csv(review)
        undecided = int(rv["decision"].isna().sum()) if "decision" in rv else len(rv)
        L += ["", f"**Person review:** {len(rv)} components, {undecided} undecided "
                  f"(`docs/person_review.csv`)."]
    if fetched.errors:
        L += ["", "**Fetch errors:**"] + [f"- {e}" for e in fetched.errors]
    L += ["", "---", "",
          "Review this report before announcing a release. New Bundesland or "
          "role values, and any jump in unresolved name strings, are the two "
          "signals that an update needs a human before it ships."]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Incremental Bundesrat corpus update.")
    ap.add_argument("--pdf-dir", default=br_fetch._DEFAULT_PDF_DIR, metavar="PATH")
    ap.add_argument("--preprocessed-dir", default=str(_DEFAULT_PREPROCESSED),
                    metavar="PATH")
    ap.add_argument("--dataset-dir", default=str(_DEFAULT_DATASET), metavar="PATH")
    ap.add_argument("--version", default="v1.1-current",
                    help="version string stamped into the release metadata")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="process sessions already on disk; fetch nothing")
    ap.add_argument("--session", default=None, metavar="LIST",
                    help="force a specific comma-separated session list")
    ap.add_argument("--revision-window", type=int, default=12)
    ap.add_argument("--all-periods", action="store_true")
    ap.add_argument("--allow-v1", action="store_true",
                    help="permit writing into the frozen published release")
    args = ap.parse_args()

    pre = Path(args.preprocessed_dir)
    dataset = Path(args.dataset_dir)
    dry = args.dry_run

    if dataset.name.endswith("_v1") and not args.allow_v1:
        sys.exit(f"REFUSING to write to {dataset.name}: that is the published "
                 f"release behind the Zenodo DOI and the paper under review. "
                 f"Use --dataset-dir {_DEFAULT_DATASET.name}, or --allow-v1 if "
                 f"you really mean it.")

    print(f"Bundesrat incremental update — {date.today().isoformat()}")
    print(f"  PDFs         {args.pdf_dir}")
    print(f"  preprocessed {pre}")
    print(f"  release      {dataset}")
    if dry:
        print("  DRY RUN — no files written, no stages executed")

    # 1. fetch ---------------------------------------------------------------
    if args.session:
        targets = sorted(int(s) for s in args.session.split(","))
        rep = br_fetch.FetchReport(new_sessions=targets)
        print(f"\n--- 1. fetch (skipped; --session {args.session})")
    elif args.skip_fetch:
        rep = br_fetch.FetchReport()
        print("\n--- 1. fetch (skipped; --skip-fetch)")
    else:
        print("\n--- 1. fetch")
        rep = br_fetch.fetch(Path(args.pdf_dir),
                             _ROOT / "outputs" / "fetch_manifest.json",
                             all_periods=args.all_periods,
                             revision_window=args.revision_window, dry_run=dry)
        print(f"    {rep.summary()}")
        targets = rep.changed

    targets = rep.changed
    if not targets:
        print("\nNothing new or revised. Corpus is up to date.")
        return 0
    scope = ",".join(str(s) for s in targets)
    print(f"\n{len(targets)} session(s) to process: {scope}")

    # 2. invalidate revised --------------------------------------------------
    print("\n--- 2. invalidate revised sessions")
    if rep.revised_sessions:
        invalidate(rep.revised_sessions, pre, dry=dry)
    else:
        print("    none revised")

    # 3-5. text stages, scoped ----------------------------------------------
    _run([str(_SRC / "br_00_text_extractor.py"), "--session", scope,
          "--input-dir", args.pdf_dir, "--output-dir", str(pre)],
         label="3. extract (br_00)", dry=dry)
    _run([str(_SRC / "br_01_text_cleaning.py"), "--session", scope,
          "--preprocessed-dir", str(pre)],
         label="4. clean (br_01)", dry=dry)
    # ⚠️ br_02 is scoped but still pre-scans every cleaned file to build its
    # confirmed-surname set, so --session gives byte-identical output to a full
    # run. Do not "optimise" that pre-pass away.
    _run([str(_SRC / "br_02_speaker_identifier.py"), "--session", scope,
          "--preprocessed-dir", str(pre)],
         label="5. attribute speakers (br_02)", dry=dry)

    # 6-8. build, resolve persons, rebuild ----------------------------------
    #
    # ⚠️ br_03 runs TWICE and that is not redundant. br_04 resolves persons from
    # the speakers table that br_03 produces, then br_03 joins person_id back in.
    # A single pass ships a release whose person layer predates the new sessions.
    build = [str(_SRC / "br_03_build_dataset.py"), "--input-dir", str(pre),
             "--output-dir", str(dataset), "--version", args.version]
    _run(build, label="6. build release (br_03, pass 1)", dry=dry)
    _run([str(_SRC / "br_04_resolve_persons.py"), "--dataset-dir", str(dataset)],
         label="7. resolve persons (br_04)", dry=dry)
    _run(build, label="8. rebuild with person_id (br_03, pass 2)", dry=dry)

    # 9. validation ----------------------------------------------------------
    _run([str(_SRC / "br_05_quality_audit.py"), "--dataset-dir", str(dataset),
          "--strict"], label="9. quality audit (br_05 --strict)", dry=dry)

    # 10. change report ------------------------------------------------------
    print("\n--- 10. change report")
    if dry:
        print("    (dry run)")
        return 0
    old = json.loads(_STATE.read_text(encoding="utf-8")) if _STATE.exists() else None
    new = snapshot(dataset)
    _REPORTS.mkdir(parents=True, exist_ok=True)
    out = _REPORTS / f"update_{new['generated']}.md"
    out.write_text(change_report(old, new, rep, dataset), encoding="utf-8")
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(new, indent=2), encoding="utf-8")
    print(f"    wrote {out.relative_to(_ROOT)}")
    print(f"\nCorpus now runs to session {new['last_session']} ({new['last_date']}): "
          f"{new['contributions']:,} contributions in {new['sessions']:,} sessions.")
    print("Review the change report before announcing a release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
