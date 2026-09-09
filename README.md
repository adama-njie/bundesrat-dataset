# The Bundesrat Corpus — extraction and attribution pipeline

Code that turns the German **Bundesrat** (Federal Council) *Plenarprotokolle* —
plenary transcripts from session 1 in 1949 to the present — into a structured,
speaker-attributed speech corpus.

The Bundesrat is a permanent body with continuous session numbering, so there
are no *Wahlperioden* to slice on. Its members represent the 16 German
*Bundesländer*, not political parties, which is why every speech carries a
`land` field rather than a `party` field.

| | |
|---|---|
| Sessions | 1,067 |
| Speeches | 47,290 |
| Unique speaker name strings | 2,907 |
| Resolved persons | 2,473 |
| Years covered | 1949–2026 |

## The dataset lives on Zenodo, not here

This repository holds the **pipeline**. The packaged corpus it produces is
published as a citable dataset:

> **DOI: [10.5281/zenodo.22086439](https://doi.org/10.5281/zenodo.22086439)**

`speeches.csv` alone is 159 MB, past GitHub's 100 MB per-file limit, and a
versioned archive with a DOI is the right home for data anyway. Each table
ships in Parquet and CSV with identical contents: `speeches`, `sessions`,
`speakers`, `persons`, plus a Frictionless `datapackage.json` schema.

Download it into `bundesrat_dataset_v1/` at the repository root and the
analysis stages below will find it.

## Pipeline

Every stage runs standalone and is resume-safe: existing outputs are skipped
unless `--force` is passed.

| Stage | Script | Input | Output |
|---|---|---|---|
| 00 | `br_00_text_extractor.py` | session PDFs | `Session_{NNN}.txt` + `metadata_catalogue.csv` |
| 01 | `br_01_text_cleaning.py` | raw text | `Session_{NNN}_cleaned.txt` |
| 02 | `br_02_speaker_identifier.py` | cleaned text | `Session_{NNN}_speakers.csv` |
| 03 | `br_03_build_dataset.py` | all `*_speakers.csv` + metadata | `bundesrat_dataset_v1/*.{parquet,csv}` |
| 04 | `br_04_resolve_persons.py` | `speakers.parquet` | `docs/person_links.csv`, `docs/person_review.csv` |

Stages 00–02 are the expensive part and were run on RWTH Aachen's CLAIX-2023
cluster; stage 03 packages the result locally. Stages 04 onward run on a laptop
in minutes.

### Audit and analysis stages

| Script | What it does |
|---|---|
| `br_05_quality_audit.py` | Standing regression audit of the built dataset — Bundesland validity, role and date sanity, duplicate and orphan checks |
| `br_06_corpus_statistics.py` | Descriptive tables and figures: decade × Land × role × gender |
| `br_07_toc_recall.py` | Speaker-level recall against each session's printed table of contents |
| `br_08_error_budget.py` | Decomposes residual attribution error by detector |

### Diagnostics

| Script | What it does |
|---|---|
| `br_c02_find_missed_speakers.py` | Ranks attribution-like lines the parser did **not** match, so misses surface as a reviewable list |
| `br_c04_find_name_variants.py` | Finds OCR surname variants by co-occurrence and edit distance (`Dr. Ehard` / `Dr. Ehardt`) |

Both diagnostics import the live patterns from `br_02` rather than copying
them — a copy drifts away from the parser it is meant to be auditing, which has
been the recurring failure mode in this project. Several other scripts do the
same for `br_02`'s name and Land helpers.

## Two problems this pipeline exists to solve

**OCR.** The 1949–1999 sessions are scans, not born-digital PDFs; 747 of 1,067
sessions go through Tesseract, 320 through `pdftotext`. The `engine` field on
`sessions` records which, and quality-sensitive NLP work should filter on it.
OCR text is readable but carries the character errors inherent to scanning
75-year-old print, which is exactly what `br_c04` is for.

**Identity.** Pre-2000 transcripts attribute by surname only, so the same
person appears under many strings across 75 years. Stage 04 groups name strings
into persons using Bundesland, first name, career era and office, and only
where nothing is ambiguous — 99.8% of speeches carry a `person_id`. Cases
deliberately left unmerged include `Dr. Ehard` / `Dr. Erhard` (Hans Ehard of
Bayern versus Chancellor Ludwig Erhard), `Kaisen` / `Kaiser`, and the two
Goppels, Alfons and his son Thomas, both sitting for Bayern. `name` and
`speaker_raw` are left untouched; `person_id` is purely additive.

## Curation files

`docs/` holds the human-reviewed inputs to person resolution — they are part of
the method, not scratch output:

| File | Contents |
|---|---|
| `name_variants.csv` | OCR variant → canonical surname, with counts and Land |
| `person_overrides.csv` | Manual person assignments that override the automatic grouping |
| `person_links.csv` | Name string → `person_id` mapping produced by stage 04 |
| `person_review.csv` | Groupings flagged for human judgement, with the decision taken |
| `name_variant_candidates.csv` | Ranked variant candidates from `br_c04`, before review |

## Running it

```bash
pip install -r requirements.txt
```

Tesseract itself is not a pip package. The binary and the German language data
(`deu.traineddata`) must come from the system — an HPC module, a package
manager, or a conda environment — with `TESSDATA_PREFIX` pointing at the
`tessdata` directory.

A single session end to end:

```bash
python src/br_00_text_extractor.py --session 941
python src/br_01_text_cleaning.py --session 941
python src/br_02_speaker_identifier.py --session 941
```

Rebuild the packaged dataset from stage-02 output, then resolve persons:

```bash
python src/br_03_build_dataset.py
python src/br_04_resolve_persons.py
```

Audit and describe an existing dataset (needs `bundesrat_dataset_v1/` present):

```bash
python src/br_05_quality_audit.py
python src/br_06_corpus_statistics.py
```

Input and output directories default to local paths set at the top of each
stage-00–02 script; override them with `--input-dir` and `--output-dir`.
Derived outputs — audit reports, tables, figures — are written to `outputs/`
and `paper/` and are not tracked here, since they are reproducible from the
dataset.

## Citation

If you use the corpus or this pipeline, please cite the dataset:

> Njie, A., Laperne, L., Torkayesh, A., & Venghaus, S. (2026). *The Bundesrat
> Corpus: speaker and Bundesland-attributed proceedings of the German Federal
> Council, 1949–2026* (v1) [Data set]. Zenodo.
> https://doi.org/10.5281/zenodo.22086439

Machine-readable metadata is in `CITATION.cff` in the Zenodo archive.

## License

The dataset is released under **CC BY 4.0**. The underlying Plenarprotokolle
are official documents of the Bundesrat.

## Authors

Adama Njie, Leandro Laperne, Ali Torkayesh and Sandra Venghaus — Chair of
Economics of Sustainability, School of Business and Economics, RWTH Aachen
University.
