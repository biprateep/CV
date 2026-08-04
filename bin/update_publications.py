#!/usr/bin/env python3
r"""Regenerate pub_contributing_author.tex from NASA ADS.

Queries ADS for every publication matching the ORCID below, drops the ones
where Biprateep Dey is 1st or 2nd author (those live in the hand-maintained
``pub_lead_author.tex``), and writes the remainder as ``\item`` lines in the
CV's existing format, newest first:

    \item Adame, A. G., et al.[including \textbf{Dey, B.}], 2025, Journal, 1, 2, \\ \textit{Title}.

Also regenerates the publication and presentation summary lines in
``main.tex`` from the ``\item`` counts of the corresponding .tex files, so the
numbers cannot drift out of sync with the lists.

``pub_lead_author.tex`` is never touched -- it carries manual annotations
(``in review``, the ``Dey, B.*`` corresponding-author asterisk, custom venue
names) that ADS cannot reproduce.

Requires the environment variable ADS_API_TOKEN (free at
https://ui.adsabs.harvard.edu/user/settings/token).

Usage:
    python bin/update_publications.py            # write the files
    python bin/update_publications.py --dry-run  # print a diff, write nothing
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ORCID = "0000-0002-5665-7912"
# matches "Dey, B." / "Dey, Biprateep" but not "Dey, Arjun" or "Dutta Dey, B."
AUTHOR_REGEX = re.compile(r"^dey,\s*b", re.IGNORECASE)

# Dey at position <= this counts as lead/significant -> excluded from this file.
LEAD_MAX_POSITION = 2
# Show every author when there are at most this many; otherwise "First, A., et al."
MAX_AUTHORS_SHOWN = 3
# Refuse to write if the new list shrinks below this fraction of the old one.
SHRINK_GUARD = 0.9

# Bibcodes to always treat as lead (keep OUT of the contributing list),
# regardless of author position. Mirror of the website script's FORCE_LEAD.
FORCE_LEAD: set = set()
# Bibcodes to drop from the CV entirely (duplicates, errata, withdrawn).
EXCLUDE: set = set()

API = "https://api.adsabs.harvard.edu/v1"
FIELDS = "bibcode,author,year,pub,volume,page,title"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUTPUT = os.path.join(REPO, "pub_contributing_author.tex")
LEAD_TEX = os.path.join(REPO, "pub_lead_author.tex")
INVITED_TEX = os.path.join(REPO, "invited_talks.tex")
CONTRIBUTED_TEX = os.path.join(REPO, "contributed_talks.tex")
MAIN_TEX = os.path.join(REPO, "main.tex")

HEADER = r"""%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% GENERATED FILE -- DO NOT EDIT BY HAND (not even in Overleaf).
% Rebuilt from NASA ADS by bin/update_publications.py.
% Edits here are overwritten by the update-publications GitHub Action.
% To suppress or reclassify an entry, use EXCLUDE / FORCE_LEAD in that script.
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

"""

# AAS/ADS LaTeX journal macros -> full names (the CV never abbreviates).
JOURNAL_MACROS = {
    "\\aj": "The Astronomical Journal",
    "\\apj": "The Astrophysical Journal",
    "\\apjl": "The Astrophysical Journal Letters",
    "\\apjs": "The Astrophysical Journal Supplement Series",
    "\\mnras": "Monthly Notices of the Royal Astronomical Society",
    "\\aap": "Astronomy & Astrophysics",
    "\\jcap": "Journal of Cosmology and Astroparticle Physics",
    "\\prd": "Physical Review D",
    "\\prl": "Physical Review Letters",
    "\\pasp": "Publications of the Astronomical Society of the Pacific",
    "\\nat": "Nature",
    "\\natas": "Nature Astronomy",
}


# --------------------------------------------------------------------------
# ADS access
# --------------------------------------------------------------------------
def api_request(url, data=None, token=None):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_papers(token):
    """Every ORCID match, newest first."""
    docs, start, rows = [], 0, 200
    while True:
        params = urllib.parse.urlencode(
            {
                "q": f"orcid:{ORCID}",
                "fl": FIELDS,
                "rows": rows,
                "start": start,
                "sort": "date desc, bibcode desc",
            }
        )
        response = api_request(f"{API}/search/query?{params}", token=token)["response"]
        docs.extend(response["docs"])
        if len(docs) >= response["numFound"]:
            return docs
        start += rows


def fetch_bibtex_titles(bibcodes, token):
    r"""Map bibcode -> LaTeX title, taken from the ADS BibTeX export.

    The search API's ``title`` field is the raw publisher string, which for APS
    journals is MathML ("<inline-formula><mml:math>...Ly&alpha;...") and for
    others contains HTML entities. The BibTeX export is already LaTeX-ified
    ("Ly$\alpha$"), which is what the CV wants.
    """
    titles = {}
    for i in range(0, len(bibcodes), 100):  # ADS caps export batch size
        chunk = bibcodes[i : i + 100]
        export = api_request(
            f"{API}/export/bibtex", data={"bibcode": chunk}, token=token
        )["export"]
        titles.update(parse_bibtex_titles(export))
    return titles


BIBTEX_ENTRY_RE = re.compile(r"@\w+\{([^,\s]+),")
BIBTEX_TITLE_RE = re.compile(r'title\s*=\s*"?\{(.*?)\}"?,\s*\n', re.DOTALL)


def parse_bibtex_titles(bibtex):
    """Pull {bibcode: title} out of an ADS BibTeX export blob."""
    titles = {}
    for chunk in re.split(r"(?=^@)", bibtex, flags=re.MULTILINE):
        key = BIBTEX_ENTRY_RE.match(chunk.strip())
        title = BIBTEX_TITLE_RE.search(chunk)
        if key and title:
            # ADS hard-wraps long titles; collapse to a single line.
            titles[key.group(1)] = " ".join(title.group(1).split())
    return titles


# --------------------------------------------------------------------------
# Text cleaning
# --------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
MATH_SPLIT_RE = re.compile(r"(\$[^$]*\$)")
# & # % _ break LaTeX in text mode. $ \ { } ^ are left alone: ADS BibTeX uses
# them deliberately for math and accents.
NEEDS_ESCAPE_RE = re.compile(r"(?<!\\)([&#%_])")

# Fallback only: publisher strings occasionally carry bare unicode maths that
# would otherwise reach LaTeX as-is. Titles taken from the BibTeX export are
# already LaTeX-ified and never need this.
UNICODE_MATH = {
    "α": r"$\alpha$", "β": r"$\beta$", "γ": r"$\gamma$", "δ": r"$\delta$",
    "λ": r"$\lambda$", "μ": r"$\mu$", "ν": r"$\nu$", "σ": r"$\sigma$",
    "χ": r"$\chi$", "Ω": r"$\Omega$", "Λ": r"$\Lambda$", "π": r"$\pi$",
    "≈": r"$\approx$", "≤": r"$\leq$", "≥": r"$\geq$", "×": r"$\times$",
    "∼": r"$\sim$", "−": "-",
}


def latex_escape(text):
    r"""Make an ADS string safe for LaTeX text mode, leaving math intact.

    Unescapes HTML entities first ("1 &lt; z" -> "1 < z", which removes the
    stray ``&``), then escapes the remaining special characters outside of
    ``$...$`` spans. The negative lookbehind keeps the function idempotent and
    stops it from mangling sequences ADS already escaped.
    """
    if not text:
        return ""
    text = TAG_RE.sub("", text)  # strip any MathML/HTML markup
    text = html.unescape(text)
    for char, macro in UNICODE_MATH.items():
        text = text.replace(char, macro)
    # Merge "$\alpha$$\beta$" produced by adjacent substitutions.
    text = text.replace("$$", "")
    parts = MATH_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:  # even indices are outside $...$
            parts[i] = NEEDS_ESCAPE_RE.sub(r"\\\1", part)
    return " ".join("".join(parts).split())


def abbreviate_name(name):
    """"Moore, Samuel G." -> "Moore, S. G."; leave already-short names alone.

    The search API returns full given names, whereas the CV (and the old ADS
    export template) uses initials.
    """
    if "," not in name:
        return name
    surname, given = name.split(",", 1)
    initials = []
    for token in given.replace(".", " ").split():
        # keep hyphenated given names hyphenated: "Jean-Paul" -> "J.-P."
        initials.append("-".join(p[0].upper() + "." for p in token.split("-") if p))
    return f"{surname.strip()}, {' '.join(initials)}" if initials else surname.strip()


def expand_journal(name):
    if not name:
        return ""
    return JOURNAL_MACROS.get(name.strip().lower(), name)


# --------------------------------------------------------------------------
# Classification and formatting
# --------------------------------------------------------------------------
def is_lead(doc):
    if doc["bibcode"] in FORCE_LEAD:
        return True
    return any(
        AUTHOR_REGEX.match(name)
        for name in doc.get("author", [])[:LEAD_MAX_POSITION]
    )


def format_authors(authors):
    r"""ADS %3G behaviour: list all authors up to MAX_AUTHORS_SHOWN, else et al.

    Appends ``[including \textbf{Dey, B.}]`` unless Dey is already visible in
    the printed author list (the old ADS export template appended it
    unconditionally, producing "Dutta, S., Khandai, N., Dey, B.[including
    \textbf{Dey, B.}]").
    """
    if not authors:
        return r"[including \textbf{Dey, B.}]"
    if len(authors) <= MAX_AUTHORS_SHOWN:
        shown = [abbreviate_name(a) for a in authors]
    else:
        shown = [abbreviate_name(authors[0]), "et al."]
    text = ", ".join(shown)
    if any(AUTHOR_REGEX.match(name) for name in shown):
        return text
    return text + r"[including \textbf{Dey, B.}]"


def first(value):
    """ADS returns page/title as single-element lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def format_entry(doc, titles=None):
    r"""One ``\item`` block matching the CV's established layout."""
    titles = titles or {}
    authors = format_authors(doc.get("author", []))
    title = titles.get(doc["bibcode"]) or first(doc.get("title")) or ""
    title = latex_escape(title).rstrip(".")
    # Omit empty journal/volume/page rather than emitting stray commas.
    bits = [
        latex_escape(str(x).strip())
        for x in (
            doc.get("year"),
            expand_journal(doc.get("pub")),
            doc.get("volume"),
            first(doc.get("page")),
        )
        if x not in (None, "", [])
    ]
    meta = ", ".join(b for b in bits if b)
    prefix = f"{authors}, {meta}" if meta else authors
    return f"\\item {prefix}, \\\\ \\textit{{{title}}}. \n \n"


def build_tex(docs, titles=None):
    return HEADER + "".join(format_entry(d, titles) for d in docs)


# --------------------------------------------------------------------------
# main.tex summary line synchronisation
# --------------------------------------------------------------------------
ITEM_RE = re.compile(r"^\s*\\item\b")

# Matches the publication summary phrase in either the old split form
# ("6 lead author, 3 significant contributing author, and 85 contributing
# author") or the generated form, so repeat runs are idempotent.
SUMMARY_RE = re.compile(
    r"(?:\d+\s+lead\s+author,\s*\d+\s+significant\s+contributing\s+author"
    r"|\d+\s+lead/significant\s+contributing\s+author)"
    r",\s*and\s+\d+\s+contributing\s+author"
)

# Matches "27 invited, 21 contributed" and "27 Invited and 21 Contributed",
# preserving the surrounding wording and capitalisation of each.
TALKS_RE = re.compile(
    r"(\d+)(\s+invited\s*(?:,|and)\s+)(\d+)(\s+contributed)", re.IGNORECASE
)


def count_items(path):
    """Number of uncommented \\item entries in a .tex list file."""
    with open(path) as f:
        return sum(
            1
            for line in f
            if ITEM_RE.match(line) and not line.lstrip().startswith("%")
        )


def update_counts(text, n_lead, n_contributing, n_invited, n_contributed):
    """Regenerate the summary lines outside of commented-out text.

    The lead/significant distinction is not derivable from ADS -- and the two
    publication summary lines historically disagreed about it -- so both
    collapse into a single "N lead/significant contributing author" figure
    taken from the length of pub_lead_author.tex.

    Returns (new_text, publication_hits, presentation_hits).
    """
    summary = (
        f"{n_lead} lead/significant contributing author, "
        f"and {n_contributing} contributing author"
    )
    out, pub_hits, talk_hits = [], 0, 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("%"):
            line, hits = SUMMARY_RE.subn(summary, line)
            pub_hits += hits
            line, hits = TALKS_RE.subn(
                lambda m: f"{n_invited}{m.group(2)}{n_contributed}{m.group(4)}", line
            )
            talk_hits += hits
        out.append(line)
    return "".join(out), pub_hits, talk_hits


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print a unified diff of what would change and exit",
    )
    args = parser.parse_args()

    token = os.environ.get("ADS_API_TOKEN")
    if not token:
        sys.exit("Error: set the ADS_API_TOKEN environment variable.")

    docs = [d for d in fetch_papers(token) if d["bibcode"] not in EXCLUDE]
    if not docs:
        sys.exit("Error: ADS returned no publications; refusing to overwrite.")

    lead = [d for d in docs if is_lead(d)]
    contributing = [d for d in docs if not is_lead(d)]

    old = ""
    if os.path.exists(OUTPUT):
        with open(OUTPUT) as f:
            old = f.read()
    old_n = old.count("\n\\item ") + old.startswith("\\item ")
    if old_n and len(contributing) < SHRINK_GUARD * old_n:
        sys.exit(
            f"Error: contributing list would shrink {old_n} -> {len(contributing)}. "
            "ADS may be incomplete; refusing to write. Re-run, or lower SHRINK_GUARD "
            "if the drop is intentional."
        )

    titles = fetch_bibtex_titles([d["bibcode"] for d in contributing], token)
    missing = [d["bibcode"] for d in contributing if d["bibcode"] not in titles]
    if missing:
        print(f"Note: no BibTeX title for {len(missing)} entry(ies); using the "
              f"raw ADS title instead: {', '.join(missing[:5])}")

    new = build_tex(contributing, titles)

    print(f"ADS returned {len(docs)} publications.")
    print(f"  lead/significant (hand-maintained, untouched): {len(lead)}")
    print(f"  contributing (generated here):                 {len(contributing)}")
    print("\nCross-check pub_lead_author.tex against these lead bibcodes:")
    for d in lead:
        print(f"  {d['bibcode']}  {(first(d.get('title')) or '')[:70]}")

    # The .tex list files are the source of truth for these counts:
    # pub_lead_author.tex holds entries ADS does not have yet (in review), and
    # the talk lists have no ADS equivalent at all.
    n_lead = count_items(LEAD_TEX)
    n_invited = count_items(INVITED_TEX)
    n_contributed = count_items(CONTRIBUTED_TEX)
    print(
        f"\nSummary lines: {n_lead} lead/significant, {len(contributing)} contributing; "
        f"{n_invited} invited, {n_contributed} contributed talks."
    )

    with open(MAIN_TEX) as f:
        main_text = f.read()
    new_main, pub_hits, talk_hits = update_counts(
        main_text, n_lead, len(contributing), n_invited, n_contributed
    )
    if not pub_hits or not talk_hits:
        sys.exit(
            f"Error: matched {pub_hits} publication and {talk_hits} presentation "
            "summary line(s) in main.tex; expected at least one of each. The "
            "wording probably changed -- fix SUMMARY_RE / TALKS_RE in this script."
        )

    if args.dry_run:
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="pub_contributing_author.tex (current)",
                tofile="pub_contributing_author.tex (new)",
            )
        )
        sys.stdout.writelines(
            difflib.unified_diff(
                main_text.splitlines(keepends=True),
                new_main.splitlines(keepends=True),
                fromfile="main.tex (current)",
                tofile="main.tex (new)",
            )
        )
        print("\n[dry run] nothing written.")
        return

    with open(OUTPUT, "w") as f:
        f.write(new)
    print(f"\nWrote {OUTPUT}")

    if new_main != main_text:
        with open(MAIN_TEX, "w") as f:
            f.write(new_main)
        print(f"Updated {pub_hits + talk_hits} summary figure(s) in {MAIN_TEX}")


if __name__ == "__main__":
    main()
