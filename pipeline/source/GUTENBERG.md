# Collect public-domain stories

Run this command from the repository root with Python 3.9+ and Git:

```sh
python -m pipeline.source.gutenberg > gutenberg.tmp && mv gutenberg.tmp gutenberg.jsonl
```

Or use `from pipeline.source.gutenberg import records`. Iterate `records()` and pass each dictionary to the store's `upsert(db, record)` within a transaction. Do not use Reddit normalization. Pass a directory to `records(cache=...)` or use `--cache DIR` to select an edition cache. Initial collection needs GitHub access. Later runs use the pinned Git objects without network requests. No API key, model, scraping service or package install is needed.

**Acquisition.** [Project Gutenberg's robot policy](https://www.gutenberg.org/policy/robot_access.html) reserves the website for people, apart from its documented harvest, mirror and catalog channels. Its main site's robots.txt returned HTTP 503 during collection. This adapter never downloads books from that site, even as a fallback. It uses Git's native fetch protocol against four public [GITenberg](https://github.com/GITenberg) snapshot repositories, not a web crawler. Repository names and immutable revision IDs are in `BOOKS`. Each record's URL points readers to the Gutenberg catalog entry, not the Git mirror.

**Rights.** For each pinned edition, require a matching Gutenberg RDF ebook ID and its explicit “Public domain in the USA.” rights declaration. Check the ebook ID, English notice and start/end markers in the text itself. Do not infer rights from the Git host, publication age or a missing copyright notice. The [permission policy](https://www.gutenberg.org/policy/permission.html) covers commercial adaptations in the US; it does not clear release in all countries. Use the underlying prose, not Project Gutenberg branding, for commercial adaptations. Read [the trademark license](https://www.gutenberg.org/policy/license.html) before distributing branded editions. The JSONL is an internal sourcing dataset; its provenance fields are not a credit roll.

**Selection.** The adapter parses the actual numbered entries in Pu Songling's *Strange Stories from a Chinese Studio* (Herbert A. Giles translation), the tale headings in Lafcadio Hearn's *Kwaidan*, and the story headings in the Grimm and Aesop collections. It keeps prose, dialogue, verse and morals. It excludes the table of contents, editor biographies, notes, appendices, insect studies and license boilerplate. It never truncates a long story to meet the 100–3,000 word window. IDs are the edition number plus the story ordinal in that pinned edition. `retrieved_at` is when the collector read the snapshot, not a historical publication date. `source_lane` is a discovery hint, not a model score or a child-suitability verdict. Read the shortlisted stories for violence, dated stereotypes and other sensitive material before adaptation.

**Updating.** Review the replacement revision's metadata, ebook notice, translation, table of contents and story boundaries before changing a pin. Check the expected section count and spot-check the first, middle and last eligible records against the edition. Recheck the story IDs if the order changed. Use `python -m pytest tests/test_source_gutenberg.py` for the offline tests.
