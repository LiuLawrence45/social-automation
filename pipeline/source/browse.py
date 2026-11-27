"""Export story metadata as a searchable local HTML page or a streaming CSV."""
import csv
import json
import unicodedata
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from .ranking import reject


ROUTES = {"gutenberg": "Project Gutenberg", "scp": "SCP Wiki", "creepypasta": "Creepypasta Wiki"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Find a story to adapt</title>
<style>
:root { color-scheme:light dark; font:16px/1.55 system-ui,sans-serif; }
body { margin:0; } main { max-width:76rem; margin:auto; padding:clamp(1rem,4vw,4rem); }
h1 { font-size:clamp(2rem,5vw,3.5rem); line-height:1.05; letter-spacing:-.055em; font-weight:550; margin:.5em 0; }
p { max-width:46rem; } label, th { font-weight:550; }
input { display:block; font:inherit; width:min(100%,35rem); padding:.65rem .85rem; margin:.4rem 0;
        box-sizing:border-box; border-radius:.4rem; border:1px solid GrayText; }
.table { overflow-x:auto; } table { width:100%; border-collapse:collapse; text-align:left; }
th, td { padding:.75rem; vertical-align:top; border-bottom:1px solid color-mix(in srgb,CanvasText 20%,Canvas); }
td:first-child { min-width:12rem; } small { display:block; color:GrayText; } [hidden] { display:none; }
</style></head><body><main>
<header><h1>Find a story to adapt</h1>
<p>Private, internal demo only. Stories appear in descending source-score order.
Search by title, author, source, license, or local filter flag. Links open the original source.</p></header>
<label for="query">Search the catalog</label><input id="query" type="search" placeholder="Try an author or “public domain”">
<p id="count" role="status"></p>
<div class="table"><table><thead><tr><th>Story</th><th>Source score</th><th>Words</th><th>Local filter</th>
<th>Source</th><th>Rights and license</th></tr></thead><tbody>
"""

END = r"""</tbody></table></div></main>
<script>
const rows = Array.from(document.querySelectorAll('tbody tr'));
const normalize = text => text.toLowerCase().replace(/[-_]+/g, ' ');
const index = rows.map(row => normalize(Array.from(row.cells, c => c.innerText).join(' ')));
const query = document.querySelector('#query');
function filter() {
  const words = normalize(query.value).trim().split(/\s+/);
  let count = 0;
  rows.forEach((row, i) => {
    row.hidden = !words.every(word => index[i].includes(word));
    if (!row.hidden) count++;
  });
  document.querySelector('#count').textContent = count + ' of ' + rows.length + ' stories shown.';
}
query.addEventListener('input', filter);
filter();
</script></body></html>
"""


def _safe_url(raw):
    if not isinstance(raw, str) or any(ord(c) < 33 or c == "\\" for c in raw):
        return ""
    try:
        url = urlsplit(raw)
        if url.scheme not in ("https", "http") or not url.hostname or url.username is not None:
            return ""
    except ValueError:
        return ""
    return raw


def _csv_cell(value):
    if not isinstance(value, str):
        return value
    first = next((ch for ch in unicodedata.normalize("NFKC", value)
                  if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc")), None)
    if first in ("=", "+", "-", "@") or value.startswith(("\t", "\r", "\n", "\ufeff")):
        return "'" + value
    return value


def export_index(db, path):
    """Write one metadata row per stored record, returning the row count."""
    def show(value, fallback="Unknown"):
        return escape(str(value)) if value not in (None, "") else fallback

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output) if path.suffix.lower() == ".csv" else None
        if writer:
            writer.writerow(["id", "title", "author", "subreddit", "words", "score", "flags", "url"])
        else:
            output.write(PAGE)
        for (data,) in db.execute("select record from posts order by popularity desc, id"):
            rec = json.loads(data)
            url = _safe_url(rec.get("url"))
            flags = reject(rec) or "Candidate"
            count += 1
            if writer:
                values = (rec.get(k) for k in ("id", "title", "author", "subreddit", "word_count", "score"))
                writer.writerow(_csv_cell(v) for v in (*values, flags, url))
                continue
            title = show(rec.get("title"), "Untitled story")
            if url:
                title = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            source = ("Reddit: r/" + show(rec["subreddit"]) if rec.get("subreddit")
                      else show(ROUTES.get(rec.get("route"), rec.get("route"))))
            rights = show(rec.get("rights")).replace("-", " ").capitalize()
            license_name = ("Public domain (US)" if rec.get("license") == "public-domain-us"
                            else show(rec.get("license")))
            output.write(
                f'<tr><td>{title}<small>{show(rec.get("author"), "Unknown author")}</small></td>'
                f'<td>{show(rec.get("score"), "Not available")}</td>'
                f'<td>{show(rec.get("word_count"))}</td><td>{show(flags)}</td>'
                f'<td>{source}<small>{show(rec.get("id"))}</small></td>'
                f'<td>{rights}<small>{license_name}</small></td></tr>\n'
            )
        if not writer:
            output.write(END)
    return count
