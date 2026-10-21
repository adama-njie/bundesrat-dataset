
import argparse
import csv
import os
import re
import sys
from pathlib import Path

# ============================================================
# USER SETTINGS  (override via --preprocessed-dir)
#
# Command-line flag wins, then the environment variable, then a folder beside
# this repository. See br_00 for why the fallback must stay relative.
# ============================================================
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PREPROCESSED = os.environ.get(
    "BUNDESRAT_PREPROCESSED_DIR", str(_PROJECT_ROOT / "preprocessed"))
_DEFAULT_METADATA_CSV = None  # auto-resolved: preprocessed/metadata/metadata_catalogue.csv
# ============================================================

# ---------------------------------------------------------------------------
# Bundesländer reference set
# ---------------------------------------------------------------------------

_BUNDESLAENDER: frozenset = frozenset({
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
})
# NB: canonical spellings only. OCR variants (dropped hyphens, stray brackets,
# mangled characters) are absorbed by _normalise_land, which always returns one
# of these strings — listing a variant here would leak it into the dataset as a
# second spelling of the same Land.

# States that existed in the early Federal Republic and later disappeared.
# Baden, Württemberg-Baden and Württemberg-Hohenzollern were founding members of
# the Bundesrat and merged into Baden-Württemberg in 1952 — they account for ~450
# attributions in the 1949–1952 sessions. They are kept under their own historical
# names rather than folded into Baden-Württemberg, which would be an anachronism.
_HISTORICAL_LAENDER: frozenset = frozenset({
    "Baden", "Württemberg-Baden", "Württemberg-Hohenzollern",
})

_ALL_LAENDER: frozenset = _BUNDESLAENDER | _HISTORICAL_LAENDER

# ---------------------------------------------------------------------------
# Name / title building blocks (shared with Bundestag approach)
# ---------------------------------------------------------------------------

_DOC_UNIT = r"(?:Dr\.(?:\s*-\s*[A-Za-z]+\.)?(?:\s+(?:[Hh]\.\s+[Cc]\.(?:\s+mult\.)?|[Ee]\.\s+[Hh]\.))?\ s+|D\.\s+)"
# "Dr." — OCR frequently prints the period as a comma ("Dr, KLEIN") or adds a
# stray comma after it ("Dr., KRAPP"), so both are tolerated.
# "h. c." is printed both spaced and unspaced ("Dr. h.c. Rau"), so the gap is \s*.
# "Dr." with the punctuation OCR actually produces: "Dr.", "Dr,", "Dr,.", "Dr.,"
# (240 occurrences), and sometimes with no space at all before the name
# ("Dr.EHARD") — hence [ \t]* rather than [ \t]+, which is safe because the name
# that follows must still start with a capital.
# The honorary suffix attaches to a bare "D." as well as to "Dr." — Heinrich
# Lübke is styled "Bundespräsident D. h. c. Lübke:" as often as "Dr. h. c.".
# Allowing it only after "Dr." lost those speeches entirely (session 209).
# The periods in "h. c." print as commas about as often as not — "Dr. h. c, Goppel",
# "Dr, h, c. Goppel", "Dr. h,c, Goppel" (85 occurrences corpus-wide).
_HON_SUFFIX = r"(?:\s*(?:[Hh][.,]{1,2}\s*[Cc][.,]{1,2}(?:\s+mult[.,])?|[Ee][.,]{1,2}\s*[Hh][.,]{1,2}))?"
# "Dr." itself is damaged: Df, Di., Dir., Dtir,, DPr., DBr., D-., DR., Ür‚.
# The letters between the D and the punctuation are restricted to the ones OCR
# actually substitutes for "r", which is what keeps "Der", "Die" and "Das" out —
# none of them can be followed immediately by a period or comma here.
# A speck may also sit between the title and the name ("Dr.' ANKERMÜLLER",
# "Dr.' BINGEIMANN"), and the punctuation run itself may carry a hyphen
# ("Dr..-FRANK"). The required following capital is what keeps this narrow.
_DOC_UNIT = (rf"(?:Dr[.,]{{1,2}}(?:\s*-\s*[A-Za-z]+\.)?{_HON_SUFFIX}[ \t]*['‘’`´]?[ \t]*"
             rf"|[DÜO][rfitlRPBO£]{{0,3}}[.,‚\-]{{1,3}}[ \t]*(?=[A-ZÄÖÜ])"
             rf"|D\.{_HON_SUFFIX}[ \t]+)")
# Engineering/other academic prefixes that precede the name: "Dipl.-Ing. Schwedler".
_DEGREE_UNIT = r"(?:Dipl\.[ \t]*-[ \t]*[A-Za-zäöü]{2,10}\.[ \t]+|Ing\.[ \t]+)"
# "Prof." is occasionally mis-OCR'd (Proi./Prot./Prol.); the variants are listed
# explicitly rather than wildcarded so ordinary words are never treated as titles.
# Same comma-for-period damage as "Dr," — "Prof, Dr. Maier (Bayern):", and
# occasionally both ("Prof,. Dr. Klug (Hamburg)").
# "Prof." loses or gains a letter as well as its period: Proi., Prot., Prol.,
# Profi., Proif. The "Pro" stem plus the required space keeps "Protokoll" out.
_PROF_UNIT = r"(?:Professor|Pro[iftlß{]{1,3})[.,]{0,2}[ \t]+"
# Modern office/degree abbreviations that appear only in the born-digital era and
# have no OCR damage, so they can be listed exactly:
#   "Ph.D. Jörg Dräger (Hamburg)"          — 3 attributions, sessions 806-825
#   "StS'in Sandra Gerken (Schleswig-Holstein)"  — Staatssekretärin
#   "RD'n Dr. Heide Wedemeyer (Hamburg)"   — Regierungsdirektorin
# The apostrophe is typeset variously (' ’ ` ´), hence the class.
_MODERN_TITLE = (r"(?:Ph[.,]?[ \t]*D[.,]|StS[’'`´]?(?:in|n)[.,]?"
                 r"|RD[’'`´]?n[.,]?|MdB[.,]?)[ \t]+")
_TITLE    = (rf"(?:(?:Frau|Herr)[ \t]+)?(?:{_MODERN_TITLE})?(?:{_PROF_UNIT})?"
             rf"(?:{_DEGREE_UNIT})*(?:{_DOC_UNIT})*(?:{_DEGREE_UNIT})*"
             rf"(?:{_MODERN_TITLE})?")

# A trailing "!" is a mis-read final "l" — the Bavarian/Austrian -l surnames are
# the usual victims: "Kies!" (Kiesl), "Streib!" (Streibl), "Dr. Seid!" (Seidl),
# "Hans Eiche!" (Eichel), and sometimes a spurious "!" after a complete name
# ("Dr. Vogel!"). _clean_name() repairs it; see _fix_bang_name().
# "]" and "1" join "!" as stand-ins for a final "l" — "Kies]", "Dr. Pirkl]",
# "Kies1l", "Dr. Sched1l", "Dr. HeubI]". _fix_bang_name() repairs all four.
_NAME_PART      = r"[A-ZÄÖÜ][a-zA-ZÄÖÜäöüßÀ-ɏ\-!\]1ﬂ]+"
# The period of an initial is printed as a comma too — "Harald B, Schäfer".
_INITIAL        = r"[A-ZÄÖÜ][.,]"
_NOBLE_PARTICLE = r"(?:von[ \t]+der|van[ \t]+der|von|van|de|zu|vom|zur)[ \t]+"

_FULL_NAME = (
    rf"{_TITLE}"
    rf"(?:{_NOBLE_PARTICLE})?"
    # A leading initial: "Prof. Dr. R. Alexander Lorz (Hessen):" — an initial was
    # only allowed *after* the first name part, so this modern-era name failed.
    rf"(?:{_INITIAL}[ \t]+)*"
    rf"{_NAME_PART}"
    # A speck can land BETWEEN two name tokens as well as before them —
    # "Dr. GEBHARD _ MÜLLER:", "Dr. HANS _ MÜLLER (Bayern)" — which used to end
    # the name at the first token and lose the attribution.
    rf"(?:[ \t]+(?:[_·‘'’.,]{{1,2}}[ \t]+)?"
    rf"(?:(?:{_NOBLE_PARTICLE}){_NAME_PART}|{_INITIAL}|{_NAME_PART}))*"
)

# ---------------------------------------------------------------------------
# Official federal roles (Pattern C)
# ---------------------------------------------------------------------------

# OCR-tolerant "Staatssekretär": the "Staats-" stem is frequently mis-OCR'd
# (Staaissekretär, Staatsekretär, Stautssekretär, Statssekretär, …) while the
# "-sekretär" suffix stays stable. Recovers ~30 attributions across the corpus.
# The "-sekretär" suffix is damaged too, not just the "Staats-" stem:
# "Staatssekrectär" (extra c), "Staatssekvetär" (r as v), "Staatssekretar"
# (umlaut lost). Keep "sek" and "t[aä]r" as the fixed points.
_STAATSSEKR = r"Sta\w{1,4}sek\w{1,3}t[aä]r(?:in)?"

_OFFICIAL_ROLES = (
    # "Bundesminister" plus the compound ministry titles used in the older
    # protocols: Bundesinnenminister, Bundesjustizminister, Bundesfinanzminister,
    # Bundeswirtschaftsminister, Bundesverkehrsminister, …
    r"Bundes(?:[a-zäöü]{2,20})?minister(?:in)?"
    r"|Bundeskanzler(?:in)?"
    # "Richard von Weizsäcker, Bundespräsident:" — the head of state addresses the
    # chamber in name-comma-role form, which had no matching role word at all.
    r"|Bundespräsident(?:in)?"
    # "Wolfgang Thierse, Präsident des Deutschen Bundestages:" — the presiding
    # officer of the OTHER chamber, and of the Ausschuss der Regionen, address
    # the Bundesversammlung in name-comma-role form.
    r"|Präsident(?:in)?[ 	]+des[ 	]+"
    rf"|Parl[.,][ \t]+{_STAATSSEKR}"
    # Spelled out in the older protocols: "Parlamentarischer Staatssekretär".
    rf"|Parlamentarisch\w{{0,3}}[ \t]+{_STAATSSEKR}"
    rf"|{_STAATSSEKR}"
    r"|Staatsminister(?:in)?"
    r"|Ministerpräsident(?:in)?"      # federal context (e.g. "Ministerpräsident ... Bundesrat")
    r"|Schriftführer(?:in)?"
    r"|Senator(?:in)?"                # Hamburg/Bremen/Berlin delegates to Bundesrat
    r"|Regierender[ \t]+Bürgermeister(?:in)?"
)

# ---------------------------------------------------------------------------
# Pre-2000 (scanned-era) building blocks
#
# The 1949–1999 protocols (recovered via OCR) use a different attribution
# style from the born-digital era:
#   * surnames are printed in ALL CAPS   ("WOLTERS", "Dr. HILPERT")
#   * the presiding officer is "PRÄSIDENT ARNOLD:" (role in caps, role-FIRST)
#   * federal officials are role-first    ("Vizekanzler Bundesminister BLÜCHER:")
# OCR also corrupts the umlaut in "PRÄSIDENT" (PRASIDENT, PRAÄSIDENT, PRXSIDENT…).
# ---------------------------------------------------------------------------

# An ALL-CAPS surname (optionally hyphenated), optionally preceded by a title.
_CAPS_SURNAME = r"[A-ZÄÖÜ][A-ZÄÖÜß]+(?:-[A-ZÄÖÜ][A-ZÄÖÜß]+)*"
_CAPS_NAME    = rf"{_TITLE}{_CAPS_SURNAME}"

# Chair role in caps, tolerant of OCR umlaut noise between "PR" and "SIDENT".
_CHAIR_ROLE_CAPS = r"(?:BUNDESTAGS|BUNDESRATS|BUNDES|ALTERS|VIZE)?PR[A-ZÄÖÜ]{1,3}SIDENT(?:IN)?"
# Titlecase chair role — used with a CAPS surname for the transitional format
# "Vizepräsident STEINHOFF:" (titlecase role, ALL-CAPS surname; 1950s–60s scans).
# "Präsident" in titlecase, tolerating the OCR corruptions actually observed in
# the scans while keeping the stem recognisable: PraÈsident (ä→aÈ), Fräsident
# (P→F), FPräsident (stray F), Präsideni (t→i). The Bundes/Alters/Vize prefix is
# matched separately so the role can still be canonicalised correctly.
# The trailing [tsi][a-z]? also covers the transposition "Präsidenit".
# The tail of "Präsident" is the part OCR mangles: Präsideht (n→h), Präsidenf,
# Präsideng, Präsiderit, Präsidenit, Präsideni. Only the two characters after
# "…side" are loosened — the "sid[eé]" core is what keeps "Präsidium" and
# "präsidiert" (both have "sidi", not "side") from matching.
# "Präsldent" / "Präslident" / "Prasldent" — the i of "sid" scanned as an l.
# "sid[eé]" is still required, which is what keeps "Präsidium" and "präsidiert"
# (both "sidi", not "side") from matching.
_PRAES_STEM = r"[PpFf]{1,2}r[aäeÈ]{1,3}s[il1!]{1,2}d[eé][nhri][tsifg]?[a-z]?(?:in)?"
# "Vizepräsident" is also mangled as a whole ("Virenräsident", "Vizeprasident"),
# so the Vize- prefix itself gets a tolerant form. Only words containing the
# "…äsident" core can match, so ordinary vocabulary is unaffected.
# "Vize" is damaged in its own right: "VizZepräsident" (a capital dropped inside
# the word), "Vižepräsident", "Vlzepräsident" (i read as l). The "präsiden" stem
# that follows is what keeps this from matching ordinary words.
_VIZE_OCR = r"V[il1][a-zA-ZäöüßžŽ]{1,4}[rp][aäeÈ]{1,3}s[il1!]{1,2}den[tsi]?[a-z]?(?:in)?"
# The chair may be introduced as acting ("Amtierender") or deputy
# ("Stellv. Präsident" / "Stellvertretender Präsident").
# "Amtierender" is damaged constantly — "Amtlerender" (i as l), "Amtierener"
# (d dropped). The chair role that has to follow is what keeps this specific.
_CHAIR_QUALIFIER = (r"(?:[AÄ]m[t!]?[il1!]{1,2}er\w{0,6}"
                    r"|(?:Erste|Zweite|Dritte|Vierte)[rn]?"   # "Dritter Vizepräsident"
                    r"|Stellv(?:[.,]|ertretende[rs]?)?)[ \t]+")

# Separator between a role word and the name that follows it. Normally a space,
# but OCR drops a speck in between ("Präsident. KOPF", "Präsident .KOPF",
# "Präsident‘ Dr. MAIER"). At least one space *or* one mark is required, so the
# role can never run straight into the name.
_ROLE_NAME_SEP = r"(?:[ \t]+[.,;:‘'’‚„|!_]?[ \t]*|[.,;‘'’‚„|!_][ \t]*)"
_CHAIR_ROLE_TITLE = (rf"(?:{_CHAIR_QUALIFIER})?"
                     rf"(?:{_VIZE_OCR}|(?:Bundestags|Bundesrats|Bundes|Alters|Vize)?{_PRAES_STEM})")
# Either casing of the chair role, for use ahead of a CAPS surname.
_CHAIR_ROLE_EITHER = rf"(?:{_CHAIR_ROLE_CAPS}|{_CHAIR_ROLE_TITLE})"

# A Bundestag MP presenting a committee report to the Bundesrat. The word is
# long and distinctive, and OCR damages it constantly — Bundestagsabgeoräneter,
# Bundestagsabgeordheter, Bundestagsabgecordnete, Bundestagsabgeordneten — so
# everything after the unmistakable "Bundestagsab" stem is left open.
_BT_ABGEORDNETER = r"Bundestags?ab\w{4,12}"   # the linking "s" is dropped too

# Role-first titles that also appear with a TITLE-CASE name rather than the CAPS
# surname _ROLE_FIRST expects — "Ministerpräsident Teufel:", "Minister Gölter:",
# "Bürgermeister Dr. Voscherau (Hamburg):". Only 14 corpus-wide, but each is a
# whole speech. Kept to this short, unambiguous list: a line opening with one of
# these words followed by a capitalised name is never ordinary prose.
_ROLE_FIRST_TC = (
    rf"{_BT_ABGEORDNETER}"
    r"|Regierende[rn]?[ \t]+Bürgermeister(?:in)?"
    r"|Ministerpräsident(?:in)?|Bürgermeister(?:in)?|Staatsminister(?:in)?"
    rf"|Staatssekretär(?:in)?|{_STAATSSEKR}|Senator(?:in)?|Minister(?:in)?"
    r"|Abgeordnete[rn]?"
)

# Role-first federal/official titles that precede an ALL-CAPS surname.
_ROLE_FIRST = (
    r"Vizekanzler(?:in)?"
    r"|Bundeskanzler(?:in)?"
    # Compound ministry titles too: "Bundesfinanzminister SCHÄFFER:".
    r"|Bundes(?:[a-zäöü]{2,20})?minister(?:in)?"
    r"|Bundesratspräsident(?:in)?"
    rf"|Parl[.,][ \t]+{_STAATSSEKR}"
    rf"|{_STAATSSEKR}"
    r"|Staatsminister(?:in)?"
    r"|Ministerpräsident(?:in)?"
    r"|Senator(?:in)?"
    r"|Regierender[ \t]+Bürgermeister(?:in)?"
    r"|Bürgermeister(?:in)?"
    # A Bundestag MP presenting a committee report to the Bundesrat
    # ("Bundestagsabgeordneter SCHOETTLE, Berichterstatter:"); the "\w{0,3}"
    # absorbs OCR endings such as "Bundestagsabgeordneier".
    # "\w{0,4}" after "…abgeordne" absorbs OCR endings: Bundestagsabgeordneter,
    # Bundestagsabgeordneiter, Bundestagsabgeordneier, …
    rf"|{_BT_ABGEORDNETER}"
    # The clerk who reads the roll / announces results: "Schriftführer ALBERTS:".
    # Common in the early sessions; the "\w{0,3}" absorbs OCR endings and the
    # doubled-f misread ("Schriffführer").
    # Doubled f is the commonest OCR misread here: Schriftführer /
    # Schriffführer / Schriftfführer (≈190 occurrences).
    r"|Schrif+t?f*ührer\w{0,3}"
    r"|Minister(?:in)?"
)

# State-representative building blocks (Pattern B).
# A (Bundesland) qualifier, tolerating OCR bracket noise: a stray { or [ before
# the "(", and a wrong closing bracket } or ] instead of ")" (both common in the
# 1949–1999 scans, e.g. "(Niedersachsen}").
# The closing class is repeated (+) because OCR sometimes emits BOTH the real
# ")" and a spurious "}"/"]" — e.g. "ZIETSCH (Bayern)}, Berichterstatter:".
# A stray "." or "," after the surname ("Dr. DUDEK. (Hamburg)") is also tolerated.
# Either bracket may be OCR-substituted, and the opening one is misread far more
# often than the closing one ("Dr. KLEIN {Berlin)", ~1,050 occurrences), so the
# OPENING class accepts ( { [ as well — not merely a stray brace before a "(".
# A speck also lands between the name and the qualifier — "Dr. WEITZ _ (NRW)",
# "Dr. DUDEK_ (Hamburg)" — which used to end the name before the paren was seen.
# The speck between the name and the qualifier is also a dash ("ALTMEIER - (RLP)")
# or a single stray letter ("ALBERTZ i(Niedersachsen)").
_MEMBER_PAREN = (r"(?:[ \t]*(?:[.,_·‘'’‚„“”?!\-]{1,2}|[ \t][A-Za-z]))?[ \t]*"
                 # A stray letter or a doubled bracket can follow the closing
                 # paren before the colon: "RENNER (Württemberg-Hohenzollern)y:",
                 # "Dr. RINGELMANN (Bayern)}):".
                 r"[{\[]?[({\[][^)\n]+[)}\]]+[A-Za-z]?[)}\]]*")
# Optional trailing role after the (Land) qualifier:
#   "Name (Land), Berichterstatter:"   (the dominant form, ~3,000 attributions)
#   "Name (Land), Schriftführer:" / ", Antragsteller:" / ", Generalberichterstatter:"
# The role word is matched permissively (any 5–30 letter word) because the
# qualifier before it must already validate as a real Bundesland — that is what
# guards against false positives. Being permissive here absorbs the ~400 OCR
# spellings of "Berichterstatter" (Berichterstatier, Berichtersiatter, …) without
# enumerating them; parse_speaker() normalises them back to canonical form.
# The separator may be "," ";" "." or absent, with an optional "_" OCR speck.
#
# Both halves are damaged constantly in the scans, and because the (Land) in
# front has already been validated as a real Bundesland, tolerance here is cheap:
#   separator  "(Hessen),, Berichterstatter"   "(Baden),. Berichterstatter"
#   role word  "Berichterstat-\n{er:"  "Bericht-\n\n” erstatfer;"  "Berichter-Statter:"
# The role is *typeset* as a hyphenated line break that br_01's rejoin could not
# repair, because the continuation line starts with an OCR speck rather than a
# letter. The continuation is only followed when a hyphen or underscore invites
# it, so an ordinary role word cannot run on into the speech body.
# The specks are separated by spaces as often as not (", _Berichterstatter"),
# so whitespace is allowed *between* them, not just around the group.
_ROLE_SEP  = r"(?:[ \t]*[,;._\-‚'‘’\"“”«»·]){0,3}[ \t]*"
# Digits and specks land INSIDE the role word too — "Berichterstat-4er",
# "Ber1chterstat\er", "Ber1ch“terstatter", "Berichteıstatter". Safe because
# _normalise_member_role() still has to recognise the result as a real role.
#
# The class also has to carry the accented Latin letters OCR substitutes for
# ordinary ones — "Bér.ichfferstatter", "äériéhtersiattéf", "SchriftfuÈhrer",
# "Schriftfçhrerin", "I£erichterstatter". Listing each glyph was the losing move
# here, exactly as it was for the chair stem: the whole À-ɏ range is admitted and
# _normalise_member_role() decides, since it still has to find a "…richt…" /
# "…erstat…" / "…chriftf…" core before anything is stored as a role.
_ROLE_TAIL = (r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9“”'‘’ıì£.\-\\/]{4,30}"
              r"(?:[-_][ \t]*(?:[^\w\s]|_|\n){0,4}[ \t]*[A-Za-zÀ-ɏ]{2,25})?")
# The trailing role may carry an acting/deputy qualifier — "Dr. Thomas Mirow
# (Hamburg), Amtierender Schriftführer:" — which _ROLE_TAIL alone cannot reach,
# because it matches one word and "Amtierender" is not itself a role. A trailing
# bracket is tolerated where the colon was doubled ("Berichterstatfer):").
_ROLE_QUALIFIER = r"(?:[Aa]mtierende[rsn]?[ \t]+|[Ss]tellv\.?[ \t]+)?"
_MEMBER_ROLE_SUFFIX = rf"(?:{_ROLE_SEP}{_ROLE_QUALIFIER}{_ROLE_TAIL}[)}}\]]?)?"

# Report-introduction heading that names the speaker, e.g.
# "Bericht des Ministers Dr. Heubl (Bayern) zu Punkt 1 der Tagesordnung:".
# The minister giving the report is the speaker; role is Berichterstatter.
# The "zu Punkt N der Tagesordnung" tail may wrap across one OCR line break, so
# the pre-colon run uses [^:] (crosses newlines) rather than [^:\n], bounded to 80.
_REPORT_INTRO = (
    rf"Bericht[ \t]+de[rs][ \t]+(?:Staats)?[Mm]inister(?:s|in)?[ \t]+"
    rf"{_FULL_NAME}[ \t]*\([^)\n]+\)[^:]{{0,80}}"
)

# Speck at the START of an attribution line. The scans routinely carry a stray
# mark where the column rule or a dirty margin was scanned in:
#   ". Dr. BEYERLE (Württemberg-Baden), Berichterstatter:"
#   "_ Dr. HOFMEISTER (Niedersachsen), Berichterstatter:"
#   "' Dr. DUDEK (Hamburg), Berichterstatter:"   "- PRASIDENT ARNOLD:"
# Because the patterns anchor on ^, one such character used to block the whole
# attribution and merge the speech into the previous one (301 occurrences).
# Kept to at most two marks and no letters, so prose is unaffected.
# The speck often arrives in pairs (". .", "_ _", "...", "- -"), and a
# mis-bracketed column marker can sit in the same place
# ("{A)! NEUENKIRCH (Hamburg), Berichterstatter:"), so up to two junk units are
# allowed, each either a bracketed column letter or one to three marks.
#
# The column marker itself is often unrecognisable — "@A}", "®", "@®" — because
# the scan renders the bracketed letter as a blob, so its opening bracket may be
# a stray glyph or missing altogether.
# The column letter is itself mis-scanned as a digit ("{4}"), and the marker's
# brackets as "%" or "£".
#
# ⚠️ The CLOSING bracket is mandatory. Making it optional let a bare "A" or "D"
# at the start of a line count as a marker, so "ALTMEIER (Rheinland-Pfalz):"
# parsed as "Ltmeier" and "DR. ANKERMÜLLER" as "R. Ankermüller" — session 5 lost
# seven speeches before this was caught.
_LINE_JUNK = (r"(?:(?:(?:[({\[]|[@®©%£])?[ABCD0-9][)}\]]"
              r"|[.,;:_\-–—'‘’‚„\"“”`´*|/\\!·•@®©»«ı°¢€§&+…%£]{1,3})[ \t]*){0,3}")

# ---------------------------------------------------------------------------
# Main speaker-attribution regex
# ---------------------------------------------------------------------------

RE_SPEAKER_LINE = re.compile(
    rf"^\s*{_LINE_JUNK}(?:"
    # Pattern A: chair / presiding officer (Bundesratspräsident, Vizepräsident, Alterspräsident)
    # Also covers "Bundespräsident" (special guest, Pattern D)
    rf"(?P<chair>"
    rf"(?:Frau[ \t]+|Herr[ \t]+)?"
    rf"{_CHAIR_ROLE_TITLE}"
    # OCR leaves a speck between the role and the name ("Präsident. KOPF",
    # "Präsident‘ Dr. MAIER", "Präsident .KOPF"), so a stray mark is allowed here.
    rf"{_ROLE_NAME_SEP}{_FULL_NAME}"
    rf"(?:[ \t]*\([^)]*\bbegrüßt\b[^)]*\))*"
    rf")"
    rf"|"
    # Pattern A-caps (pre-2000): "PRÄSIDENT ARNOLD" (caps role) and the
    # transitional "Vizepräsident STEINHOFF" (titlecase role + CAPS surname).
    rf"(?P<chair_caps>{_CHAIR_ROLE_EITHER}[ \t]+{_CAPS_NAME})"
    rf"|"
    # Pattern C-pre (pre-2000): role-first official "Vizekanzler Bundesminister BLÜCHER".
    rf"(?P<official_pre>(?:{_ROLE_FIRST})(?:[ \t]+(?:{_ROLE_FIRST}))*[ \t]+{_CAPS_NAME}{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    # Same, but with a Title-case surname. From ~1959 the MP's name is no longer
    # set in caps ("Bundestagsabgeordneter Dr. Schellenberg, Berichterstatter:").
    # Its own alternative rather than a relaxation of _ROLE_FIRST above: letting
    # every role-first title accept a Title-case name would start matching prose.
    # The optional paren is the MP's constituency ("Seidl (Dorfen)", "Dr. Schmidt
    # (Wuppertal)") and is deliberately NOT read as a Land — a Bundestag MP does
    # not represent one here, even when the town shares a name with a state.
    rf"(?P<official_pre_tc>(?:{_ROLE_FIRST_TC})[ \t]+{_FULL_NAME}"
    rf"(?:{_MEMBER_PAREN})?{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    # Report-intro heading: "Bericht des Ministers NAME (Land) zu Punkt N …:".
    rf"(?P<report_intro>{_REPORT_INTRO})"
    rf"|"
    # Pattern B: state representative — name + (Bundesland) qualifier(s), with an
    # optional trailing committee-rapporteur role ("Name (Land), Berichterstatter").
    # The qualifier MUST be a Bundesland; we validate this in parse_speaker().
    rf"(?P<member>{_FULL_NAME}(?:{_MEMBER_PAREN})+{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    # Pattern C: federal official — name + comma + role + optional description
    rf"(?P<official>{_FULL_NAME},[ \t]*(?:{_OFFICIAL_ROLES})[^\n:]{{0,120}}"
    rf"(?:\n\n?(?!Tagesordnungspunkt[ \t]*\d|Anlage[ \t]*\d|\d{{4,}}[ \t])[^\n:]{{0,120}}){{0,2}})"
    rf")"
    # OCR sometimes prints the closing colon as a semicolon ("Präsident KOPF;"),
    # ~98 occurrences; the attribution structure before it is what keeps this safe.
    rf"\s*[:;]\s*$",
    re.MULTILINE,
)

# Inline speaker: same patterns but colon is followed by text on the same line.
# This is the DOMINANT format in the Bundesrat.
RE_INLINE_SPEAKER = re.compile(
    rf"^\s*{_LINE_JUNK}(?:"
    rf"(?P<chair_inline>"
    rf"(?:Frau[ \t]+|Herr[ \t]+)?"
    rf"{_CHAIR_ROLE_TITLE}"
    # OCR leaves a speck between the role and the name ("Präsident. KOPF",
    # "Präsident‘ Dr. MAIER", "Präsident .KOPF"), so a stray mark is allowed here.
    rf"{_ROLE_NAME_SEP}{_FULL_NAME}"
    rf"(?:[ \t]*\([^)]*\bbegrüßt\b[^)]*\))*"
    rf")"
    rf"|"
    rf"(?P<chair_caps_inline>{_CHAIR_ROLE_EITHER}[ \t]+{_CAPS_NAME})"
    rf"|"
    rf"(?P<official_pre_inline>(?:{_ROLE_FIRST})(?:[ \t]+(?:{_ROLE_FIRST}))*[ \t]+{_CAPS_NAME}{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    rf"(?P<official_pre_tc_inline>(?:{_ROLE_FIRST_TC})[ \t]+{_FULL_NAME}"
    rf"(?:{_MEMBER_PAREN})?{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    rf"(?P<report_intro_inline>{_REPORT_INTRO})"
    rf"|"
    rf"(?P<member_inline>{_FULL_NAME}(?:{_MEMBER_PAREN})+{_MEMBER_ROLE_SUFFIX})"
    rf"|"
    rf"(?P<official_inline>{_FULL_NAME},[ \t]*(?:{_OFFICIAL_ROLES})[^\n:]{{0,120}}"
    rf"(?:\n\n?(?!Tagesordnungspunkt[ \t]*\d|Anlage[ \t]*\d|\d{{4,}}[ \t])[^\n:]{{0,120}}){{0,2}})"
    rf")"
    rf"\s*[:;][ \t]*(?=\S)",
    re.MULTILINE,
)

# Splits a chair attribution into role + name.
RE_CHAIR_PREFIX = re.compile(
    r"^(?:Frau[ \t]+|Herr[ \t]+)?"
    rf"({_CHAIR_ROLE_TITLE})"
    rf"{_ROLE_NAME_SEP}(.+)$",
    re.IGNORECASE,
)

# Splits a federal-official attribution into name + role.
RE_OFFICIAL_SPLIT = re.compile(
    rf"^({_FULL_NAME}),[ \t]*((?:{_OFFICIAL_ROLES}).*)$",
    re.DOTALL,
)

# Pre-2000 splitters.
# Caps/mixed chair: "PRÄSIDENT ARNOLD" / "Vizepräsident STEINHOFF" → role, name.
RE_CHAIR_CAPS_PREFIX = re.compile(rf"^({_CHAIR_ROLE_EITHER})[ \t]+(.+)$", re.DOTALL)
# Role-first official: "Vizekanzler Bundesminister BLÜCHER" → role, name.
# The name half accepts either casing: CAPS in the 1950s protocols, Title case
# from ~1959 ("Bundestagsabgeordneter Dr. Schellenberg"). Without the Title-case
# alternative the split silently fails and the role word stays glued to the name,
# splitting one MP into "Westphal" and "Bundestagsabgeordneter Westphal".
# A trailing constituency paren ("Seidl (Dorfen)") is matched and discarded — it
# is not a Land and there is no field for it.
RE_OFFICIAL_PRE_SPLIT = re.compile(
    rf"^((?:{_ROLE_FIRST})(?:[ \t]+(?:{_ROLE_FIRST}))*)[ \t]+"
    rf"({_CAPS_NAME}|{_FULL_NAME})(?:{_MEMBER_PAREN})?$",
    re.DOTALL,
)

# Parenthesised qualifier extractor.
RE_PAREN_QUALIFIER = re.compile(r"\(([^)]+)\)")

# TOC page-reference patterns for the Bundesrat.
# Early sessions have 1–2 digit page refs with column letters; later sessions
# have 2–5 digit refs.  Column letter (A–D) is required to avoid false positives
# on year references.
_RE_TOC_PAGEREF = re.compile(r"\b\d{1,5}\*?[ \t]*[A-D]\b")

# TOC section-heading patterns specific to Bundesrat ToC bodies.
_RE_TOC_BODY = re.compile(
    r"(?:"
    r"Beschluss\s*:"                   # vote result lines in TOC
    r"|\bDrucksache\b"                 # Drucksache references
    r"|^\S.*\d{1,5}[ \t]*[A-D]\s*$"  # "Name (State) . . . 11 C" style
    r")",
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _land_key(s: str) -> str:
    """Reduce a Land name to letters only, umlauts folded — an OCR-robust key.

    Drops brackets, spaces, hyphens and stray marks, so "Nordrhein - Westfalen",
    "N iedersachsen", "Niedersachsen]" and "Nordrhein-West£falen" all collapse to
    the same key as the canonical spelling.
    """
    s = s.lower()
    # A capital H scanned as its two separate strokes, with the crossbar read as a
    # hyphen: "Schleswig-l-lolstein", "I-Iamburg", "Württeh1berg-l-lohenzollern".
    s = re.sub(r"[li1][-–—][li1]", "h", s)
    # Digits standing in for the letters they resemble ("Schlesw1g-Holstem").
    # These have to be mapped BEFORE the strip below, which would delete them.
    s = s.replace("1", "i").replace("0", "o")
    # "ii" is the standard mis-read of "ü" ("Wiirttemberg" for "Württemberg").
    s = s.replace("ii", "u")
    s = (s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
          .replace("ß", "ss").replace("é", "e").replace("è", "e"))
    s = re.sub(r"[^a-z]", "", s)
    # "rn" and "in" both collapse to "m" once the strokes merge — "Bayem" for
    # "Bayern", "Holstem" for "Holstein", "Nordrhem" for "Nordrhein". Folding
    # both sides turns these into zero-distance matches instead of risky guesses.
    s = s.replace("rn", "m").replace("in", "m")
    # "l" and "i" are freely interchanged by these scans ("Sch1eswig", "Pfaiz").
    # Applied last, so it cannot feed back into the "ii" rule above.
    return s.replace("l", "i")


_LAND_BY_KEY: dict[str, str] = {_land_key(b): b for b in _ALL_LAENDER}

# Historical/administrative spellings that are the same delegation under another
# name. Keyed through _land_key so the entries cannot go stale when that fold
# changes — writing the folded form by hand silently broke this once already.
_LAND_ALIASES: dict[str, str] = {
    _land_key(k): v for k, v in {
        "Groß-Berlin": "Berlin",    # Berlin's official name until 1990
    }.items()
}

# The constitutional long forms. Stripped before matching rather than listed as
# aliases, so "Freistaat Bayern" and a damaged "Freistaat Bayem" both resolve.
_LAND_PREFIX = re.compile(
    r"^(?:das[ \t]+)?(?:Land[ \t]+|Freistaat[ \t]+"
    r"|Freie[rn]?[ \t]+(?:und[ \t]+)?Hansestadt[ \t]+|Hansestadt[ \t]+)",
    re.IGNORECASE,
)


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance between a and b, capped at limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1] if prev[-1] <= limit else limit + 1


def _normalise_land(raw: str) -> str | None:
    """Return the canonical Land name for a parenthesised qualifier, else None.

    Three tiers, in order of confidence:
      1. exact match on the canonical spelling;
      2. exact match on the letters-only key — absorbs stray brackets, spacing
         and hyphen damage ("(Niedersachsen]", "Nordrhein - Westfalen");
      3. fuzzy match within 2 edits of a key, for OCR damage the fold above
         does not cover ("Schleswig-Hoistein", "Baden-Wçrttemberg"). The
         *closest* key wins; a tie between two different Länder is refused
         rather than guessed at (the historical "Baden" sits two edits from
         "Bayern", so that pair has to be decided on distance, not presence).
    """
    s = raw.strip().strip("[](){}<>.,;:'\"` ")
    if s in _ALL_LAENDER:
        return s
    # "Freistaat Bayern", "Freie Hansestadt Bremen", "Land Niedersachsen".
    s = _LAND_PREFIX.sub("", s).strip()
    if s in _ALL_LAENDER:
        return s

    key = _land_key(s)
    if not key:
        return None
    if key in _LAND_BY_KEY:
        return _LAND_BY_KEY[key]
    if key in _LAND_ALIASES:
        return _LAND_ALIASES[key]

    if len(key) >= 5:
        best, best_d = None, 3
        for k, name in _LAND_BY_KEY.items():
            if len(k) < 5:
                continue
            d = _edit_distance(key, k, 2)
            if d > 2:
                continue
            if d < best_d:
                best, best_d = name, d
            elif d == best_d and name != best:
                best = None              # tied between two Länder — refuse
        return best
    return None


def _clean_name(raw: str) -> str:
    """Normalise whitespace and academic-title punctuation in a speaker name.

    OCR prints the period of "Dr." as a comma ("Dr, Ehard") or adds a stray one
    ("Dr., Altmeier"). Left as-is these fragment a person's identity across the
    corpus — "Dr, Ehard" and "Dr. Ehard" would be two different speakers — so the
    title is canonicalised here (~190 rows).
    """
    s = re.sub(r"[ \t]+", " ", raw.strip())
    # The (?!-) guard keeps hyphenated compounds intact ("Dipl.-Ing. Schwedler").
    s = re.sub(r"\b(Dr|Prof|Dipl)[.,]+\s*,?\s*(?!-)", r"\1. ", s)
    # The mis-OCR'd forms of "Prof." reach the name too ("Proi, Dr. Heuss"), and
    # left in they split the speaker. A following capital and at least one
    # period/comma are both required, so "Protokoll" is never touched.
    s = re.sub(r"\b(?:Professor|Pro[iftlß{]{1,3})[.,]{0,2}[ \t]*(?=[A-ZÄÖÜ])", "Prof. ", s)
    # ...and so do the mis-OCR'd forms of "Dr." — Dtir,, Dir., DPr., DBr., Ür‚.
    # At least one letter after the D is required, so a bare "D." (Heinrich Lübke
    # is styled "D. h. c.") is left alone, as are "Der", "Die" and "Das", none of
    # which can be followed immediately by a period or comma here.
    # The D itself is sometimes read as an O, or an O is inserted after it:
    # "Or. Haak", "DOr. Rüdiger", "Prof. Or. Schreckenberger". Folding these back
    # matters more than the attribution did — left alone, "Or. Haak" is a second
    # speaker beside "Dr. Haak" (24 speeches) and "DOr. Rüdiger" beside
    # "Dr. Rüdiger" (79).
    # The "r" may also be read as "£" ("D£. Weitz"), and the punctuation run may
    # carry a hyphen ("Dr..-Frank"). Kept in step with _DOC_UNIT, whose classes
    # these mirror — a tolerance added there but not here leaves the damage in
    # `name` and gives the speaker a second identity.
    # The (?![Ii]ng) guard is what keeps "Dr.-Ing. Müller" intact — the hyphen is
    # inside the punctuation class, so without it the engineering degree is eaten.
    s = re.sub(r"\b(?:[DÜO][rfitlRPBO£]{1,3}|Ü[rfitlRPB£]{0,3})[.,‚\-]{1,3}[ \t]*"
               r"(?![Ii]ng\b)(?=[A-ZÄÖÜ])",
               "Dr. ", s)
    # A punctuation run left between the title and the name ("Dr..-FRANK"): the
    # rule above stops at the first period because of its (?!-) guard. Restricted
    # to "Dr" and to a run that opens with a period/comma, so "Dipl.-Ing." and
    # "Dr.-Ing." are untouched.
    s = re.sub(r"\bDr\.\s*[.,‚]{1,2}\s*-?\s*(?=[A-ZÄÖÜ])", "Dr. ", s)
    # "Ph.D." is typeset both spaced and unspaced.
    s = re.sub(r"\bPh[.,]?[ \t]*D[.,][ \t]*(?=[A-ZÄÖÜ])", "Ph.D. ", s)
    # "h. c." with commas for its periods, so "Dr. h, c. Goppel" and
    # "Dr. h. c. Goppel" do not become two speakers.
    s = re.sub(r"\b([Hh])[.,]{1,2}\s*([Cc])[.,]{1,2}", r"\1. \2.", s)
    # The period after the title is dropped outright often enough to matter
    # ("Dr Ehard", "Prof Dr. Kurt Biedenkopf"). A capitalised token must follow,
    # so an ordinary word is never mistaken for a title.
    s = re.sub(r"\b(Dr|Prof|Dipl)[ \t]+(?=[A-ZÄÖÜ])", r"\1. ", s)
    # "Frau"/"Herr" is an honorific, never part of the name. Keeping it split the
    # same person in two ("Frau Simonis" vs "Simonis"); the original form is still
    # preserved in speaker_raw. A following token is required so a bare surname
    # "Herr" is left alone.
    s = re.sub(r"^(?:Frau|Herr)n?\s+(?=\S)", "", s)
    # A speck stranded between two name tokens ("Dr. GEBHARD _ MÜLLER") is print
    # damage, not part of the name; leaving it in would split the speaker.
    s = re.sub(r"\s+[_·‘'’.,]{1,2}\s+", " ", s)
    # The period of an initial printed as a comma ("Harald B, Schäfer").
    s = re.sub(r"\b([A-ZÄÖÜ]),(?=\s)", r"\1.", s)
    # ⚠️ _fix_bang_name MUST run before the trailing-speck strip below. A trailing
    # "!" is usually the final "l" of the name, not a speck ("Erwin Teufe!",
    # "Hans Eiche!", "Dr. Voge!"); stripping first silently truncated 12 real
    # surnames to "Erwin Teufe", "Hans Eiche", "Dr. Voge". "!" is therefore also
    # absent from the class below — by this point it has already been consumed.
    s = _fix_bang_name(s)
    # Specks trailing the last name token — "Dr. Hans Müller „‚", "Hans Eichel?",
    # "Dr. FRANK.‚". The lookbehind spares a genuine trailing initial ("Harald B.")
    # while still stripping the period off an ALL-CAPS surname — an initial is a
    # LONE capital, so the \b inside the lookbehind is doing the real work.
    s = re.sub(r"(?<!\b[A-ZÄÖÜ])[.,;:_·‘'’‚„“”«»?]+$", "", s)
    return s.strip()


def _fix_bang_name(s: str) -> str:
    """Repair a name token whose final "l" was scanned as "!", "]" or "1".

    "Kies!" → "Kiesl", "Streib!" → "Streibl", "Hans Eiche!" → "Hans Eichel",
    "Kies]" → "Kiesl", "Kies1l" → "Kiesl", "Dr. Sched1l" → "Dr. Schedl".
    When the token already ends in "l" the mark is spurious rather than a
    substituted letter ("Dr. Vogel!", "Apel!", "Dr. Pirkl]"), so it is simply
    dropped instead of doubling the l.
    """
    # A trailing capital I after lowercase letters is an l, not an I — no German
    # surname ends in one ("Dr. HeubI]" → "Dr. Heubl]" → "Dr. Heubl").
    s = re.sub(r"(?<=[a-zäöüß])I(?=[lL])", "", s)
    s = re.sub(r"(?<=[a-zäöüß])I(?![a-zäöüß])", "l", s)
    # A mark immediately followed by an l is a spurious insert ("Goppe!l",
    # "Kies1l"), as is one that follows an l ("Dr. Vogel!", "Dr. Pirkl]");
    # anywhere else it stands in for the l.
    s = re.sub(r"[!\]1](?=[lL])", "", s)
    return re.sub(r"([A-Za-zÄÖÜäöüß])[!\]1]",
                  lambda mm: mm.group(1) if mm.group(1) in "lL" else mm.group(1) + "l",
                  s)


def _fix_caps_name(name: str) -> str:
    """
    Convert ALL-CAPS surname tokens (a pre-2000 print convention) to Title
    case for consistency with the born-digital era, so the same person is not
    split into "KOPF" (1949) and "Kopf" (2000). Titles (Dr., Prof., D.) and
    mixed-case tokens are left untouched.
    """
    def fix(tok: str) -> str:
        core = tok.replace(".", "")
        if len(core) >= 2 and core.isupper():
            return "-".join(p.capitalize() for p in tok.split("-"))
        return tok
    return " ".join(fix(t) for t in name.split(" "))


def _normalise_member_role(word: str) -> str | None:
    """
    Canonicalise the role word that follows a "(Bundesland)" qualifier.

    OCR mangles "Berichterstatter" in ~30 different ways (Berichterstatier,
    Berichtersiatter, Berichierstatter, DBerichterstatter …); they all keep a
    recognisable "…richt…" / "…erstat…" core, so they are folded back to the
    canonical spelling. Returns None for a word that is not a known role, so the
    caller can leave `role` empty rather than storing body text.
    """
    w = word.strip()
    lw = w.lower()

    # Berichterstatter family (incl. General-/Mit- prefixes and OCR variants)
    if re.search(r"richt|erstat|ersiat|erstai|ersta[tf]", lw):
        prefix = ""
        if lw.startswith("general"):
            prefix = "General"
        elif lw.startswith("mit"):
            prefix = "Mit"
        female = lw.endswith("erin") or lw.endswith("terin")
        # German compounds keep the stem lowercase after a prefix:
        # "Mitberichterstatter", not "MitBerichterstatter".
        stem = "berichterstatter" if prefix else "Berichterstatter"
        return f"{prefix}{stem}{'in' if female else ''}"

    if lw.startswith("schriftf") or "chriftf" in lw:
        return "Schriftführerin" if lw.endswith("in") else "Schriftführer"
    if lw.startswith("antragst"):
        return "Antragstellerin" if lw.endswith("in") else "Antragsteller"
    if lw.startswith("fragest"):
        return "Fragesteller"
    return None


_RE_NAME_ROLE_PREFIX = re.compile(
    r"^(Abgeordnete[rn]?|Ministerpräsident(?:in)?|Staatsminister(?:in)?|"
    r"Staatssekretär(?:in)?|Bürgermeister(?:in)?|Senator(?:in)?|Minister(?:in)?)"
    r"\s+(?=\S)"
)


def _demote_role_prefix(parsed: dict) -> None:
    """Move a role word that leaked into `name` back into `role`, in place.

    "Senator Wolters" / "Minister Zietsch" split the same person off from their
    main entry ("Wolters", 116 speeches), so the title is stripped from the name.
    It is preserved in `role` when that field is otherwise empty, and
    `speaker_raw` always keeps the original string.
    """
    name = parsed.get("name") or ""
    m = _RE_NAME_ROLE_PREFIX.match(name)
    if not m:
        return
    stripped = name[m.end():].strip()
    if not stripped:
        return
    parsed["name"] = _clean_name(stripped)
    if not parsed.get("role"):
        parsed["role"] = m.group(1)


# ---------------------------------------------------------------------------
# OCR name-variant lookup (docs/name_variants.csv)
#
# OCR produces one-character corruptions of surnames ("Kopfi"/"Koptf" for "Kopf",
# "Koschnuick" for "Koschnick"), which otherwise become separate speakers. The
# merges live in a reviewable CSV rather than in code, because deciding that two
# name strings are the same person is a research judgement, not a parsing rule.
#
# Only high-confidence pairs belong in that file: the variant is rare, is one
# edit from a far more frequent name, shares its Bundesland, and does not carry a
# conflicting role. That last test is what keeps genuinely different politicians
# apart — "Erhard"/"Ehard", "Kaiser"/"Kaisen" and "Wörner"/"Börner" are each one
# edit apart but are different people, distinguishable by their roles.
#
# The file is optional: if it is missing, names are left exactly as parsed.
# ---------------------------------------------------------------------------

_NAME_VARIANTS: dict[str, str] = {}


def load_name_variants(path: Path) -> dict[str, str]:
    """Load {variant → canonical} from a two-column CSV. Missing file → {}."""
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            v = (row.get("variant") or "").strip()
            c = (row.get("canonical") or "").strip()
            if v and c and v != c:
                mapping[v] = c
    return mapping


def _apply_name_variant(name: str | None) -> str | None:
    """Map an OCR-variant spelling to its canonical name, if one is registered."""
    if not name:
        return name
    return _NAME_VARIANTS.get(name, name)


# ---------------------------------------------------------------------------
# Bare-surname attributions (bootstrapped)
#
# The 1949–55 protocols sometimes attribute a speech with nothing but the
# surname: the chair has just announced who is speaking ("Das Wort hat Herr
# Bundesminister Hellwege,") and the speech then opens "HELLWEGE: ...".
# There is no role and no (Land) to make the line safe, so a bare CAPS token
# plus a colon on its own would also swallow "SPD:", "DDR:", "BGB:", "EXPO:",
# "ANMERKUNG:".
#
# The gate: accept the line only if that surname is INDEPENDENTLY confirmed as
# a speaker somewhere in the corpus by one of the stronger patterns. Measured
# on the full corpus this accepts 82 occurrences (46 distinct, every one a real
# politician) and rejects 14 (11 distinct, every one a non-name) — the only
# genuine losses are "Dr. WERZ" (2) and the OCR typo "KUBDEL" (1).
#
# _KNOWN_SURNAMES is empty by default, which disables the pattern entirely; run()
# fills it from a pre-pass over EVERY cleaned file present, so the result does
# not depend on which sessions the current invocation happens to write.
# ---------------------------------------------------------------------------

_KNOWN_SURNAMES: set[str] = set()

RE_CAPS_BARE = re.compile(
    rf"^[ \t]*{_LINE_JUNK}"
    rf"(?P<caps_bare>"
    # ALL-CAPS surname, with or without a title: "HELLWEGE:", "Dr. ADENAUER:"
    rf"(?:{_DOC_UNIT})?[A-ZÄÖÜ][A-ZÄÖÜ\-]{{2,24}}"
    # Title-case surname — an academic title is REQUIRED here ("Dr. Jaeger:"),
    # since a bare Title-case word before a colon is ordinary German prose.
    rf"|(?:{_PROF_UNIT})?(?:{_DOC_UNIT}){{1,2}}[A-ZÄÖÜ][a-zäöüß\-]{{2,20}}"
    rf")"
    rf"[ \t]*[:;][ \t]+(?=\S)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# State representative with NO terminator at all
#
# OCR sometimes drops the colon outright, leaving the speech running straight on
# from the qualifier:
#     "Dr. MEYEES (Nordrhem—West;ﬁalen) Herr Pra-sident! Meine Herren! …"
# Without a terminator the ordinary patterns cannot see it, and the whole turn is
# swallowed by the previous speaker (session 146).
#
# A missing terminator is far too weak a signal on its own, so this pattern is
# filtered in split_speeches() the same way RE_CAPS_BARE is: the parenthesis must
# validate as a real Bundesland, and the line must not be a table-of-contents
# entry. Those two conditions are what make it safe.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Chair attribution whose ROLE WORD is damaged past any character class
#
# "Präsident" is nine characters and the scans destroy one or two of them in
# ways no hand-written alternation keeps up with: Präsigent, Präskient,
# Prästdent, Präsdent, Präisdent, Präéident, PPESIDENT, PRASIBENT, PRXSIDEN,
# V11eprasuient, szepräsldent, Vizeprasxdent. Each new spelling was costing a
# whole speech, because the chair's turn is what separates two floor speakers.
#
# So the stem is matched the way a Bundesland is: fold the token to letters and
# accept it if it is within two edits of a real chair word. The NAME half must
# still parse as _FULL_NAME, which is what keeps prose out — "Bundespräsident
# Roman Herzog hat schon 1997 gesagt:" fails because "Roman Herzog hat schon
# 1997 gesagt" is not a name.
# ---------------------------------------------------------------------------

_CHAIR_CANON: dict[str, str] = {
    "prasident": "Präsident",                 "prasidentin": "Präsidentin",
    "vizeprasident": "Vizepräsident",         "vizeprasidentin": "Vizepräsidentin",
    "bundesprasident": "Bundespräsident",     "altersprasident": "Alterspräsident",
    "bundestagsprasident": "Bundestagspräsident",
    "bundesratsprasident": "Bundesratspräsident",
}


def _chair_key(s: str) -> str:
    """Fold a candidate role word to letters only, as _land_key does."""
    s = s.lower()
    s = (s.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
          .replace("é", "e").replace("è", "e").replace("1", "i").replace("0", "o"))
    s = re.sub(r"[^a-z]", "", s)
    # "l" and "i" are interchangeable in these scans ("szepräsldent"), the same
    # fold _land_key applies. Safe here because the vocabulary is eight strings.
    return s.replace("l", "i")


def _chair_role_fuzzy(token: str) -> str | None:
    """Canonical chair role for a damaged role word, else None.

    Closest match within two edits; a tie between two different roles is refused,
    exactly as in _normalise_land.
    """
    k = _chair_key(token)
    if len(k) < 7:
        return None
    best, best_d = None, 3
    for kk, role in _CHAIR_CANON.items():
        d = _edit_distance(k, kk, 2)
        if d > 2:
            continue
        if d < best_d:
            best, best_d = role, d
        elif d == best_d and role != best:
            best = None
    return best


RE_CHAIR_FUZZY = re.compile(
    rf"^[ \t]*{_LINE_JUNK}"
    # The token shape is deliberately permissive — _chair_role_fuzzy() is the gate,
    # so there is no need to enumerate the glyphs OCR substitutes (é, ı, £, ﬂ …).
    rf"(?P<chair_fuzzy>[^\s:;°,]{{6,22}})"
    rf"{_ROLE_NAME_SEP}"
    rf"(?P<chair_fuzzy_name>{_FULL_NAME})"
    rf"[ \t]*[:;°][ \t]*",
    re.MULTILINE,
)


# The chair's colon is dropped too ("Präsident Engholm  Die Tagesordnungspunkte
# 1 bis 4 werde ich …"). Gated hard in split_speeches(): the role must fuzzy-match
# a chair word, the surname must already be a confirmed speaker, and real speech
# text must follow on the same line.
#
# ⚠️ The separator may NOT be "!". "Herr Präsident! Meine Herren! Ich …" is the
# vocative that opens a speech, and reading it as an attribution produced 1,085
# false positives on the first attempt.
RE_CHAIR_NO_COLON = re.compile(
    rf"^[ 	]*{_LINE_JUNK}(?P<chair_nc_role>[^\s:;°,!]{{6,22}})[ 	]{{1,3}}"
    rf"(?P<chair_nc>(?:{_DOC_UNIT})?[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{{2,24}})"
    rf"[ 	]{{1,6}}(?=[A-ZÄÖÜ])",
    re.MULTILINE,
)


RE_MEMBER_NO_COLON = re.compile(
    rf"^[ \t]*{_LINE_JUNK}"
    rf"(?P<member_nc>{_FULL_NAME}(?:{_MEMBER_PAREN})+{_MEMBER_ROLE_SUFFIX})"
    # …either the speech runs straight on, or the colon is replaced by a speck
    # and the line simply ends ("… Berichterstatter'", "… Berichterstatter.").
    rf"(?:[ \t]+(?=[A-ZÄÖÜ])|[ \t]*[^\w\s:;]{{0,3}}[ \t]*$)",
    re.MULTILINE,
)

# A ToC line carries dot leaders and/or a trailing page reference
# ("Dr. Katz (Schleswig-Holstein) Berichterstatter . ..0..0.001. .‚ 131A").
_RE_TOC_TAIL = re.compile(r"\.\s*\.|\.{3,}|\d{1,5}\s*\*?\s*[A-D]\s*$")

# A bare chair-role word, in any case ending, is never a speaker's name — see the
# session-opening formula handled at the end of split_speeches().
_RE_ROLE_WORD_ONLY = re.compile(
    r"^(?:Vize|Bundes|Alters|Minister)?[Pp]r[aä]sid[ei]n?[a-z]{0,4}$", re.IGNORECASE)


def _surname_key(name: str) -> str:
    """Key a name by its last token: umlauts folded, non-letters dropped.

    Deliberately NOT _land_key(). That fold is tuned for a closed set of 19
    strings whose collisions have been checked one by one, and it is far too
    aggressive for 2,000-odd surnames — l/i, in/m and ii/u together collapse
    about 120 distinct speakers onto shared keys, which would quietly widen the
    bare-surname gate.
    """
    parts = name.strip().split()
    if not parts:
        return ""
    s = parts[-1].lower()
    s = (s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
          .replace("ß", "ss").replace("é", "e").replace("è", "e"))
    return re.sub(r"[^a-z]", "", s)


def _surname_confirmed(token: str) -> bool:
    """True if this bare surname is a confirmed speaker, allowing one OCR slip.

    The exact check carries almost everything; the distance-1 tier exists for
    surnames the scan corrupts by a single character ("KUBDEL" for "KUBEL",
    session 6). Measured corpus-wide, that tier admits exactly that one case and
    no noise, because the confirmed set contains only real speaker surnames.
    Closest-wins with ties refused, as in _normalise_land.

    NB: this decides only whether the LINE is an attribution. It deliberately
    does not rename the speaker — deciding that two spellings are the same person
    is a research judgement and belongs in docs/name_variants.csv.
    """
    key = _surname_key(re.sub(rf"^{_DOC_UNIT}", "", token).strip())
    if not key:
        return False
    if key in _KNOWN_SURNAMES:
        return True
    if len(key) < 5:
        return False
    best = None
    for k in _KNOWN_SURNAMES:
        if len(k) >= 5 and _edit_distance(key, k, 1) <= 1:
            if best is not None and best != k:
                return False           # ambiguous — refuse rather than guess
            best = k
    return best is not None


# The chair hands the floor over by name just before the speech starts — "Ich
# darf Herrn Dr. Werz bitten, darüber einiges zu sagen," followed by "Dr. WERZ:".
# That announcement is a second, independent way to confirm a bare surname is a
# person, and it rescues speakers who are NEVER attributed by a strong pattern
# anywhere in the corpus (Dr. Werz is the case that motivated it). Measured: it
# adds exactly "Dr. WERZ" and none of SPD / CSU / DDR / BGB / EXPO / ANMERKUNG,
# which are never introduced by an honorific.
_ANNOUNCE_HONORIFIC = (
    r"(?:Herrn?|Frau|Dr\.|Prof\.|Minister\w*|Staatsminister\w*|Staatssekretär\w*"
    r"|Senator\w*|Bürgermeister\w*|Präsident\w*|Abgeordnete\w*|Kollege\w*)"
)
_ANNOUNCE_WINDOW = 1500


def build_known_surnames(texts) -> set[str]:
    """Collect surnames confirmed as speakers, for the bare-surname pattern.

    Two independent sources of confirmation:
      1. the surname is attributed by one of the strong patterns somewhere;
      2. the chair announces it by name shortly before a bare-surname line.
    """
    known: set[str] = set()
    pending: list[tuple[str, int, str]] = []   # (text, position, surname)

    for text in texts:
        for rx in (RE_SPEAKER_LINE, RE_INLINE_SPEAKER):
            for m in rx.finditer(text):
                nm = parse_speaker(m).get("name")
                if nm:
                    k = _surname_key(nm)
                    if k:
                        known.add(k)
        for m in RE_CAPS_BARE.finditer(text):
            tok = m.group("caps_bare")
            surname = re.sub(rf"^{_DOC_UNIT}", "", tok).strip()
            if surname:
                pending.append((text, m.start(), surname))

    for text, pos, surname in pending:
        if _surname_key(surname) in known:
            continue
        window = text[max(0, pos - _ANNOUNCE_WINDOW):pos]
        if re.search(rf"{_ANNOUNCE_HONORIFIC}[ \t]+(?:Dr\.[ \t]*)?{re.escape(surname.title())}\b",
                     window, re.IGNORECASE):
            known.add(_surname_key(surname))
    return known


def _canonical_chair_role(raw_role: str) -> str:
    """Canonicalise a titlecase chair role, repairing OCR damage to the stem.

    "PraÈsident"/"Fräsident"/"Präsideni" → "Präsident", while the meaningful
    qualifiers are preserved: Amtierender/Amtierende, Bundes-, Alters-, Vize-,
    and the feminine -in ending.
    """
    s = re.sub(r"\s+", " ", raw_role.strip())
    # Built from _CHAIR_QUALIFIER itself. It used to carry its own copy of the
    # pattern, which drifted out of sync: a damaged "Amtlerender Präsident" and an
    # ordinal "Dritter Vizepräsident" both fell through and were flattened to a
    # plain "Präsident", losing the acting/vice distinction.
    m = re.match(rf"^({_CHAIR_QUALIFIER})?(.+)$", s, re.IGNORECASE)
    if not m:
        return s
    amt = m.group(1) or ""
    core = m.group(2).strip()
    cl = core.lower()
    # Normalise both qualifiers to one canonical spelling, so a damaged
    # "Amtlerender Präsident" does not become its own role value. The acting
    # qualifier agrees with the gender of the role that follows.
    if amt and amt.lower().startswith("stellv"):
        amt = "Stellvertretender "
    elif amt and amt.lower()[:2] in ("am", "äm"):
        amt = "Amtierende " if cl.endswith("in") else "Amtierender "
    else:
        # An ordinal ("Dritter Vizepräsident") is dropped: the Bundesrat does not
        # number its vice-presidents, and keeping it would fragment the role
        # vocabulary for four rows.
        amt = ""
    # Match the qualifier on its opening letters so OCR damage further along the
    # word ("Virenräsident" for "Vizepräsident") still resolves correctly.
    # "vi" covers Vizepräsident/Virenräsident; "vl"/"v1" the i-read-as-l variants
    # (Vlzepräsident), which would otherwise be demoted to a plain "Präsident".
    if cl[:2] in ("vi", "vl", "v1"):
        pre = "Vize"
    # The longer prefixes must be tested first: "bundes" also matches
    # "Bundestagspräsident", which would be flattened to "Bundespräsident" —
    # a different office presiding over a different chamber.
    elif cl.startswith("bundestags"):
        pre = "Bundestags"
    elif cl.startswith("bundesrats"):
        pre = "Bundesrats"
    elif cl.startswith("bundes"):
        pre = "Bundes"
    elif cl.startswith("alters"):
        pre = "Alters"
    else:
        pre = ""
    fem = cl.endswith("in")
    # German compounds keep the stem lowercase after a prefix: "Vizepräsident",
    # but a standalone role is capitalised: "Präsident".
    stem = "präsident" if pre else "Präsident"
    return f"{amt}{pre}{stem}{'in' if fem else ''}".strip()


def _canonical_caps_chair_role(raw_role: str) -> str:
    """Map an OCR-noisy caps chair role (PRÄSIDENT/PRXSIDENT/VIZEPRÄSIDENT…)
    to a canonical title."""
    u = raw_role.upper()
    if u.startswith("VIZE"):
        return "Vizepräsident"
    if u.startswith("ALTERS"):
        return "Alterspräsident"
    if u.startswith("BUNDES"):
        return "Bundespräsident"
    return "Präsident"


def _normalise_official_role(role: str) -> str:
    """Fold the OCR spellings of a federal office title back to canonical form.

    The "-sekretär" suffix is damaged as often as the stem ("Staatssekvetär",
    "Staatssekrectär", "Staatssekretar"), and "Parl." prints with a comma. Left
    as-is each variant becomes its own role value.
    """
    role = re.sub(r"Parl[.,]", "Parl.", role)
    role = re.sub(r"Sta\w{1,4}sek\w{1,3}t[aä]r(in)?",
                  lambda m: "Staatssekretär" + (m.group(1) or ""), role)
    role = re.sub(r"Bundesmin\w*isterium", "Bundesministerium", role)
    return role


def parse_speaker(match: re.Match) -> dict:
    """
    Extract name, role, and land from a regex match against RE_SPEAKER_LINE
    or RE_INLINE_SPEAKER.

    Returns a dict with keys: name, role, land, speaker_raw.
    All values may be None if not applicable.
    """
    # Determine which pattern group matched (strip "_inline" suffix for inline).
    # Use groupdict() so we only test groups that exist in THIS match's pattern:
    # RE_SPEAKER_LINE defines chair/member/official; RE_INLINE_SPEAKER defines
    # the *_inline variants. Calling match.group("chair_inline") on a
    # RE_SPEAKER_LINE match would raise "no such group".
    gd = match.groupdict()
    group_name = None
    for g in ("chair", "chair_caps", "official", "official_pre", "report_intro", "member",
              "chair_inline", "chair_caps_inline", "official_inline",
              "official_pre_inline", "report_intro_inline", "member_inline",
              "official_pre_tc", "official_pre_tc_inline",
              "caps_bare", "member_nc", "chair_fuzzy", "chair_nc"):
        if gd.get(g) is not None:
            group_name = g
            break

    if group_name is None:
        return {"name": None, "role": None, "land": None, "speaker_raw": match.group(0)}

    raw = match.group(group_name).strip()
    speaker_raw = raw

    # --- Chair whose closing colon OCR dropped altogether ---
    if group_name == "chair_nc":
        return {"name": _fix_caps_name(_clean_name(raw)),
                "role": _chair_role_fuzzy(match.group("chair_nc_role")),
                "land": None,
                "speaker_raw": f"{match.group('chair_nc_role')} {raw}".strip()}

    # --- Chair whose role word is damaged past the character classes ---
    if group_name == "chair_fuzzy":
        role = _chair_role_fuzzy(raw)
        nm = match.group("chair_fuzzy_name")
        return {"name": _fix_caps_name(_clean_name(nm)), "role": role,
                "land": None, "speaker_raw": f"{raw} {nm}".strip()}

    # --- Bare surname (1949–55 era): "HELLWEGE:", "Dr. ADENAUER:" ---
    # No role and no Land — the chair announced the speaker in the previous
    # sentence ("Das Wort hat Herr Bundesminister Hellwege,"). Only reached when
    # the surname is already a confirmed speaker (see _KNOWN_SURNAMES).
    if group_name == "caps_bare":
        return {"name": _fix_caps_name(_clean_name(raw)), "role": None,
                "land": None, "speaker_raw": speaker_raw}

    # --- Pattern A-caps (pre-2000): "PRÄSIDENT ARNOLD" ---
    if "chair_caps" in group_name:
        m = RE_CHAIR_CAPS_PREFIX.match(raw)
        if m:
            role = _canonical_caps_chair_role(m.group(1))
            name = _fix_caps_name(_clean_name(m.group(2)))
        else:
            role = "Präsident"
            name = _fix_caps_name(_clean_name(raw))
        return {"name": name, "role": role, "land": None, "speaker_raw": speaker_raw}

    # --- Pattern A / D: chair or Bundespräsident (born-digital) ---
    if "chair" in group_name:
        m = RE_CHAIR_PREFIX.match(raw)
        if m:
            role = _canonical_chair_role(m.group(1))
            name = _fix_caps_name(_clean_name(m.group(2)))
        else:
            role = "Präsident"
            name = _fix_caps_name(_clean_name(raw))
        return {"name": name, "role": role, "land": None, "speaker_raw": speaker_raw}

    # --- Pattern C-pre (pre-2000): role-first official "Vizekanzler ... BLÜCHER" ---
    if "official_pre" in group_name:
        # Peel off a trailing rapporteur role first, e.g.
        # "Bundestagsabgeordneter SCHOETTLE, Berichterstatter".
        # A separator is required here (unlike _MEMBER_ROLE_SUFFIX, which has a
        # validated Land in front of it) so the last name token is never mistaken
        # for a role. The role word itself may be hyphen-broken: "Berichter-Statter".
        trailing_role = None
        # The separator is optional because _normalise_member_role() below is the
        # real gate — it returns None for anything that is not a recognised role,
        # so "NIEDERALT Berichterstatter" (comma dropped by OCR) peels while a
        # plain two-token surname cannot.
        mt = re.search(rf"(?:[ \t]*[,;._‚]){{0,3}}[ \t]+({_ROLE_TAIL})[ \t]*$", raw, re.DOTALL)
        if mt:
            canon = _normalise_member_role(re.sub(r"[^A-Za-zÄÖÜäöü]", "", mt.group(1)))
            if canon:
                trailing_role = canon
                raw = raw[:mt.start()].rstrip()

        m = RE_OFFICIAL_PRE_SPLIT.match(raw)
        if m:
            role = re.sub(r"\s+", " ", m.group(1).strip())
            # Fold OCR spellings of the MP title back to canonical form
            # (Bundestagsabgeordneiter / -eier / … → Bundestagsabgeordneter).
            role = re.sub(_BT_ABGEORDNETER, "Bundestagsabgeordneter", role)
            # "Schriffführer"/"Schriftfuhrer" → canonical spelling
            role = re.sub(r"Schrif+t?f*ührer\w{0,3}", "Schriftführer", role)
            name = _fix_caps_name(_clean_name(m.group(2)))
        else:
            name = _fix_caps_name(_clean_name(raw))
            role = None
        # The rapporteur role (if any) is the more specific description here.
        if trailing_role:
            role = f"{role}, {trailing_role}" if role else trailing_role
        return {"name": name, "role": role, "land": None, "speaker_raw": speaker_raw}

    # --- Pattern C: federal official (born-digital, and pre-2000 caps surnames
    #     in name-comma-role form like "SCHÄFFER, Bundesminister der Finanzen") ---
    if "official" in group_name:
        m = RE_OFFICIAL_SPLIT.match(raw)
        if m:
            name = _fix_caps_name(_clean_name(m.group(1)))
            # \s+ (not just [ \t]+) because a long ministry title routinely wraps
            # across a line: "Bundesminister für gesamtdeutsche\nAngelegenheiten".
            role = re.sub(r"\s+", " ", m.group(2).strip())
            role = _normalise_official_role(role)
            # When OCR prints the closing colon as ";" the pattern runs on into the
            # speech text. Official titles never contain a semicolon, so cutting
            # there restores a clean role (~16 rows). The swallowed text stays in
            # the role's original span — a known, rare limitation.
            if ";" in role:
                role = role.split(";", 1)[0].strip()
        else:
            name = _fix_caps_name(_clean_name(raw))
            role = None
        return {"name": name, "role": role, "land": None, "speaker_raw": speaker_raw}

    # --- Report-intro heading: "Bericht des Ministers NAME (Land) zu Punkt N …" ---
    if "report_intro" in group_name:
        m = re.search(r"(?:Staats)?[Mm]inister(?:s|in)?[ \t]+(.+?)[ \t]*\(([^)]+)\)", raw)
        if m:
            name = _fix_caps_name(_clean_name(m.group(1)))
            land = _normalise_land(m.group(2).strip())
            return {"name": name, "role": "Berichterstatter", "land": land,
                    "speaker_raw": speaker_raw}
        # fall through to member handling if the split unexpectedly fails

    # --- Pattern B: state representative ---
    work = raw
    role = None

    # Trailing committee-rapporteur role: "Name (Land), Berichterstatter"
    # (tolerating a stray "_" OCR speck before the role word).
    # The role word sits after the closing bracket of the (Land) qualifier,
    # separated by "," ";" "." or nothing. Keep the bracket so the Land still
    # parses; only a recognised role word is accepted (see _normalise_member_role).
    # The separator after the closing bracket collects specks of its own —
    # ")‚ Berichterstatier" — and a low quote is not a comma, so the role word
    # stayed glued to the name.
    # Uses _ROLE_TAIL so the damaged spellings the pattern now matches
    # ("Berichterstat-4er") are peeled here too, rather than left inside the name.
    #
    # ⚠️ This carried its own hand-written copy of the separator class and the
    # copy drifted: every speck added to _ROLE_SEP (a quote, a guillemet) and the
    # qualifier added to _MEMBER_ROLE_SUFFIX made the ATTRIBUTION match while the
    # peel here silently failed, so the role word stayed inside the name —
    # "Lübke 'Berichterstatter", "Dr. Heinsen « Berichterstatter",
    # "Dr. Thomas Mirow Amtierender Schriftführer" all became their own speakers.
    # It is now built from _ROLE_SEP and _ROLE_QUALIFIER so it cannot drift again.
    #
    # ⚠️ The trailing bracket is part of that mirror and was missed the first time.
    # _MEMBER_ROLE_SUFFIX ends "[)}\]]?" for the doubled-colon shape
    # "BÜHRNSEN (Schleswig-Holstein), Berichterstatter)", so without it here the
    # attribution matched and the peel failed — leaving "Bührnsen Berichterstatter"
    # and "Dr. Katz Berichterstatter" as their own speakers, which then surfaced in
    # person_review.csv as a component to "adjudicate" that was really a parse bug.
    mrole = re.search(
        rf"[)}}\]]{_ROLE_SEP}({_ROLE_QUALIFIER})({_ROLE_TAIL})[)}}\]]?[ \t]*$", work)
    if mrole:
        canon = _normalise_member_role(mrole.group(2))
        if canon:
            qual = (mrole.group(1) or "").strip()
            role = f"{qual[:1].upper()}{qual[1:]} {canon}" if qual else canon
            work = work[:mrole.start() + 1].rstrip()   # +1 keeps the closing bracket

    # Strip a stray OCR bracket before the (Land) paren: "Wertz {(NRW)" → "Wertz (NRW)".
    work = re.sub(r"[{\[]+(?=\()", "", work)
    # A brace standing IN PLACE of the opening paren: "{Berlin)" → "(Berlin)".
    # The content class is deliberately permissive: OCR drops specks inside the
    # Land too ("{Rheinland-?faiz)"), and a restrictive class made the lookahead
    # fail so the brace stayed and the whole qualifier was kept as part of the
    # name. Nothing is lost by being generous here — _normalise_land() still has
    # to recognise whatever is inside before a `land` is set.
    # A speck can also sit between the brace and the name: "{_Rheinland-‘.?fa.lz)".
    work = re.sub(r"[{\[](?=[_ \t]{0,2}[A-ZÄÖÜ][^)}\]\n]{2,28}[)}\]])", "(", work)
    # Normalise a wrong closing bracket to ")": "(Niedersachsen}" → "(Niedersachsen)".
    work = re.sub(r"[}\]](?=[,: ]|$)", ")", work)

    # Extract parenthesised qualifiers. In the Bundesrat a qualifier is a Bundesland.
    qualifiers = RE_PAREN_QUALIFIER.findall(work)
    base_name  = RE_PAREN_QUALIFIER.sub("", work).strip()
    # A doubled OCR bracket ("Kopf (Niedersachsen))") leaves an orphan ")" behind
    # once the qualifier is removed — strip any such stray bracket/punctuation.
    # "_" and the stray quotes belong to the same speck family — left on, they
    # split "Dr. Weitz _" from "Dr. Weitz".
    base_name  = re.sub(r"[\s,;.({\[)}\]_·‘'’\-]+$", "", base_name).strip()
    # A lone letter left dangling before the qualifier is a speck, not an initial
    # ("ALBERTZ i(Niedersachsen)"); a real initial carries a period.
    base_name  = re.sub(r"\s+[A-Za-z]$", "", base_name).strip()
    name       = _fix_caps_name(_clean_name(base_name))

    land = None
    for q in qualifiers:
        candidate = _normalise_land(q.strip())
        if candidate:
            land = candidate
            break
        # Some pre-2000 attributions carry a role in the parenthesis instead of
        # a Bundesland, e.g. "ALBERTZ (Schriftführer):".
        if role is None and re.search(r"Schriftführer|Berichterstatter|Bürgermeister|Minister",
                                       q, re.IGNORECASE):
            role = _clean_name(q)

    return {"name": name, "role": role, "land": land, "speaker_raw": speaker_raw}


# ---------------------------------------------------------------------------
# TOC filtering
# ---------------------------------------------------------------------------

def _is_toc_entry(sp: dict, text_after: str) -> bool:
    """
    Return True if this apparent speaker attribution is a TOC artefact.

    Two signals:
    1. The raw attribution itself contains an embedded page reference.
    2. The speech body (text_after) consists mostly of TOC-style reference lines.
    """
    if _RE_TOC_PAGEREF.search(sp.get("speaker_raw", "")):
        return True
    stripped = text_after.strip()
    if not stripped:
        return True
    # If more than half the non-blank lines match TOC body patterns, discard.
    lines = [l for l in stripped.splitlines() if l.strip()]
    if not lines:
        return True
    toc_hits = sum(1 for l in lines if _RE_TOC_BODY.search(l))
    return toc_hits / len(lines) > 0.5


# ---------------------------------------------------------------------------
# Speech splitter
# ---------------------------------------------------------------------------

def split_speeches(text: str) -> list[dict]:
    """
    Split cleaned session text into individual speech segments.

    Both standalone attribution lines (ending with ":") and inline attributions
    (colon immediately followed by text) are detected and merged into a unified
    list of match positions.  Speeches are extracted as the text between
    consecutive attribution positions.

    Returns a list of dicts (unsorted by sequence; caller adds sequence numbers).
    Each dict has: speaker_raw, name, role, land, char_offset, text.
    """
    # Collect all attribution matches from both patterns.
    matches: list[tuple[int, int, re.Match]] = []

    for m in RE_SPEAKER_LINE.finditer(text):
        matches.append((m.start(), m.end(), m))

    for m in RE_INLINE_SPEAKER.finditer(text):
        # Only add if not already covered by a standalone match at same position.
        already = any(abs(m.start() - s) < 5 for s, _e, _m in matches)
        if not already:
            matches.append((m.start(), m.end(), m))

    # Bare-surname attributions ("HELLWEGE:"), only for surnames already
    # confirmed as speakers by the patterns above. Skipped entirely when
    # _KNOWN_SURNAMES is empty, and never allowed to override a real match:
    # a bare CAPS token that falls inside an existing attribution span is a
    # fragment of it, not a new speaker.
    if _KNOWN_SURNAMES:
        spans = [(s, e) for s, e, _m in matches]
        for m in RE_CAPS_BARE.finditer(text):
            if not _surname_confirmed(m.group("caps_bare")):
                continue
            if any(s <= m.start() < e for s, e in spans):
                continue
            matches.append((m.start(), m.end(), m))

    # Chair attributions whose role word is too damaged for the ordinary stems.
    # Gated on _chair_role_fuzzy(), so an arbitrary word cannot pose as the chair.
    spans = [(s, e) for s, e, _m in matches]
    for m in RE_CHAIR_FUZZY.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if _chair_role_fuzzy(m.group("chair_fuzzy")) is None:
            continue
        matches.append((m.start(), m.end(), m))

    # Chair turns whose colon vanished. Three gates, all required.
    spans = [(s, e) for s, e, _m in matches]
    for m in RE_CHAIR_NO_COLON.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if _chair_role_fuzzy(m.group("chair_nc_role")) is None:
            continue
        if _surname_key(m.group("chair_nc").split()[-1]) not in _KNOWN_SURNAMES:
            continue
        eol = text.find(chr(10), m.end())
        if len(text[m.end():eol if eol >= 0 else len(text)].strip()) < 15:
            continue
        matches.append((m.start(), m.end(), m))

    # State representatives whose terminator OCR dropped entirely. Only accepted
    # when the qualifier really is a Bundesland and the line is not a ToC entry —
    # without both, "no colon" would match ordinary prose.
    spans = [(s, e) for s, e, _m in matches]
    for m in RE_MEMBER_NO_COLON.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        parsed = parse_speaker(m)
        if parsed.get("land") is None:
            continue
        # ⚠️ Anything after the closing bracket must be a REAL role. Without this
        # the pattern reads prose that merely lists people — "von Frau Staatsrätin
        # Böhning (Bremen) sowie Herrn Staatssekretär Bischoff (Saarland)" — by
        # swallowing "sowie" as a role word and matching on the capital after it.
        raw = m.group("member_nc")
        tail = raw[raw.rfind(")") + 1:] if ")" in raw else ""
        if tail.strip(" \t,;.·_‘'’") and parsed.get("role") is None:
            continue
        bol = text.rfind("\n", 0, m.start()) + 1
        eol = text.find("\n", m.end())
        eol = eol if eol >= 0 else len(text)
        # The ToC prints the same "Name (Land), Berichterstatter" shape, so the
        # whole line is tested — dot leaders or a trailing page reference mean it
        # is a contents entry, not a speech.
        if _RE_TOC_TAIL.search(text[bol:eol]):
            continue
        # When the line simply ENDS (no speech text after it), a valid role is
        # required: "Name (Land)" alone on a line is a running column header,
        # which br_01 normally strips, not somebody speaking.
        if not text[m.end():eol].strip() and parsed.get("role") is None:
            continue
        matches.append((m.start(), m.end(), m))

    # Sort by position in text.
    matches.sort(key=lambda x: x[0])

    # Drop running column headers. Where a speech runs from the foot of one
    # column to the head of the next, the protocol reprints the speaker as a
    # header, which br_01 cannot strip because it carries a colon and so looks
    # like an attribution:
    #     "... Ich möchte den Herrn Vertreter von"
    #     ". Dr. HILPERT (Hessen): ."          <- header
    #     "Rheinland-Pfalz dringend bitten, ..."
    # Left in, it cuts one speech in two. TWO conditions are needed together:
    # the line carries no speech text after the colon (only a speck), AND it
    # repeats the speaker who is already speaking. Either alone is wrong — 63
    # attributions corpus-wide have a punctuation-only tail but genuinely start
    # a speech on the next line, and speakers are legitimately re-attributed
    # after a vote. Together they select exactly the real headers.
    deduped: list[tuple[int, int, re.Match]] = []
    header_spans: list[tuple[int, int]] = []
    prev_raw = None
    for start, end, mm in matches:
        raw = parse_speaker(mm)["speaker_raw"]
        if raw == prev_raw:
            eol = text.find("\n", end)
            eol = eol if eol >= 0 else len(text)
            rest = text[end:eol]
            if rest.strip() and not re.search(r"[^\W_]", rest):
                header_spans.append((start, eol))   # excised from the body below
                continue
        deduped.append((start, end, mm))
        prev_raw = raw
    matches = deduped

    if not matches:
        return []

    speeches: list[dict] = []
    toc_filter_active = True   # discard TOC-lookalike attributions at session start

    for i, (start, end, m) in enumerate(matches):
        parsed = parse_speaker(m)
        _demote_role_prefix(parsed)
        parsed["name"] = _apply_name_variant(parsed["name"])

        # Determine text body: from end of this attribution to start of next.
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        # Cut any running header that falls inside this speech — it is print
        # furniture, not something anybody said.
        if header_spans:
            parts, cur = [], end
            for hs, he in header_spans:
                if end <= hs and he <= next_start:
                    parts.append(text[cur:hs])
                    cur = he
            parts.append(text[cur:next_start])
            body = "".join(parts).strip()
        else:
            body = text[end:next_start].strip()
        # OCR occasionally doubles the terminator (";:"), leaving the second one
        # at the head of the speech text.
        body = re.sub(r"^[:;]+\s*", "", body)

        # For inline attributions the colon + leading text is part of the match's
        # end position only up to the ":" — the body text starts immediately after.
        # RE_INLINE_SPEAKER uses "(?=\S)" lookahead so `end` points just past ":".
        # The body is already correct as `text[end:next_start]`.

        # TOC filter: active until we see the first non-TOC chair speech.
        if toc_filter_active:
            if _is_toc_entry(parsed, body):
                continue
            toc_filter_active = False

        speeches.append({
            "speaker_raw": parsed["speaker_raw"],
            "name":        parsed["name"],
            "role":        parsed["role"],
            "land":        parsed["land"],
            "char_offset": start,
            "text":        body,
        })

    # Drop attributions with no speech body. These are almost always residual
    # attendance-roster lines ("Dr. Zander, Senator für Justiz … Hamburg") that
    # survived the preamble cut — a roster entry is not a speech. Filtering here
    # also renumbers sequences automatically, since the caller enumerates.
    speeches = [sp for sp in speeches if sp["text"].strip()]

    # Drop matches whose "name" is nothing but a chair-role word. These come from
    # the printed session-opening formula, where the role stands in the dative:
    #     "Die Sitzung wird … durch den Präsidenten, Ministerpräsident Dr. Ehard,
    #      eröffnet."
    # Pattern C reads that as name="Präsidenten" with the rest as a role, which
    # buries the real "Präsident Dr. EHARD:" that follows on the next line.
    speeches = [sp for sp in speeches if not _RE_ROLE_WORD_ONLY.match(sp["name"] or "")]

    return speeches


# ---------------------------------------------------------------------------
# Metadata lookup
# ---------------------------------------------------------------------------

def load_metadata(csv_path: Path) -> dict[str, dict]:
    """
    Load the metadata catalogue CSV and return a dict keyed by session number
    (zero-padded to 4 digits) → {date, date_iso, city, year, ...}.
    """
    result: dict[str, dict] = {}
    if not csv_path.exists():
        return result
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = row.get("session", "").zfill(4)
            result[key] = row
    return result


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "session_id",
    "session",
    "date",
    "date_iso",
    "sequence",
    "speaker_raw",
    "name",
    "role",
    "land",
    "char_offset",
    "text",
]


def write_speeches_csv(path: Path, session_id: str, session: str,
                       meta: dict, speeches: list[dict]) -> None:
    date     = meta.get("date", "")
    date_iso = meta.get("date_iso", "")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, sp in enumerate(speeches, start=1):
            writer.writerow({
                "session_id":  session_id,
                "session":     session,
                "date":        date,
                "date_iso":    date_iso,
                "sequence":    i,
                "speaker_raw": sp.get("speaker_raw", ""),
                "name":        sp.get("name", "") or "",
                "role":        sp.get("role", "") or "",
                "land":        sp.get("land", "") or "",
                "char_offset": sp.get("char_offset", ""),
                "text":        sp.get("text", ""),
            })


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
        metadata_csv=None,
        session_filter: set[str] | None = None,
        name_variants_csv=None) -> None:

    in_root = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)
    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    # OCR name-variant merges (optional; default docs/name_variants.csv next to src/)
    global _NAME_VARIANTS
    nv_path = Path(name_variants_csv) if name_variants_csv else (
        Path(__file__).resolve().parent.parent / "docs" / "name_variants.csv"
    )
    _NAME_VARIANTS = load_name_variants(nv_path)
    if _NAME_VARIANTS:
        print(f"Loaded {len(_NAME_VARIANTS)} OCR name-variant merge(s) from {nv_path.name}")
    else:
        print(f"  [INFO] No name-variant file at {nv_path}; names used exactly as parsed.")

    # Resolve metadata catalogue
    meta_path = Path(metadata_csv) if metadata_csv else (
        in_root / "metadata" / "metadata_catalogue.csv"
    )
    metadata = load_metadata(meta_path)
    if not metadata:
        print(f"  [WARN] No metadata catalogue found at {meta_path}; date fields will be empty.")

    log_path = in_root / "speakers.log"
    done: set[str] = set()
    if not force and log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    cleaned_files = sorted(
        p for p in in_root.rglob("*_cleaned.txt")
        if (in_root / "metadata") not in p.parents
    )

    print(f"Found {len(cleaned_files)} cleaned .txt file(s) under {in_root}")

    # Pre-pass: confirm which surnames really are speakers, so the bare-surname
    # pattern ("HELLWEGE:") has something to validate against. Built from EVERY
    # cleaned file, not just the ones this run writes, so a --session run and a
    # full run produce identical output.
    global _KNOWN_SURNAMES
    _KNOWN_SURNAMES = build_known_surnames(
        p.read_text(encoding="utf-8", errors="replace") for p in cleaned_files
    )
    print(f"Confirmed {len(_KNOWN_SURNAMES)} speaker surname(s) for bare-surname attributions")

    ok_count = skip_count = error_count = 0

    for cleaned_path in cleaned_files:
        # Extract session number from filename: "Session_NNNN_cleaned.txt"
        m = re.search(r"Session_(\d+)_cleaned", cleaned_path.stem)
        if not m:
            print(f"  [SKIP] Unrecognised filename: {cleaned_path.name}")
            skip_count += 1
            continue

        session = m.group(1)

        if session_filter is not None and session not in session_filter:
            skip_count += 1
            continue

        session_id = f"Session_{session}"
        out_path   = cleaned_path.with_name(f"Session_{session}_speakers.csv")

        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {cleaned_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {cleaned_path.name} ...", end=" ", flush=True)

        try:
            text = cleaned_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            speeches = split_speeches(text)
        except Exception as e:
            print(f"SPLIT ERROR\n  -> {e}")
            error_count += 1
            continue

        meta = metadata.get(session, {})

        try:
            write_speeches_csv(out_path, session_id, session, meta, speeches)
            _append_log(log_path, str(out_path))
            print(f"OK -> {len(speeches)} speech(es) -> {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bundesrat speaker identifier"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing _speakers.csv files.",
    )
    parser.add_argument(
        "--preprocessed-dir",
        default=None,
        metavar="PATH",
        help=f"Root preprocessed folder. Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--metadata-csv",
        default=None,
        metavar="PATH",
        help="Path to metadata_catalogue.csv. Default: auto-resolved.",
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="N[,N,...]",
        help="Comma-separated session numbers to process (e.g. '941,1000').",
    )
    parser.add_argument(
        "--name-variants-csv",
        default=None,
        metavar="PATH",
        help=("CSV of reviewed OCR name merges (columns: variant,canonical). "
              "Default: docs/name_variants.csv. Omit the file to disable merging."),
    )
    args = parser.parse_args()

    session_filter = (
        {s.strip().zfill(4) for s in args.session.split(",")}
        if args.session else None
    )

    print("Bundesrat speaker identifier")
    print(f"Input/Output: {args.preprocessed_dir or _DEFAULT_PREPROCESSED}")
    if session_filter:
        print(f"Session filter: {sorted(session_filter)}")
    print()

    run(
        force=args.force,
        preprocessed_dir=args.preprocessed_dir,
        metadata_csv=args.metadata_csv,
        session_filter=session_filter,
        name_variants_csv=args.name_variants_csv,
    )
