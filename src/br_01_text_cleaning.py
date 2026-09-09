
# ============================================================
# USER SETTINGS
# ============================================================
_DEFAULT_PREPROCESSED = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\Bundesrat\preprocessed"
# ============================================================

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TXT_PAGE_SEP = "\n\n--- Page Break ---\n\n"

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# --- Step 0: column-order fix ---
_RE_COL_MARKER_LINE = re.compile(r"^[ \t]*\(([ABCD])\)[ \t]*$", re.MULTILINE)
_COL_ORDER: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3}

# --- Step 1: page-break marker ---
RE_PAGE_BREAK = re.compile(
    r"^-{3,}\s*Page Break\s*-{3,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 2: Bundesrat page headers ---
# Format: "Bundesrat – 941. Sitzung – 29. Januar 2016"
# The en-dash may be encoded as –, —, ·, or a plain hyphen-minus in some
# pdftotext outputs, so we accept any non-alphanumeric separator char.
# Optional leading page number (e.g. "2  Bundesrat – 941. Sitzung ...").
# Also catches the form "Bundesrat · 941. Sitzung · 29. Januar 2016" that
# pdftotext produces for some column-layout PDFs where the dash is in the
# right column and OCR merges it as a middle-dot.
# OCR speckle that sits between the page number and the header word: an
# underscore, quote, degree sign, pipe, stray single letter, etc.
# ("56 _ Bundesrat — …", "88 ” Bundesrat — …", "' 66 Bundesrat — …", "__Bundesrat").
# Junk flanking the page number in a running header. A one- OR two-letter token
# is allowed ("WB ı Bundesrat, — 12. Sitzung"); anything longer is a real word.
# A junk UNIT is a stray mark, a short number, or a one/two-letter token. Anything
# three letters or longer is a real word, and that is the only thing standing
# between this pattern and ordinary prose — so the unit count may be generous
# ("52 a _.._._Bundesrat", "32 3 I Bundesrat", "\\ | „ * Bundesrat") but the
# letter bound may not.
_HDR_JUNK = (
    r"(?:[ \t]*(?:[_.,;:|/°”“„‚\"'`´’‘*~^¬»«–—?!+\\()\[\]{}-]"
    r"|\b[a-zA-Zı]{1,2}\b|\b\d{1,4}[A-Za-z]?\b)){0,10}[ \t]*"
)

RE_HEADER_BUNDESRAT = re.compile(
    # The page number may be Arabic or a Roman numeral (front matter: "IV").
    # A speck can fuse to the page number itself ("14Q Bundesrat — 11. Sitzung").
    r"^[ \t]*" + _HDR_JUNK + r"(?:\d{1,5}[A-Za-z]?|[IVXLC]{1,6})?\*?" + _HDR_JUNK +
    r"(?:\([ABCD]\)[ \t]*)?"
    # The separator and the session number are damaged too — "Bundesrat — ”7.
    # Sitzung", "Bundesrat_—-10. Sitzung", "Bundesrat — 9 Sitzung" (no period),
    # "Bundesrat — 2, Sitzung" (comma), "Bundesrat — 5.. Sitzung" (doubled).
    # "Bundesrat," with a comma, and a stray glyph fused to the session number
    # ("9‘. Sitzung", "12Z, Sitzung", "14Q Bundesrat — 11. Sitzung").
    # A single capital can be fused to the word itself ("a EBundesrat — 23."),
    # and the header may be broken across the blank line between two columns
    # ("Bundesrat — 27.\n\nSitzung 7. Juli 1950").
    r"[A-ZÄÖÜ]?Bundesrat[,;]?[ \t]*[_–—\-·‚]{0,3}[ \t]*[^\w\n]{0,3}[ \t]*"
    r"\d{1,4}[A-Za-z]?[ \t]*[^\w\n]{0,2}[ \t]*[.,]{0,2}"
    r"[ \t]*(?:\n[ \t]*){0,2}Sitzung.*$",
    re.IGNORECASE | re.MULTILINE,
)

# A second, UNANCHORED pass keyed on the trailing DATE.
#
# The line-start pattern above cannot reach two shapes. (1) The header is fused
# into the middle of real text, because the page number sits on the same line as
# a hyphenated word: "…Vertrag von Maastricht wer-422 Bundesrat — 646. Sitzung —
# 25. September 1993" — deleting the whole line would take the speech with it, so
# only the header SPAN may be excised. (2) The junk before it is a long run of
# OCR garbage ("280 r rnrnrnrnrnrnunuv©ü©&Bundesrat", "|. rrirrnrnunur\"wunuwun—“©ü…").
#
# ⚠️ Loosening the prefix junk to admit those is exactly what would start eating
# prose, so the discriminator moves to the OTHER end instead: a running header
# always finishes with a date. "Der Bundesrat hat in seiner 9. Sitzung
# beschlossen…" cannot match, because nothing but marks may stand between
# "Sitzung" and the day number. That makes the prefix free.
#
# The leading mark class excludes hyphens so a broken word keeps its hyphen
# ("… oder Ersatz-Bundesrat — 651. Sitzung — 14. Januar 1993" → "… oder Ersatz-").
_HDR_SPECK = r"[^\w\n]{0,3}[ \t]*"
_HDR_WRAP = r"(?:[ \t]*\n){0,2}[ \t]*"
RE_HEADER_BUNDESRAT_DATED = re.compile(
    r"\d{0,5}[ \t]*[^\w\s\-–—]{0,2}[ \t]*"
    r"[A-ZÄÖÜ]?Bundesrat[,;]?[ \t]*" + _HDR_SPECK + _HDR_WRAP + _HDR_SPECK +
    r"\d{1,4}[A-Za-z]?[ \t]*" + _HDR_SPECK + _HDR_WRAP + _HDR_SPECK +
    r"Sitzung[ \t]*" + _HDR_SPECK + _HDR_WRAP + _HDR_SPECK +
    # the date: "18. April 1986", "26. und 27. Juli 1955", "8 Jult [988"
    r"\d{1,2}[ \t]*" + _HDR_SPECK +
    r"(?:und[ \t]+\d{1,2}[ \t]*" + _HDR_SPECK + r")?"
    r"[A-Za-zäöüÄÖÜ]{3,10}[ \t]*" + _HDR_SPECK +
    # the year's leading "1" is often lost to a speck ("8 Jult [988", "i964")
    r"[il\d]?\d{3}[ \t]*" + _HDR_SPECK + r"\*?\d{0,4}",
    re.IGNORECASE,
)

# "Plenarprotokoll N" cover-page header (first line of some PDFs)
RE_HEADER_PLENARPROTOKOLL = re.compile(
    r"^[ \t]*Plenarprotokoll[ \t]+\d{1,4}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# "BUNDESRAT" all-caps title line on cover page
RE_HEADER_BUNDESRAT_TITLE = re.compile(
    r"^[ \t]*BUNDESRAT[ \t]*$",
    re.MULTILINE,
)

# "Stenografischer Bericht N. Sitzung  City, Weekday, den DD. Month YYYY"
# Appears on the cover page, sometimes as a multi-line block.
RE_HEADER_STENO = re.compile(
    r"^[ \t]*Stenografischer[ \t]+Bericht[ \t]+\d{1,4}\.[ \t]*Sitzung.*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 3: running speaker headers ---
# A speaker name is printed at the top of each new page as a standalone line.
# Two forms occur:
#   (a) "Hannelore Kraft (Nordrhein-Westfalen)"  — name + (state), no colon
#   (b) "Präsident Stanislaw Tillich"            — role + name, no colon
# We strip a line only when it matches a name/role pattern AND has no colon,
# no following text on the same line, AND is not preceded by a line ending
# with a colon (which would make it the text of the previous speaker).
#
# Strategy: match lines that look like speaker headers but lack a terminal ":"
# and are surrounded by blank lines (i.e. truly standalone).
# Using a broad but safe pattern: Title-case words, optional noble particle,
# optional (Bundesland) suffix.
_BUNDESLAENDER_PATTERN = (
    r"Baden-Württemberg|Bayern|Berlin|Brandenburg|Bremen|Hamburg|Hessen"
    r"|Mecklenburg-Vorpommern|MecklenburgVorpommern"
    r"|Niedersachsen|Nordrhein-Westfalen|Rheinland-Pfalz|Saarland|Sachsen"
    r"|Sachsen-Anhalt|Schleswig-Holstein|Thüringen"
)

RE_RUNNING_SPEAKER_HEADER = re.compile(
    r"^[ \t]*"
    # Optional chair role prefix
    r"(?:(?:Vize)?(?:Bundesrats)?(?:Präsident(?:in)?|Alterspräsident(?:in)?)\s+)?"
    # Optional title
    r"(?:(?:Prof\.\s+)?(?:Dr\.\s+)*)?"
    # Name: one or more title-case words (including nobility particles)
    r"[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+"
    r"(?:\s+(?:von\s+der|van\s+der|von|van|de|zu|vom|zur)\s+[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+)?"
    r"(?:\s+[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+)*"
    # Mandatory (Bundesland) suffix — this is what distinguishes running headers
    # from ordinary lines with a name at the start.
    #
    # ⚠️ The CONTENT is left open and validated afterwards by _is_land_like(),
    # the same lesson br_02 learned with _MEMBER_PAREN: an exact alternation
    # fails on one mis-scanned character, and the header then survives into the
    # speech body. "Erwin Teufel (Baden-Württernberg)" (rn for m) was welded onto
    # the previous line's hyphen break, leaving "…Maastricht wer-Erwin Teufel
    # (Baden-Württernberg)" inside the text and "fen die Frage…" orphaned after.
    r"\s+\([ \t]*([^)\n]{3,40}?)[ \t]*\)"
    r"[ \t]*$",
    re.MULTILINE,
)

# The 16 current Länder plus the three that merged into Baden-Württemberg in
# 1952 — a running header can carry any of them.
_RUNNING_HEADER_LAENDER = (
    "Baden-Württemberg Bayern Berlin Brandenburg Bremen Hamburg Hessen "
    "Mecklenburg-Vorpommern Niedersachsen Nordrhein-Westfalen Rheinland-Pfalz "
    "Saarland Sachsen Sachsen-Anhalt Schleswig-Holstein Thüringen "
    "Baden Württemberg-Baden Württemberg-Hohenzollern"
).split()


def _land_fold(s: str) -> str:
    """Fold a parenthesised qualifier to a comparison key.

    A deliberately small subset of br_02's `_land_key` — enough to absorb the
    scanner failures that actually occur in a running header, and no more. This
    is NOT imported from br_02: br_01 runs first in the pipeline, and a 19-string
    vocabulary is small enough to collision-check by hand (asserted below).
    """
    s = s.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"),
                 ("1", "i"), ("0", "o")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z]", "", s)
    for a, b in (("rn", "m"), ("in", "m"), ("ii", "u")):
        s = s.replace(a, b)
    return s.replace("l", "i")


_LAND_FOLD_KEYS = {_land_fold(n) for n in _RUNNING_HEADER_LAENDER}
assert len(_LAND_FOLD_KEYS) == len(_RUNNING_HEADER_LAENDER), "Land keys collide"


def _is_land_like(s: str) -> bool:
    """True when a parenthesised qualifier resolves to one of the 19 Länder."""
    return _land_fold(s) in _LAND_FOLD_KEYS

# A simpler standalone chair-role header (no Bundesland suffix):
# "Präsident Stanislaw Tillich" on its own line between blank lines.
# We check that it is surrounded by blank lines in the stripping function.
# Form (b) of the running column header: the chair's role + name reprinted at
# the top of a column, e.g. "Praesident Stobbe", "Vizepraesident Spaeth".
#
# NOTE: this pattern is deliberately NARROW and three known gaps are left open.
# It requires TWO OR MORE name tokens, so surname-only chairs do not match, and
# it has no "Amtierender"/"Alters" qualifier and no Bundespraesident/
# Bundeskanzler. Widening it removes 3,214 lines and clears 1,315 header leaks
# -- but it also COSTS 13 speeches, because an isolated chair line with no colon
# is ambiguous: it is a running header only if the same person was already
# speaking, and otherwise it is a colon-less attribution that br_02's detector E
# is designed to recover.
#
# br_01 cannot tell the two apart, because it does not know who is speaking.
# The correct place for this fix is br_02's split_speeches(), where the current
# speaker IS known -- exactly as the colon-carrying "Name (Land):" form is
# already handled there. Until then a cosmetic header leak is the cheaper
# failure: a deleted speech cannot be recovered.
RE_RUNNING_CHAIR_HEADER = re.compile(
    r"^[ 	]*"
    r"(?:Vize)?(?:Bundesrats)?Praesident(?:in)?".replace("Praesident", "Präsident")
    + r"[ 	]+(?:(?:Prof\.\s+)?(?:Dr\.\s+)*)?[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+"
    r"(?:\s+[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+)+"
    r"[ 	]*$",
    re.MULTILINE,
)

# --- Step 4: CID artefacts ---
RE_CID = re.compile(r"\(cid:\d+\)")

# --- Step 5: proceedings anchor ---
# "Die Sitzung" is mangled in its own right — session 99 prints "Dle Sltzung"
# (i read as l twice). Both words get a tolerant stem; "Sitzung" may also be
# hyphen-broken across a line, since this runs before rejoin_hyphens.
_DIE      = r"D[il1íì]e"
_SITZUNG  = r"S[iíìl1][a-zäöüß]{0,2}t[-\s]*z[uüa]ng"

# ⚠️ The same formula announces a RESUMPTION after a break ("Die Sitzung wird um
# 14 Uhr fortgesetzt", "… wird um 9 Uhr wieder eröffnet"). Matching those made
# remove_toc cut at the resumption and discard 50–75 % of sessions 65 and 99, so
# the opening sense is required explicitly.
_NOT_RESUMED = r"(?![\s\S]{0,80}(?:fortgesetzt|wieder|erneut))"

RE_ANCHOR_STRONG = re.compile(
    r"(?:"
    # "Beginn: 9,32 Uhr" — the time separator is a comma as often as a colon or
    # period in the scans, and the line sometimes carries a trailing speck.
    r"^\s*Beginn\s*:\s*\d{1,2}[:.,]\d{2}\s*Uhr"
    rf"|{_DIE}\s+{_SITZUNG}\s+wird\s+{_NOT_RESUMED}(?:um\s+\d|eröffnet)"
    # Same opening formula without the leading "Die" — OCR frequently mangles that
    # word ("pi_e Sitzung wird um 11.16 Uhr … eröffnet", session 29), which left
    # the whole attendance roster in the text and parsed it as speeches.
    rf"|{_SITZUNG}\s+wird\s+{_NOT_RESUMED}um\s+\d{{1,2}}[.,:]\d{{2}}\s*Uhr"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# The weak anchor: a chair attribution opening a paragraph. It was written for
# the born-digital format only — case-sensitive, umlaut required, colon at end of
# line — so the pre-2000 scans never matched it. Session 5 opens
# "\n\nPRASIDENT ARNOLD: Meine sehr verehrten Damen und Herren!": ALL CAPS, no
# umlaut, and inline. With none of the three anchors matching, its ToC and
# attendance roster survived into the text and were parsed as speeches.
# Being loose is safe here because remove_toc caps how deep this anchor may cut.
RE_ANCHOR_SPEAKER = re.compile(
    r"(?<=\n\n)"
    r"(?:(?:Vize|Bundesrats|Alters)?Pr[aä]sident(?:in)?)"
    r"[ \t]+[A-ZÄÖÜ][^\n:]{1,60}:(?:[ \t]*$|[ \t]+\S)",
    re.IGNORECASE | re.MULTILINE,
)

# The unambiguous session-opening phrase, spoken by the chair in the first
# speech: "… eröffne die 321. Sitzung des Bundesrates". Unlike the "Beginn:"
# / standalone-chair anchors, this is unique to the real opening and never
# appears in the ToC/attendance preamble, so it reliably marks where the
# proceedings start even when the opening attribution is inline (session 321,
# where the strict anchors landed far too deep and discarded the opening).
# The article between "eröffne" and the session number is often OCR-mangled
# ("eröffne d:1e 29. Sitzung"), so any short token is accepted in its place.
# The whitespace runs must stay \s (not [^\n]) because the phrase frequently
# wraps across a line break: "Ich eröffne\ndie 321. Sitzung".
# "eröffne" is mangled too — session 99 prints "eroiine" (ff read as ii, ö as o).
# NB the whitespace runs must stay \s: the phrase routinely wraps a line
# ("Ich eröffne\ndie 321. Sitzung"). The session NUMBER is what keeps this
# specific, so the verb stem can afford to be loose.
_EROEFFNE = r"er[oö][fi]{1,3}[fn]?e"

RE_ANCHOR_OPENER = re.compile(
    # "…eröffne die 371, Sit-\nzung" — the number is followed by a comma as often
    # as a period, and "Sitzung" may be hyphen-broken (see _SITZUNG).
    rf"{_EROEFFNE}\s+(?:\S{{1,6}}\s+)?\d{{1,4}}\s*[.,]\s*(?:\(\d+\.\)\s*)?{_SITZUNG}",
    re.IGNORECASE,
)

# Cap on how much of a session remove_toc may discard — see the guard there.
_TOC_MAX_FRACTION = 0.35

# --- Step 6: standalone page numbers ---
# Bundesrat uses column letters A–D like the Bundestag; page numbers are
# typically 1–4 digits in early sessions and can appear with * suffix.
RE_PAGE_NUMBER = re.compile(
    r"^\s*(?:[IVXivx]{1,5}\s+)?\d{1,5}\*?(?:\s*[A-D])?(?:\s*,\s*\d{1,5}\*?(?:\s*[A-D])?)*\s*$",
    re.MULTILINE,
)

# --- Step 7: footnotes and colophon ---
# A footnote in these protocols is always marked with a star and a bracket:
#   "*) Anlage 10"   "**) Anlagen 14 und 15"
#
# ⚠️ This pattern used to also carry "\d{1,2}[.)]\s+" and "[a-z][.)]\s+", meaning
# ANY line opening with a 1–2 digit number or a single letter plus a dot. That is
# not a footnote marker in German prose — it is a numbered agenda item
# ("1. Wahl eines Geschäftsordnungsausschusses;"), a lettered sub-item
# ("c) den Absatz 2 zu streichen;"), the abbreviation "z. B.", or a date that
# happens to wrap to the start of a line ("Der Bundestag hat … am / 6. Mai 2004
# einstimmig gebilligt"). Measured: those two alternatives deleted 16,695 lines
# of real speech text corpus-wide, while the 5,288 genuine "*)" footnotes
# survived untouched, because "\*\s+" demands whitespace after the star and the
# real marker has a bracket there. The net effect was exactly inverted.
RE_FOOTNOTE_MARKER = re.compile(
    r"^\s*(?:"
    r"\*+\)?[ \t]*"
    r"|†[ \t]+"
    r")",
    re.MULTILINE,
)

# Bundesrat publishers (different from Bundestag):
#   "Gesamtherstellung: H. Heenemann GmbH & Co. ..."
#   "Vertrieb: Bundesanzeiger Verlag GmbH ..."
#   "ISSN 0720-7999" / "ISSN 0722-7999"
#   "Druck und Vertrieb: ..."
RE_COLOPHON = re.compile(
    r"^\s*(?:"
    r"Gesamtherstellung\s*:.*"
    r"|Satz\s*:\s*Satzweiss.*"
    r"|Druck\s*:\s*(?:H\.\s+Heenemann|Printsystem).*"
    r"|Vertrieb\s*:.*(?:Bundesanzeiger|betrifft-gesetze).*"
    r"|ISSN\s+[\d\-]+"
    r"|BUB\s+Gebr\."
    r"|Druck\s+und\s+Vertrieb.*"
    r"|H\.\s+Heenemann\s+GmbH.*"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 8: column/margin position markers ---
# The printed column markers are (A) (B) (C) (D), but OCR reads the brackets as
# braces or square brackets in almost any combination — "{B)", "(D}", "{A}",
# "(A]". Matching only the clean form left 21,237 markers in the cleaned text,
# and 280 of those sit at the head of a line directly in front of a speaker
# attribution ("{A] Vizepräsident Dr. GEBHARD MÜLLER: Weitere …"), where they
# stop br_02 seeing the attribution at all and the speech is swallowed.
# The single letter A–D plus the whitespace boundaries keep this off real prose.
_MARK = r"[({\[][ABCD][)}\]]"

# A marker that landed inside a word: lowercase on BOTH sides, with at most a
# space or one line break in between. Removing it (and the gap) restores the word.
RE_MARGIN_MARKER_INWORD = re.compile(
    rf"([a-zäöüß]){_MARK}[ \t]*\n?[ \t]*([a-zäöüß])"
)

# A marker sitting directly before sentence punctuation: drop the marker and the
# space in front of it, so the punctuation stays attached to the word.
RE_MARGIN_MARKER_PUNCT = re.compile(rf"[ \t]*{_MARK}(?=[.,;:!?])")

# A marker sitting at a LINE BOUNDARY — the column break itself. The lookbehind
# is restricted to a lowercase letter or a hyphen, which is what keeps the unit
# "67 dB(A)" (capital B) out. Unlike the in-word rule this only deletes the
# marker; the line break is left for rejoin_hyphens / normalise_whitespace to
# resolve, so "Unter-(C)\n\nsuchungsausschuß" can be put back together.
RE_MARGIN_MARKER_EOL = re.compile(rf"(?<=[a-zäöüß\-]){_MARK}[ \t]*(?=\n)")
RE_MARGIN_MARKER_BOL = re.compile(rf"(?<=\n)[ \t]*{_MARK}[ \t]*(?=[a-zäöüß])")

RE_MARGIN_MARKER = re.compile(
    # A stray mark is often fused to the marker — "{C)'", "[B]‘", "{C}-", "(C)." —
    # which used to defeat the whitespace boundary and leave the marker in place.
    # A mark fused after the marker is sometimes scanned as a letter ("{D)J").
    # "_" is a \w character, so the leading class needs the same widening as the
    # trailing one — otherwise "_{D]_" on its own line survived untouched.
    rf"(?:^|\s)(?:[^\w\s]|[JjlI_]){{0,2}}{_MARK}(?:[^\w\s]|[JjlI_]){{0,2}}(?:\s|$)"
    rf"|(?:{_MARK}){{2,}}",
    re.MULTILINE,
)

# --- Step 9: bare separator lines ---
RE_BARE_SEPARATOR = re.compile(
    r"^\s*[-–—]{1,2}\s*$",
    re.MULTILINE,
)

# --- Step 10: back-matter reference blocks ---
RE_BACKM_REF = re.compile(
    r"^\s*(?:"
    r"(?:Drucksache\s+[\d/,\s]+)"
    r"|(?:Ratsdokument\s+[\d/]+)"
    r"|(?:EuB-EP\s+[\d;/\s\w\-]+)"
    r"|(?:P\d+_TA[^\n]{0,60})"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

RE_BACKM_START = re.compile(
    r"^Amtliche\s+Mitteilungen\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 12: hyphenation ---
# End-of-line hyphenation. OCR often prints a spurious second dash after the
# real hyphen ("Bericht- -\n\nerstatter", ~800 occurrences corpus-wide), which
# previously blocked the rejoin and left broken words — including inside speaker
# attributions, where it hid the "Berichterstatter" role entirely.
RE_HYPHEN_BREAK = re.compile(r"(\w)[-\xad][ \t]*[-–—]?[ \t]*\n+(\w)", re.UNICODE)

# A continuation line that opens "Name …: text" is a speaker attribution, not the
# tail of a broken word — see rejoin_hyphens(). Deliberately loose: the few prose
# lines it also catches ("Ich glaube, es ist besser: wir bleiben …") merely keep a
# hyphenated word split, which is far cheaper than welding over an attribution.
# The second alternative covers the colon-LESS shape: a running column header
# ("Erwin Teufel (Baden-Württernberg)") that step 3 could not strip. Joining a
# hyphen break onto it buries both the header and the word's other half.
RE_ATTRIBUTION_AHEAD = re.compile(
    r"[A-ZÄÖÜ][^\n:]{0,70}:[ \t]"
    r"|[A-ZÄÖÜ][^\n:(]{0,60}\([^)\n]{3,40}\)[ \t]*$"
)

# --- Step 11: whitespace ---
RE_EXCESS_BLANK = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Individual cleaning steps
# ---------------------------------------------------------------------------

def _fix_page_column_order(page: str) -> str:
    """Reorder blocks within one extracted page to canonical A→B→C→D."""
    markers = [
        (m.group(1), m.start(), m.end())
        for m in _RE_COL_MARKER_LINE.finditer(page)
    ]
    if len(markers) < 2:
        return page

    letters = [letter for letter, _s, _e in markers]
    saw_right = False
    needs_reorder = False
    for letter in letters:
        if letter in ("C", "D"):
            saw_right = True
        elif letter in ("A", "B") and saw_right:
            needs_reorder = True
            break

    if not needs_reorder:
        return page

    pre_text = page[: markers[0][1]]
    blocks: list[tuple[str, str]] = []
    for i, (letter, start, _end) in enumerate(markers):
        block_end = markers[i + 1][1] if i + 1 < len(markers) else len(page)
        blocks.append((letter, page[start:block_end]))

    sorted_blocks = sorted(blocks, key=lambda x: _COL_ORDER.get(x[0], 99))
    return pre_text + "".join(content for _letter, content in sorted_blocks)


def fix_column_order(text: str) -> str:
    """Step 0 — fix two-column horizontal-band interleaving."""
    pages = text.split(_TXT_PAGE_SEP)
    return _TXT_PAGE_SEP.join(_fix_page_column_order(p) for p in pages)


def remove_page_breaks(text: str) -> str:
    """Step 1 — remove '--- Page Break ---' markers."""
    return RE_PAGE_BREAK.sub("", text)


def remove_headers(text: str) -> str:
    """Step 2 — remove Bundesrat page headers and cover-page titles."""
    text = RE_HEADER_BUNDESRAT.sub("", text)
    text = RE_HEADER_BUNDESRAT_DATED.sub("", text)
    text = RE_HEADER_PLENARPROTOKOLL.sub("", text)
    text = RE_HEADER_BUNDESRAT_TITLE.sub("", text)
    text = RE_HEADER_STENO.sub("", text)
    return text


# The sitting's formal close. Everything after it is appendix material.
_RE_SITTING_CLOSE = re.compile(r"\(\s*Schluss\s*(?:der\s+Sitzung)?\s*:", re.IGNORECASE)


def remove_running_speaker_headers(text: str) -> str:
    """
    Step 3 — remove running speaker headers (name printed at top of each page).

    A running header is a standalone line matching a speaker-name pattern
    (with a Bundesland suffix, or a chair-role prefix) that appears between
    blank lines.  Lines with a colon are speaker attribution lines and are
    always kept.
    """
    # ⚠️ STOP at the close of the sitting. Everything after "(Schluss: HH.MM
    # Uhr)" is the Anlagen — statements submitted in writing — and an appendix
    # has no columns, so there are no running headers there to remove. What it
    # DOES have is author lines of exactly the same shape:
    #
    #     Anlage 4
    #     Erklärung von Minister Becker (Saarland)      <- deleted as a header
    #     zu Punkt 1 a der Tagesordnung
    #
    # Stripping those destroyed the author of roughly 3,300 written statements
    # corpus-wide, which then reach the dataset as unattributed text. The damage
    # is invisible in the speech layer, because the appendix is not a speech.
    m_close = _RE_SITTING_CLOSE.search(text)
    head, tail = (text[:m_close.start()], text[m_close.start():]) if m_close \
        else (text, "")

    # Form (a): "Name (Bundesland)" — unambiguously a running header, but only
    # once the parenthesis actually resolves to a Land (the pattern itself lets
    # any content through so OCR damage does not defeat it).
    head = RE_RUNNING_SPEAKER_HEADER.sub(
        lambda m: "" if _is_land_like(m.group(1)) else m.group(0), head
    )
    text = head + tail

    # Form (b): "Präsident Name" standalone — only strip when surrounded by
    # blank lines to avoid accidentally removing the first word of a sentence.
    # We replace each match with an empty string only when preceded and followed
    # by a blank line.
    def _strip_if_isolated(m: re.Match) -> str:
        start, end = m.start(), m.end()
        before = text[max(0, start - 2):start]
        after  = text[end:min(len(text), end + 2)]
        if "\n" in before and ("\n" in after or end == len(text)):
            return ""
        return m.group(0)

    head2 = RE_RUNNING_CHAIR_HEADER.sub(_strip_if_isolated, text[:len(head)])
    return head2 + text[len(head):]


def remove_cid_artefacts(text: str) -> str:
    """Step 4 — strip (cid:N) tokens."""
    return RE_CID.sub("", text)


def remove_toc(text: str) -> str:
    """
    Step 5 — remove the table of contents, cover page, and attendance list.

    Everything before the first unambiguous proceedings opener is discarded:
      Pass A — "Beginn: HH.MM Uhr" or "Die Sitzung wird ... eröffnet"
      Pass B — first Präsident speaker line preceded by a blank line

    The attendance list (Verzeichnis der Anwesenden) and ToC are in the
    preamble, so discarding the entire preamble removes them cleanly.
    """
    # Collect every candidate opener position, then cut at the EARLIEST one.
    # Taking the earliest guarantees we never discard MORE of the preamble than
    # the previous strong/speaker logic did (so no session loses real content),
    # while the opener-phrase candidate recovers inline openings the strict
    # anchors skipped (session 321). Each candidate is a genuine session start,
    # so the text kept before the next one is never attendance-list preamble.
    candidates: list[int] = []

    m = RE_ANCHOR_STRONG.search(text)
    if m is not None:
        candidates.append(m.start())

    op = RE_ANCHOR_OPENER.search(text)
    if op is not None:
        para = text.rfind("\n\n", 0, op.start())
        candidates.append(para + 2 if para != -1 else op.start())

    # The standalone-chair line is the WEAK anchor: unlike "Beginn: HH.MM Uhr" and
    # the spoken opening phrase, it recurs throughout the sitting, so a match can
    # land anywhere. When the two reliable anchors are OCR-damaged and only this
    # one fires deep in the text, cutting there deletes the session rather than
    # the preamble — session 177 lost 86 % of its text that way.
    #
    # Corpus-wide the cut is a median 8.6 % of the text and 14.7 % at the 90th
    # percentile, so a weak match past _TOC_MAX_FRACTION is not a preamble and is
    # ignored. The reliable anchors are trusted at any depth: sessions 716 and 987
    # genuinely open 37 % and 54 % in, both confirmed by a printed "Beginn:" line.
    #
    # Keeping a preamble is the far cheaper failure — br_02's ToC filter and its
    # empty-body rule discard most roster lines, whereas a deleted speech is gone.
    m = RE_ANCHOR_SPEAKER.search(text)
    if m is not None and m.start() <= len(text) * _TOC_MAX_FRACTION:
        candidates.append(m.start())

    if not candidates:
        return text
    return text[min(candidates):]


def remove_page_numbers(text: str) -> str:
    """Step 6 — remove lines containing only a page/column reference."""
    return RE_PAGE_NUMBER.sub("", text)


def remove_footnotes(text: str) -> str:
    """Step 7 — remove footnote lines and colophon/publisher lines."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if RE_COLOPHON.match(line):
            continue
        if RE_FOOTNOTE_MARKER.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def remove_margin_markers(text: str) -> str:
    """Step 8 — remove OCR column/margin markers (A) (B) (C) (D).

    A marker that falls INSIDE a word is removed together with the whitespace
    around it, so the word is put back together: "Mehr(B) heit" → "Mehrheit",
    "erle(B) digten" → "erledigten". 1,674 words corpus-wide are broken this way,
    almost all in the two-column born-digital era.

    ⚠️ Both neighbours must be lowercase. "67 dB(A) bauliche Schallschutz-
    maßnahmen" is a UNIT, not a marker, and "Schwall(D)\\nDüren" is a hyphenated
    surname whose hyphen is lost — joining those would corrupt real text.
    """
    text = RE_MARGIN_MARKER_INWORD.sub(r"\1\2", text)
    text = RE_MARGIN_MARKER_PUNCT.sub("", text)
    text = RE_MARGIN_MARKER_EOL.sub("", text)
    text = RE_MARGIN_MARKER_BOL.sub("", text)
    return RE_MARGIN_MARKER.sub(" ", text)


def remove_bare_separators(text: str) -> str:
    """Step 9 — remove lines consisting solely of one or two hyphens/dashes."""
    return RE_BARE_SEPARATOR.sub("", text)


def remove_backm_references(text: str) -> str:
    """Step 10 — remove EU/Ratsdokument reference lines from back-matter."""
    start = RE_BACKM_START.search(text)
    if start is None:
        return text
    preamble   = text[:start.start()]
    backm      = text[start.start():]
    return preamble + RE_BACKM_REF.sub("", backm)


def normalise_whitespace(text: str) -> str:
    """Step 11 — strip trailing spaces and collapse excess blank lines.

    Also closes a space that has opened up before sentence punctuation. German
    never spaces before a comma or full stop, so this only ever repairs damage —
    typically left behind when a column marker sat between the word and its
    punctuation ("kein Mensch (C)." → "kein Mensch ." → "kein Mensch.").
    """
    lines = [line.rstrip() for line in text.split("\n")]
    text  = "\n".join(lines)
    text  = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text  = RE_EXCESS_BLANK.sub("\n\n", text)
    return text.strip()


_DEDUP_MIN_CHARS = 40   # a repeat this long is print damage, never speech
_DEDUP_MAX_LINES = 14   # longest repeated block observed is 4 lines


def remove_duplicated_blocks(text: str) -> str:
    """Step 11b — collapse a block of lines that is immediately repeated.

    `pdftotext` sometimes emits the same block twice on a two-column page, so
    the duplication is already present in the raw extraction — br_01 only has to
    undo it. Left in, it becomes a second speech attributed to the speaker who
    is already speaking:

        Präsident Dieter Althaus: Vielen Dank!
        Weitere Wortmeldungen liegen nicht vor.
        Präsident Dieter Althaus: Vielen Dank!      <- duplicate
        Weitere Wortmeldungen liegen nicht vor.     <- duplicate
        Wir kommen zur Abstimmung. Bayern hat in …

    Only an *immediately adjacent* repeat is removed. Procedural sentences recur
    constantly across a sitting ("Damit hat der Bundesrat dem Gesetz
    zugestimmt."), and those recurrences are real; what no transcript ever does
    is print the same 40+ characters twice back to back. Session 644 makes the
    case plainly — there the repeat lands in the middle of a sentence
    ("… ein Ansatz, der / entgegenkommt, was die Arbeits- und Sozialminister /
    entgegenkommt, was die Arbeits- und Sozialminister / der neuen Länder …").

    Blank lines between the two copies are ignored when matching, so the check
    survives the paragraph break that often separates them.
    """
    raw = text.split("\n")
    idx = [i for i, line in enumerate(raw) if line.strip()]
    content = [raw[i].strip() for i in idx]
    n = len(content)
    drop: set[int] = set()

    i = 0
    while i < n:
        found = 0
        for k in range(1, min(_DEDUP_MAX_LINES, (n - i) // 2) + 1):
            # a repeat has to start with the same line — cheap pre-filter
            if content[i + k] != content[i]:
                continue
            if (content[i:i + k] == content[i + k:i + 2 * k]
                    and sum(len(x) for x in content[i:i + k]) >= _DEDUP_MIN_CHARS):
                found = k
                break
        if found:
            # keep the second copy, so the block stays adjacent to what follows
            drop.update(idx[i:i + found])
            i += 2 * found
        else:
            i += 1

    if not drop:
        return text
    return "\n".join(line for j, line in enumerate(raw) if j not in drop)


def rejoin_hyphens(text: str) -> str:
    """
    Step 12 — rejoin words broken by end-of-line hyphenation.

    lowercase continuation → remove hyphen and join:  "Bun-\\ndesrat" → "Bundesrat"
    uppercase continuation → keep hyphen, drop newline: "Baden-\\nWürttemberg" → "Baden-Württemberg"

    ⚠️ Never join onto a line that starts a speaker attribution. Because
    RE_HYPHEN_BREAK allows "\\n+", a word broken at the foot of a column can reach
    across the blank line to the next speaker and swallow them:

        ... in Richtung auf eine aufga-
                                                  <- column break
        Erwin Sellering (Mecklenburg-Vorpommern): Herr Präsident! ...

    joined to "aufga-Erwin Sellering (Mecklenburg-Vorpommern): Herr ...", which
    is no longer at the start of a line, so br_02 cannot see it and the speech is
    lost into the previous one. 205 attributions corpus-wide are affected.
    The test errs towards refusing: a wrongly kept break leaves one hyphenated
    word split, while a wrong join costs a whole speech.
    """
    def _fix(m: re.Match) -> str:
        before, after = m.group(1), m.group(2)
        eol = text.find("\n", m.end())
        continuation = text[m.start(2):eol if eol >= 0 else len(text)]
        if RE_ATTRIBUTION_AHEAD.match(continuation):
            return m.group(0)                      # leave the break in place
        return before + after if after.islower() or after in "äöüß" else before + "-" + after

    return RE_HYPHEN_BREAK.sub(_fix, text)


# ---------------------------------------------------------------------------
# Full cleaning pipeline
# ---------------------------------------------------------------------------

PIPELINE = [
    ("fix_column_order",              fix_column_order),           # step 0 — before page-break removal
    ("remove_page_breaks",            remove_page_breaks),         # step 1
    ("remove_cid_artefacts",          remove_cid_artefacts),       # step 4 — before headers (some have cid tokens)
    ("remove_headers",                remove_headers),             # step 2
    ("remove_running_speaker_headers",remove_running_speaker_headers),  # step 3
    ("remove_toc",                    remove_toc),                 # step 5 — before page numbers
    ("remove_page_numbers",           remove_page_numbers),        # step 6
    ("remove_footnotes",              remove_footnotes),           # step 7
    ("remove_margin_markers",         remove_margin_markers),      # step 8
    ("remove_bare_separators",        remove_bare_separators),     # step 9
    ("remove_backm_references",       remove_backm_references),    # step 10
    ("normalise_whitespace",          normalise_whitespace),       # step 11
    ("remove_duplicated_blocks",      remove_duplicated_blocks),   # step 11b — after whitespace is uniform
    ("rejoin_hyphens",                rejoin_hyphens),             # step 12
    ("remove_headers",                remove_headers),             # second pass: residuals after hyphen join
    ("normalise_whitespace",          normalise_whitespace),       # final whitespace pass
]


def clean(text: str) -> str:
    """Run the full cleaning pipeline on a text string."""
    for _name, fn in PIPELINE:
        text = fn(text)
    return text


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def cleaned_path_for(txt_path: Path) -> Path:
    return txt_path.with_name(txt_path.stem + "_cleaned.txt")


# ---------------------------------------------------------------------------
# Log helper
# ---------------------------------------------------------------------------

def _append_log(log_path: Path, line: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(force: bool = False,
        preprocessed_dir=None,
        session_filter: set[str] | None = None) -> None:
    in_root = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)

    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    log_path = in_root / "cleaning.log"
    done: set[str] = set()
    if not force and log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    metadata_dir = in_root / "metadata"
    txt_files = sorted(
        p for p in in_root.rglob("*.txt")
        if not p.stem.endswith("_cleaned")
        and metadata_dir not in p.parents
    )

    print(f"Found {len(txt_files)} extracted .txt file(s) under {in_root}")

    ok_count = skip_count = error_count = 0

    for txt_path in txt_files:
        # Session filter: match the session number in the filename "Session_NNNN"
        if session_filter is not None:
            m = re.search(r"Session_(\d+)", txt_path.stem)
            if not m or m.group(1) not in session_filter:
                skip_count += 1
                continue

        out_path = cleaned_path_for(txt_path)

        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {txt_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {txt_path.name} ...", end=" ", flush=True)

        try:
            raw = txt_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            cleaned_text = clean(raw)
        except Exception as e:
            print(f"CLEAN ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            out_path.write_text(cleaned_text, encoding="utf-8")
            _append_log(log_path, str(out_path))
            print(f"OK -> {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bundesrat text cleaner")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing _cleaned.txt files.",
    )
    parser.add_argument(
        "--preprocessed-dir",
        default=None,
        metavar="PATH",
        help=f"Root preprocessed folder. Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="N[,N,...]",
        help="Comma-separated session numbers to process (e.g. '941,1000').",
    )
    args = parser.parse_args()

    session_filter = (
        {s.strip().zfill(4) for s in args.session.split(",")}
        if args.session else None
    )

    print("Bundesrat text cleaner")
    print(f"Input/Output: {args.preprocessed_dir or _DEFAULT_PREPROCESSED}")
    if session_filter:
        print(f"Session filter: {sorted(session_filter)}")
    print()

    run(
        force=args.force,
        preprocessed_dir=args.preprocessed_dir,
        session_filter=session_filter,
    )
