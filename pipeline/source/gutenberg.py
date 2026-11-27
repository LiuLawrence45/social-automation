"""Collect complete stories from a small, reviewed set of US public-domain editions."""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


BOOKS = {
    43629: ("Strange-Stories-from-a-Chinese-Studio--Volumes-I-and-II-_43629",
            "e1510ce3a41c1371c99470e5f9586676f1397826", "Pu Songling",
            "Strange Stories from a Chinese Studio, translated by Herbert A. Giles"),
    1210: ("Kwaidan--Stories-and-Studies-of-Strange-Things_1210",
           "dacf8032fdf80099627a950f7187142961e9066a", "Lafcadio Hearn", "Kwaidan"),
    2591: ("Grimms-Fairy-Tales_2591", "9991a30953853be9b40751408c3ad0d3af314fb7",
           "Jacob and Wilhelm Grimm", "Grimms’ Fairy Tales, based on translations by Edgar Taylor and Marian Edwardes"),
    21: ("Aesop-s-Fables--13-Translated-by-George-Fyler-Townsend_21",
         "f5217ed19e50e730395c5f14c5a01f7c194ad1ef", "Aesop",
         "Aesop’s Fables, translated by George Fyler Townsend"),
}
DEFAULT_CACHE = Path.home() / ".cache" / "social-automation" / "gitenberg"
_SPECTRAL = {1, 2, 3, 6, 9, 12, 13, 14, 16, 17, 18, 23, 25, 32, 33, 35, 36,
             45, 48, 51, 52, 54, 56, 57, 61, 65, 68, 71, 72, 73, 75, 76, 77, 78,
             79, 84, 85, 86, 87, 89, 90, 93, 95, 96, 101, 106, 107, 108,
             109, 110, 112, 116, 118, 121, 129, 130, 131, 137, 148, 150}


def verify_edition(number, rdf, source):
    ebook = ET.fromstring(rdf).find("{http://www.gutenberg.org/2009/pgterms/}ebook")
    if (ebook is None or ebook.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") != f"ebooks/{number}"
            or ebook.findtext("{http://purl.org/dc/terms/}rights") != "Public domain in the USA."):
        raise ValueError(f"Missing US public-domain declaration for edition {number}")
    header, start, _ = source.partition("*** START OF THIS PROJECT GUTENBERG EBOOK")
    if (not start or f"[EBook #{number}]" not in header or "Language: English" not in header
            or re.search(r"copyright holder|posted with permission", header, re.I)
            or "*** END OF THIS PROJECT GUTENBERG EBOOK" not in source):
        raise ValueError(f"Unexpected notice for edition {number}")


def _text(node):
    if node.get("class") in {"pagenum", "fnanchor", "legacy-indent"}:
        return ""
    value = (node.text or "") + "".join(_text(child) + (child.tail or "") for child in node)
    return value + (" " if node.tag in {"br", "div", "td", "tr"} else "")


def _tidy(text):
    return " ".join(text.split())


def sections(number, source):
    if number == 1210:
        contents = source.split("TABLE OF CONTENTS\n", 1)[1].split("  INSECT STUDIES", 1)[0]
        titles = [line.strip() for line in contents.splitlines() if line.strip()]
        body = source.split("\nKWAIDAN\n", 1)[1].split("\nINSECT STUDIES\n", 1)[0]
        for index, title in enumerate(titles, 1):
            _, body = body.split(f"\n{title}\n", 1)
            entry = body.split(f"\n{titles[index]}\n", 1)[0] if index < len(titles) else body
            entry = re.sub(r" ?(?:\[\d+\]|\(\d+\))", "", entry)
            yield index, title.title(), "\n\n".join(_tidy(p) for p in re.split(r"\n\s*\n", entry.strip()))
        return
    source = re.sub(r"&([a-zA-Z]+);", lambda m: m[0] if m[1] in
                    {"amp", "lt", "gt", "quot", "apos"} else html.unescape(m[0]), source)
    root = ET.fromstring(source)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if number == 43629:
        for group in root.iter("div"):
            title = group.find("./h2/a")
            if title is None or not re.fullmatch(r"chapter-\d+", title.get("id", "")):
                continue
            index = int(title.get("id").split("-")[1])
            name = re.sub(r"^[IVXLCDM]+\.\s*", "", _tidy(_text(title))).rstrip(".")
            siblings = list(group)
            paragraphs = [_tidy(_text(n)) for n in siblings[siblings.index(group.find("h2")) + 1:]]
            yield index, name, "\n\n".join(filter(None, paragraphs))
        return
    first = {21: "The Wolf And The Lamb", 2591: "THE GOLDEN BIRD"}[number]
    title, paragraphs, index = "", [], 0
    for node in root.find("body"):
        if node.tag == "h2":
            if title:
                yield index, title, "\n\n".join(paragraphs)
            name = _tidy(_text(node))
            if name == "LIFE OF AESOP":
                return
            if index or name == first:
                index += 1
                title, paragraphs = name, []
        elif title and node.get("class") == "mynote":
            yield index, title, "\n\n".join(paragraphs)
            return
        elif title:
            paragraph = _tidy(_text(node))
            if paragraph:
                paragraphs.append(paragraph)
    raise ValueError(f"Missing reviewed end boundary in edition {number}")


def records(cache=DEFAULT_CACHE):
    """Yield canonical records. Fetch each pinned Git snapshot once; reruns use the cache."""
    for number, (repo, rev, author, edition) in BOOKS.items():
        path = Path(cache).expanduser() / str(number)
        if not path.exists():
            path.mkdir(parents=True)
        if not (path / ".git").is_dir():
            subprocess.run(["git", "init", "-q", str(path)], check=True)
        git = ["git", "-C", str(path)]
        if subprocess.run(git + ["cat-file", "-e", rev], stderr=subprocess.DEVNULL).returncode:
            subprocess.run(git + ["fetch", "-q", "--depth=1", "https://github.com/GITenberg/" + repo, rev],
                           check=True, timeout=120)
        def read(name):
            return subprocess.check_output(git + ["show", f"{rev}:{name}"]).decode("utf-8-sig").replace("\r\n", "\n")
        source = read(f"{number}.txt" if number == 1210 else f"{number}-h/{number}-h.htm")
        verify_edition(number, read(f"pg{number}.rdf"), source)
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entries = list(sections(number, source))
        if len(entries) != {43629: 164, 1210: 17, 2591: 62, 21: 313}[number]:
            raise ValueError(f"Incomplete anthology {number}: {len(entries)} sections")
        for index, title, text in entries:
            words = len(text.split())
            if (not 100 <= words <= 3000 or (number == 43629 and index not in _SPECTRAL)
                    or (number == 1210 and index >= 16)):
                continue
            yield dict(id=f"gutenberg:{number}:{index}", subreddit=None, title=title, author=author,
                       url=f"https://www.gutenberg.org/ebooks/{number}", created_utc=None,
                       retrieved_at=retrieved_at, route="gutenberg", score=None, num_comments=None,
                       flair=None, word_count=words, text=text, over_18=False, removed=False,
                       scores=None, brief=None, license="Public domain in the USA",
                       license_url="https://www.gutenberg.org/policy/permission.html",
                       attribution=f"{title} — {author}, {edition}.",
                       rights="adaptation-permitted", permission=None,
                       obligations=["US public domain only; check rights in each other jurisdiction.",
                                    "Do not trade on the Project Gutenberg name; use unbranded adaptations."],
                       source_lane="scary" if number in {1210, 43629} else "fable")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    for record in records(parser.parse_args().cache):
        print(json.dumps(record, ensure_ascii=False))
