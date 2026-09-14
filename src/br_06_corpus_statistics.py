#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "bundesrat_dataset_v1"
DEFAULT_TABLES = ROOT / "paper" / "tables"
DEFAULT_FIGURES = ROOT / "paper" / "figures"

# Speaker-level recall against the printed table of contents, spoken speakers
# only. Asterisked page refs are Anlagen (given in writing) and are correctly
# absent from a transcript.
#
# ⚠️ Keys must match decade_label() exactly — the first bucket is "1949–59",
# not "1950s", and a mismatched key drops the value silently rather than erroring.
#
# ⚠️ This used to be a hard-coded dict, which meant the paper's headline
# validation figure was frozen at whatever the corpus looked like when it was
# last measured by hand — a reviewer running the shipped code could not
# reproduce it. Run `python src/br_07_toc_recall.py` to regenerate. The
# fallback below is the last hand-measured set, kept only so this module still
# imports before br_07 has ever been run; it is NOT authoritative.
_TOC_RECALL_CSV = DEFAULT_TABLES / "table_11_toc_recall.csv"
_TOC_RECALL_FALLBACK = {"1949–59": 92.2, "1960s": 85.2, "1970s": 78.4,
                        "1980s": 92.2, "1990s": 92.4, "2000s": 97.0,
                        "2010s": 98.5}


def _load_toc_recall() -> tuple[dict, float | None]:
    try:
        d = pd.read_csv(_TOC_RECALL_CSV)
        col = "decade" if "decade" in d.columns else d.columns[0]
        rec = {str(r[col]): float(r["recall_pct"])
               for _, r in d.iterrows()
               if str(r[col]) != "ALL" and pd.notna(r.get("recall_pct"))}
        allv = d.loc[d[col].astype(str) == "ALL", "recall_pct"]
        return rec, (float(allv.iloc[0]) if len(allv) and pd.notna(allv.iloc[0])
                     else None)
    except Exception:
        return dict(_TOC_RECALL_FALLBACK), 93.6


TOC_RECALL, TOC_RECALL_ALL = _load_toc_recall()

INK, GREY = "#16202b", "#6b7885"
OCR_C, PDF_C = "#c1666b", "#3d6f8e"
FEM_C, MASC_C = "#b35a5f", "#4b7f9e"

plt.rcParams.update({
    "font.size": 9, "axes.edgecolor": INK, "axes.labelcolor": INK,
    "text.color": INK, "xtick.color": INK, "ytick.color": INK,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 150, "pdf.fonttype": 42, "ps.fonttype": 42,
})

# --- speaker categories -----------------------------------------------------
#
# ⚠️ Order matters, and the trap is the same one br_02 hit: "Bundestagspräsident"
# and "Bundespräsident" both contain "präsident", so they must be tested BEFORE
# the Präsidium rule or they are silently folded into the chair of this chamber.
#
# Labels are English because the figures and tables are read by an
# English-language audience; the German office nouns they stand for are given in
# the paper's own description of the categories.
_CATEGORY_RULES: list[tuple[str, str]] = [
    ("Federal chancellor",     r"Bundeskanzler"),
    ("Federal president",      r"Bundespr[äa]sident"),
    ("Bundestag member",       r"Bundestag"),
    ("Presiding chair",        r"pr[äa]sident"),
    ("Federal minister",       r"Bundesminister"),
    ("State secretary",        r"Staatssekret|Staatssekr\."),
    ("Rapporteur",             r"Berichterstatter"),
    ("Clerk",                  r"Schriftf[üu]hrer"),
    ("Land government",        r"Staatsminister|Senator|Landesminister|B[üu]rgermeister|^Minister"),
]
_COMPILED = [(lbl, re.compile(pat, re.I)) for lbl, pat in _CATEGORY_RULES]

# A speech with NO role but a Bundesland is a state delegate speaking for their
# Land — the Bundesrat's structural equivalent of an ordinary MdB, and a third
# of the corpus. It has no Bundestag counterpart, so it needs its own label.
CAT_DELEGATE = "Land delegate"
CAT_OTHER = "Other"

ALL_CATEGORIES = [
    "Presiding chair", CAT_DELEGATE, "Rapporteur", "Federal minister",
    "State secretary", "Land government", "Federal chancellor",
    "Federal president", "Bundestag member", "Clerk", CAT_OTHER,
]

_CAT_COLORS = {
    "Presiding chair": "#3d6f8e", CAT_DELEGATE: "#c1666b",
    "Rapporteur": "#7a9e7e", "Federal minister": "#d99a4e",
    "State secretary": "#e3c26f", "Land government": "#8c7aa8",
    "Federal chancellor": "#16202b", "Federal president": "#5b8fa8",
    "Bundestag member": "#a8a8a8", "Clerk": "#cfcfcf", CAT_OTHER: "#e8e8e8",
}

HISTORIC_LAENDER = {"Baden", "Württemberg-Baden", "Württemberg-Hohenzollern"}


def classify(role, land) -> str:
    r = str(role or "").strip()
    if not r:
        return CAT_DELEGATE if (land and str(land).strip()) else CAT_OTHER
    for label, pat in _COMPILED:
        if pat.search(r):
            return label
    return CAT_OTHER


def decade_label(year: int) -> str:
    """1949 belongs with the 1950s — the chamber's first sitting, not a decade."""
    d = (int(year) // 10) * 10
    if d == 1940:
        return "1949–59"
    if d == 1950:
        return "1949–59"
    return f"{d}s"


DECADES = ["1949–59", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]


def load(dataset_dir: Path):
    sp = pd.read_parquet(dataset_dir / "speeches.parquet")
    # ⚠️ SPOKEN turns only. Every series in this module is historical, and
    # written statements exist only from the 1970s — including them would put a
    # step change into every per-decade figure that looks like history and is
    # the practice of submitting to the record beginning. The written total is
    # reported separately in the overview table.
    if "contribution_type" in sp.columns:
        sp = sp[sp["contribution_type"] == "spoken"].copy()
    se = pd.read_parquet(dataset_dir / "sessions.parquet")
    pe = (pd.read_parquet(dataset_dir / "persons.parquet")
          if (dataset_dir / "persons.parquet").exists() else None)
    sp["words"] = sp["text"].fillna("").str.split().str.len()
    sp["decade"] = sp["year"].map(decade_label)
    se["decade"] = se["year"].map(decade_label)
    sp["category"] = [classify(r, l) for r, l in zip(sp["role"], sp["land"])]
    sp = sp.merge(se[["session_id", "engine", "presiding_land"]],
                  on="session_id", how="left")
    if pe is not None:
        sp = sp.merge(pe[["person_id", "gender", "gender_source"]],
                      on="person_id", how="left")
    return sp, se, pe


# --- tables -----------------------------------------------------------------

def table_overview(sp, se, out: Path):
    rows = []
    for d in DECADES:
        s, x = se[se.decade == d], sp[sp.decade == d]
        if not len(s):
            continue
        rows.append(dict(
            decade=d, sessions=len(s), speeches=len(x),
            words=int(x.words.sum()),
            mean_words=round(x.words.mean(), 1) if len(x) else 0,
            median_words=int(x.words.median()) if len(x) else 0,
            speeches_per_session=round(len(x) / len(s), 1),
            unique_persons=x.person_id.nunique(),
            sessions_ocr=int((s.engine == "ocr-tesseract").sum()),
            sessions_textlayer=int((s.engine == "pdftotext").sum()),
        ))
    tot = dict(decade="ALL", sessions=len(se), speeches=len(sp),
               words=int(sp.words.sum()),
               mean_words=round(sp.words.mean(), 1),
               median_words=int(sp.words.median()),
               speeches_per_session=round(len(sp) / len(se), 1),
               unique_persons=sp.person_id.nunique(),
               sessions_ocr=int((se.engine == "ocr-tesseract").sum()),
               sessions_textlayer=int((se.engine == "pdftotext").sum()))
    df = pd.DataFrame(rows + [tot])
    df.to_csv(out / "table_01_corpus_overview.csv", index=False, encoding="utf-8")
    return df


def table_categories(sp, out: Path):
    pv = (sp.pivot_table(index="decade", columns="category", values="speech_id",
                         aggfunc="count", fill_value=0)
            .reindex(DECADES).reindex(columns=ALL_CATEGORIES, fill_value=0))
    pv.loc["ALL"] = pv.sum()
    pv.to_csv(out / "table_02_speaker_categories.csv", encoding="utf-8")
    return pv


def table_land(sp, out: Path):
    d = sp[sp.land.notna()]
    pv = (d.pivot_table(index="land", columns="decade", values="speech_id",
                        aggfunc="count", fill_value=0)
           .reindex(columns=[c for c in DECADES if c in d.decade.unique()],
                    fill_value=0))
    pv["total"] = pv.sum(axis=1)
    pv["historic"] = [ix in HISTORIC_LAENDER for ix in pv.index]
    pv = pv.sort_values("total", ascending=False)
    pv.to_csv(out / "table_03_land_speeches.csv", encoding="utf-8")
    return pv


def table_presidency(se, out: Path):
    """Presidency terms derived from the corpus — self-validating against the
    Königsteiner rotation (expect ~76 annual terms)."""
    s = se.sort_values("session")
    runs, cur = [], object()
    for _, r in s.iterrows():
        if r.presiding_land != cur:
            runs.append(dict(presiding_land=r.presiding_land,
                             first_session=int(r.session), last_session=int(r.session),
                             first_date=r.date_iso, last_date=r.date_iso, sessions=1))
            cur = r.presiding_land
        else:
            runs[-1].update(last_session=int(r.session), last_date=r.date_iso,
                            sessions=runs[-1]["sessions"] + 1)
    df = pd.DataFrame([r for r in runs if isinstance(r["presiding_land"], str)])
    df["days"] = (pd.to_datetime(df.last_date) - pd.to_datetime(df.first_date)).dt.days
    df.to_csv(out / "table_04_presidency_terms.csv", index=False, encoding="utf-8")
    return df


def table_gender(sp, pe, out: Path):
    rows = []
    for d in DECADES:
        x = sp[sp.decade == d]
        if not len(x):
            continue
        nf = int((x.gender == "female").sum())
        nm = int((x.gender == "male").sum())
        nu = len(x) - nf - nm
        rows.append(dict(decade=d, speeches=len(x), n_female=nf, n_male=nm,
                         n_unknown=nu,
                         pct_female=round(nf / len(x) * 100, 2),
                         pct_male=round(nm / len(x) * 100, 2),
                         pct_unknown=round(nu / len(x) * 100, 2)))
    nf = int((sp.gender == "female").sum()); nm = int((sp.gender == "male").sum())
    rows.append(dict(decade="ALL", speeches=len(sp), n_female=nf, n_male=nm,
                     n_unknown=len(sp) - nf - nm,
                     pct_female=round(nf / len(sp) * 100, 2),
                     pct_male=round(nm / len(sp) * 100, 2),
                     pct_unknown=round((len(sp) - nf - nm) / len(sp) * 100, 2)))
    df = pd.DataFrame(rows)
    df.to_csv(out / "table_05_gender_over_time.csv", index=False, encoding="utf-8")
    if pe is not None:
        g = (pe.groupby(["gender", "gender_source"], dropna=False)
               .size().rename("persons").reset_index())
        g.to_csv(out / "table_06_gender_evidence.csv", index=False, encoding="utf-8")
    return df


def table_completeness(sp, out: Path):
    rows = []
    for d in DECADES + ["ALL"]:
        x = sp if d == "ALL" else sp[sp.decade == d]
        if not len(x):
            continue
        rows.append(dict(
            decade=d, speeches=len(x),
            pct_role=round(x.role.notna().mean() * 100, 1),
            pct_land=round(x.land.notna().mean() * 100, 1),
            pct_land_or_presiding=round(
                (x.land.notna() | x.presiding_land.notna()).mean() * 100, 1),
            pct_person_id=round(x.person_id.notna().mean() * 100, 1),
            pct_gender=round(x.gender.notna().mean() * 100, 1),
            toc_recall=TOC_RECALL.get(d, ""),
        ))
    df = pd.DataFrame(rows)
    df.to_csv(out / "table_07_completeness.csv", index=False, encoding="utf-8")
    return df


def table_top_speakers(sp, pe, out: Path, n=40):
    if pe is None:
        return None
    df = pe.nlargest(n, "n_speeches")[
        ["person_id", "person_name", "n_speeches", "n_sessions", "n_name_strings",
         "most_common_role", "most_common_land", "gender", "first_year", "last_year"]]
    df.to_csv(out / "table_08_top_speakers.csv", index=False, encoding="utf-8")
    return df


# --- figures ----------------------------------------------------------------

def _save(fig, name, figdir: Path):
    for ext in ("png", "pdf"):
        fig.savefig(figdir / f"{name}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print(f"  figures/{name}.png / .pdf")


def fig_categories(sp, figdir: Path):
    pv = (sp.pivot_table(index="decade", columns="category", values="speech_id",
                         aggfunc="count", fill_value=0)
            .reindex(DECADES).fillna(0))
    pv = pv.reindex(columns=[c for c in ALL_CATEGORIES if c in pv.columns], fill_value=0)
    frac = pv.div(pv.sum(axis=1), axis=0)
    fig, ax = plt.subplots(figsize=(7.6, 4.0))
    bottom = np.zeros(len(frac))
    for c in frac.columns:
        ax.bar(frac.index, frac[c] * 100, bottom=bottom, label=c,
               color=_CAT_COLORS.get(c, "#cccccc"), width=0.72, linewidth=0)
        bottom += frac[c].values * 100
    ax.set_ylabel("Share of speeches (%)")
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, fontsize=7.5, ncol=2, loc="upper center",
              bbox_to_anchor=(0.5, -0.12))
    _save(fig, "fig5_speaker_categories", figdir)


def fig_gender(sp, figdir: Path):
    rows = []
    for d in DECADES:
        x = sp[sp.decade == d]
        if not len(x):
            continue
        n_f = int((x.gender == "female").sum())
        n_m = int((x.gender == "male").sum())
        # Unknown is the RESIDUAL, not a `== "unknown"` test: the field is null
        # where the protocol marks the speaker nowhere, so a string comparison
        # would silently return zero for every decade.
        rows.append((d, (x.gender == "female").mean() * 100,
                     n_f, n_m, len(x) - n_f - n_m))
    lab = [r[0] for r in rows]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.6, 3.4))
    a1.plot(lab, [r[1] for r in rows], "o-", color=FEM_C, lw=1.8, ms=4)
    a1.set_ylabel("Women's share of speeches (%)")
    a1.set_ylim(0, max(r[1] for r in rows) * 1.18)
    for x_, y_ in zip(lab, [r[1] for r in rows]):
        a1.annotate(f"{y_:.1f}", (x_, y_), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=7.5)
    w = 0.27
    idx = np.arange(len(rows))
    a2.bar(idx - w, [r[2] for r in rows], w, label="female", color=FEM_C, linewidth=0)
    a2.bar(idx, [r[3] for r in rows], w, label="male", color=MASC_C, linewidth=0)
    a2.bar(idx + w, [r[4] for r in rows], w, label="unknown", color=GREY, linewidth=0)
    a2.set_xticks(idx); a2.set_xticklabels(lab)
    a2.set_ylabel("Speeches")
    a2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    a2.legend(frameon=False, fontsize=8)
    for a in (a1, a2):
        a.tick_params(axis="x", labelrotation=45)
    fig.tight_layout()
    _save(fig, "fig6_gender", figdir)


def fig_completeness(comp, figdir: Path):
    d = comp[comp.decade != "ALL"]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    series = [("pct_person_id", "person_id", INK),
              ("pct_gender", "gender", FEM_C),
              ("pct_land_or_presiding", "Land (incl. presiding)", PDF_C),
              ("pct_role", "role", "#7a9e7e"),
              ("pct_land", "land (own)", GREY)]
    for col, lab, c in series:
        ax.plot(d.decade, d[col], "o-", color=c, lw=1.6, ms=3.5, label=lab)
    ax.plot(d.decade, pd.to_numeric(d.toc_recall, errors="coerce"),
            "s--", color=OCR_C, lw=1.4, ms=4, label="ToC speaker recall")
    ax.set_ylabel("% of speeches")
    ax.set_ylim(0, 105)
    ax.tick_params(axis="x", labelrotation=45)
    ax.legend(frameon=False, fontsize=7.5, ncol=2)
    _save(fig, "fig7_completeness", figdir)


# --- external validity: do known events appear at the right time? -----------
#
# This is not a substantive finding; it is a CHECK. If the dates, the text and
# the session ordering are correctly aligned, then vocabulary tied to datable
# events must peak in the decade the event happened. It tests the whole pipeline
# end to end in a way no internal consistency check can.
_EVENT_KEYWORDS: list[tuple[str, str]] = [
    # The label is English, the pattern is the German it actually searches for.
    ("Refugees",             r"Fl[üu]chtling"),
    ("Reunification",        r"Wiedervereinigung|Deutsche[nr]?\s+Einheit"),
    ("Monetary union, euro", r"W[äa]hrungsunion|Euro-Einf|Euroeinf"),
    ("Financial crisis",     r"Finanzkrise|Bankenkrise|Finanzmarktkrise"),
    ("Climate policy",       r"Klimawandel|Klimaschutz|Klimakrise"),
    ("COVID-19",             r"COVID|Pandemie|Coronavirus|SARS-CoV"),
]
_EVENT_COLORS = ["#8c7aa8", "#c1666b", "#d99a4e", "#7a9e7e", "#3d6f8e", "#16202b"]


def table_events(sp, out: Path):
    text = sp["text"].fillna("")
    rows = []
    for label, pat in _EVENT_KEYWORDS:
        hit = text.str.contains(pat, case=False, regex=True, na=False)
        by = sp[hit].groupby("decade").size()
        tot = sp.groupby("decade").size()
        pct = (by / tot * 100).reindex(DECADES)
        r = {"keyword_group": label, "pattern": pat,
             "peak_decade": pct.idxmax() if pct.notna().any() else ""}
        for d in DECADES:
            r[d] = round(float(pct.get(d, 0) or 0), 2)
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(out / "table_09_event_keywords.csv", index=False, encoding="utf-8")
    return df


def table_land_timeline(sp, out: Path):
    """When each Bundesland enters and leaves the chamber.

    The territorial analogue of a party-system table, and self-validating: the
    corpus alone reproduces the Südweststaat merger of 1952, the Saarland's
    accession in 1957, and the five new Länder seated after reunification.
    """
    d = sp[sp.land.notna()]
    g = (d.groupby("land")
           .agg(first_year=("year", "min"), last_year=("year", "max"),
                speeches=("speech_id", "count"),
                sessions=("session_id", "nunique"))
           .sort_values("first_year"))
    g["historic"] = [ix in HISTORIC_LAENDER for ix in g.index]
    g.to_csv(out / "table_10_land_timeline.csv", encoding="utf-8")
    return g


def fig_events(sp, figdir: Path):
    text = sp["text"].fillna("")
    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    for (label, pat), col in zip(_EVENT_KEYWORDS, _EVENT_COLORS):
        hit = text.str.contains(pat, case=False, regex=True, na=False)
        pct = (sp[hit].groupby("decade").size() / sp.groupby("decade").size()
               * 100).reindex(DECADES).fillna(0)
        ax.plot(DECADES, pct.values, "o-", color=col, lw=1.7, ms=4, label=label)
    ax.set_ylabel("Share of speeches mentioning the topic (%)")
    ax.tick_params(axis="x", labelrotation=45)
    ax.legend(frameon=False, fontsize=8, ncol=2)
    fig.text(0.005, -0.06,
             "An external-validity check, not a finding: vocabulary tied to "
             "datable events must peak in the decade the event occurred.",
             fontsize=7.5, color=GREY)
    _save(fig, "fig8_event_keywords", figdir)


def check_skeleton(sp, se, pe, skeleton: Path) -> int:
    """Verify the paper skeleton's headline table against the live dataset.

    The skeleton is hand-written prose, so its numbers are the one place in the
    project that cannot self-refresh — and they have silently drifted three
    times. This compares the few that matter and names any mismatch.
    """
    if not skeleton.exists():
        print(f"  (no skeleton at {skeleton})")
        return 0
    text = skeleton.read_text(encoding="utf-8")
    # ⚠️ Read the UNFILTERED table here. `sp` arrives spoken-only (see load()),
    # which is right for every figure in this module and wrong for the paper's
    # headline counts — those are corpus-wide. Checking the filtered frame
    # reported the skeleton as drifted when it was correct.
    # Spoken and written share ONE table again, separated by contribution_type,
    # so the corpus-wide count is simply the row count and the spoken series is
    # a filter on it.
    both = pd.read_parquet(DEFAULT_DATASET / "speeches.parquet")
    spoken = both[both["contribution_type"] == "spoken"]
    written = both[both["contribution_type"] == "written"]
    words = both["text"].fillna("").str.split().str.len()
    spoken_words = spoken["text"].fillna("").str.split().str.len()
    expect = {
        "Sessions": f"{len(se):,}",
        "**Contributions**": f"{len(both):,}",
        "— spoken in the chamber": f"{len(spoken):,}",
        "— written statements (named author)": f"{len(written):,}",
        "Words": f"{int(words.sum()):,}",
        "Unique speaker name strings": f"{both['name'].nunique():,}",
        "Resolved persons": f"{0 if pe is None else len(pe):,}",
        "Median spoken speech length": f"{int(spoken_words.median())} words",
    }
    bad = 0
    for label, want in expect.items():
        row = next((ln for ln in text.splitlines()
                    if ln.startswith(f"| {label} ")), None)
        if row is None:
            print(f"  ?  {label}: row not found in skeleton")
            continue
        if want in row:
            print(f"  ok {label}: {want}")
        else:
            bad += 1
            print(f"  !! {label}: skeleton says {row.strip()!r}, data says {want}")
    return bad


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Bundesrat corpus statistics")
    ap.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    ap.add_argument("--tables-dir", default=str(DEFAULT_TABLES))
    ap.add_argument("--figures-dir", default=str(DEFAULT_FIGURES))
    ap.add_argument("--check-skeleton", action="store_true",
                    help="verify paper/SKELETON.md headline numbers and exit")
    args = ap.parse_args()

    ds = Path(args.dataset_dir)
    tdir, fdir = Path(args.tables_dir), Path(args.figures_dir)
    tdir.mkdir(parents=True, exist_ok=True)
    fdir.mkdir(parents=True, exist_ok=True)

    print(f"Reading {ds} …")
    sp, se, pe = load(ds)
    if args.check_skeleton:
        print("\nChecking paper/SKELETON.md against the build:")
        bad = check_skeleton(sp, se, pe, ROOT / "paper" / "SKELETON.md")
        print(f"\n{bad} mismatch(es)." if bad else "\nSkeleton is in sync.")
        return 1 if bad else 0
    print(f"  {len(sp):,} speeches / {len(se):,} sessions / "
          f"{0 if pe is None else len(pe):,} persons\n")

    print("Tables:")
    ov = table_overview(sp, se, tdir); print("  table_01_corpus_overview.csv")
    table_categories(sp, tdir);        print("  table_02_speaker_categories.csv")
    table_land(sp, tdir);              print("  table_03_land_speeches.csv")
    pres = table_presidency(se, tdir); print("  table_04_presidency_terms.csv")
    table_gender(sp, pe, tdir);        print("  table_05_gender_over_time.csv")
    print("  table_06_gender_evidence.csv")
    comp = table_completeness(sp, tdir); print("  table_07_completeness.csv")
    table_top_speakers(sp, pe, tdir);  print("  table_08_top_speakers.csv")
    ev = table_events(sp, tdir);       print("  table_09_event_keywords.csv")
    table_land_timeline(sp, tdir);     print("  table_10_land_timeline.csv")

    print("\nFigures:")
    fig_categories(sp, fdir)
    fig_gender(sp, fdir)
    fig_completeness(comp, fdir)
    fig_events(sp, fdir)

    print("\nHeadline numbers for the paper:")
    print(ov[["decade", "sessions", "speeches", "median_words",
              "speeches_per_session"]].to_string(index=False))
    print("\nExternal-validity check — peak decade per event keyword:")
    print(ev[["keyword_group", "peak_decade"]].to_string(index=False))
    print(f"\npresidency terms detected: {len(pres)} "
          f"(median {int(pres.days.median())} days) — expect ~76 annual terms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
