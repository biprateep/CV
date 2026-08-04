#!/usr/bin/env python3
r"""Regenerate pub_contributing_author.tex from NASA ADS.

Queries ADS for every publication matching the ORCID below, drops the ones
where Biprateep Dey is 1st or 2nd author (those live in the hand-maintained
``pub_lead_author.tex``), and writes the remainder as ``\item`` lines in the
CV's existing format, newest first:

    \item Adame, A. G., et al.[including \textbf{Dey, B.}], 2025, Journal, 1, 2, \\ \textit{Title}.

Also rewrites the "N contributing author" counts in ``main.tex``.

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
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ORCID = "0000-0002-5665-7912"
# matches "Dey, B." / "Dey, Biprateep" but not "Dey, A." or "Dutta Dey, B."
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
MAIN_TEX = os.path.join(REPO, "main.tex")

HEADER = r"""%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% GENERATED FILE -- DO NOT EDIT BY HAND (not even in Overleaf).
% Rebuilt from NASA ADS by bin/update_publications.py.
% Edits here are overwritten by the update-publications GitHub Action.
% To suppress or reclassify an entry, use EXCLUDE / FORCE_LEAD in that script.
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

"""


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
        shown = list(authors)
    else:
        shown = [authors[0], "et al."]
    text = ", ".join(shown)
    if any(AUTHOR_REGEX.match(name) for name in shown):
        return text
    return text + r"[including \textbf{Dey, B.}]"


def first(value):
    """ADS returns page/title as single-element lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def format_entry(doc):
    r"""One ``\item`` block matching the CV's established layout."""
    authors = format_authors(doc.get("author", []))
    title = (first(doc.get("title")) or "").strip().rstrip(".")
    # Omit empty journal/volume/page rather than emitting stray commas.
    bits = [
        str(x).strip()
        for x in (doc.get("year"), doc.get("pub"), doc.get("volume"), first(doc.get("page")))
        if x not in (None, "", [])
    ]
    meta = ", ".join(bits)
    prefix = f"{authors}, {meta}" if meta else authors
    return f"\\item {prefix}, \\\\ \\textit{{{title}}}. \n \n"


def build_tex(docs):
    return HEADER + "".join(format_entry(d) for d in docs)


# --------------------------------------------------------------------------
# main.tex count synchronisation
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


def count_items(path):
    """Number of uncommented \\item entries in a publication .tex file."""
    with open(path) as f:
        return sum(
            1
            for line in f
            if ITEM_RE.match(line) and not line.lstrip().startswith("%")
        )


def update_counts(text, n_lead, n_contributing):
    """Regenerate the publication summary lines outside of commented-out text.

    The lead/significant distinction is not derivable from ADS -- and the two
    summary lines historically disagreed about it -- so both counts collapse
    into a single "N lead/significant contributing author" figure taken from
    the length of pub_lead_author.tex.
    """
    replacement = (
        f"{n_lead} lead/significant contributing author, "
        f"and {n_contributing} contributing author"
    )
    out, changed = [], 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("%"):
            line, hits = SUMMARY_RE.subn(replacement.replace("\\", "\\\\"), line)
            changed += hits
        out.append(line)
    return "".join(out), changed


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

    new = build_tex(contributing)

    print(f"ADS returned {len(docs)} publications.")
    print(f"  lead/significant (hand-maintained, untouched): {len(lead)}")
    print(f"  contributing (generated here):                 {len(contributing)}")
    print("\nCross-check pub_lead_author.tex against these lead bibcodes:")
    for d in lead:
        print(f"  {d['bibcode']}  {(first(d.get('title')) or '')[:70]}")

    # pub_lead_author.tex is the source of truth for the lead count: it carries
    # entries ADS does not have yet, such as papers in review.
    n_lead = count_items(LEAD_TEX)
    print(f"\npub_lead_author.tex holds {n_lead} entries; summary line will say "
          f"{n_lead} lead/significant, {len(contributing)} contributing.")

    with open(MAIN_TEX) as f:
        main_text = f.read()
    new_main, hits = update_counts(main_text, n_lead, len(contributing))
    if not hits:
        sys.exit(
            "Error: found no publication summary line in main.tex to update. "
            "Its wording probably changed; fix SUMMARY_RE in this script."
        )

    if args.dry_run:
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="pub_contributing_author.tex (current)",
            tofile="pub_contributing_author.tex (new)",
        )
        sys.stdout.writelines(diff)
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
        print(f"Updated {hits} count(s) in {MAIN_TEX}")


if __name__ == "__main__":
    main()
