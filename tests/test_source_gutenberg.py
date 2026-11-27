import pytest

from pipeline.source.gutenberg import sections, verify_edition


def test_anthology_separates_story_text_from_metadata():
    xml = """<html><body><div><p>Volume one</p>
        <h2><a id="chapter-4"><abbr>IV.</abbr><br/>THE FOX.</a></h2>
        <p>A <i>fox</i><a class="fnanchor">[2]</a> ran.</p>
        <div class="poetry"><div>First line.</div><div>Last line.</div></div>
        </div><div><h2><a id="appendix">A note.</a></h2><p>Editorial.</p></div></body></html>"""
    assert list(sections(43629, xml)) == [(4, "THE FOX", "A fox ran.\n\nFirst line. Last line.")]


@pytest.mark.parametrize("number,first,end", [
    (21, "The Wolf And The Lamb", "<h2>LIFE OF AESOP</h2>"),
    (2591, "THE GOLDEN BIRD", "<div class='mynote'><p>Biography</p></div>"),
])
def test_fables_keep_moral_and_stop_before_end_matter(number, first, end):
    xml = (f"<html><body><h2>By an author</h2><p>Contents</p><h2>{first}</h2>"
           f"<p>Story</p><p>Moral</p><h2>Next</h2><pre>Another story</pre>{end}"
           "<pre>License</pre></body></html>")
    assert list(sections(number, xml)) == [(1, first, "Story\n\nMoral"), (2, "Next", "Another story")]


def test_kwaidan_uses_printed_headings_not_insect_studies():
    source = ("TABLE OF CONTENTS\n  ONE\n  TWO\n  INSECT STUDIES"
              "\nKWAIDAN\n\nONE\n\nFirst tale [1].\n\nTWO\n\nSecond tale."
              "\nINSECT STUDIES\nButterflies")
    assert list(sections(1210, source)) == [(1, "One", "First tale."), (2, "Two", "Second tale.")]


def test_edition_requires_matching_id_and_affirmative_rights():
    rdf = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           'xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">'
           '<pg:ebook rdf:about="ebooks/21"><dc:rights>Public domain in the USA.</dc:rights>'
           '</pg:ebook></rdf:RDF>')
    notice = ("[EBook #21]\nLanguage: English\n*** START OF THIS PROJECT GUTENBERG EBOOK "
              "***\nStory\n*** END OF THIS PROJECT GUTENBERG EBOOK ***")
    verify_edition(21, rdf, notice)
    for metadata, text in [(rdf.replace("USA", "UK"), notice), (rdf.replace("/21", "/12"), notice),
                           (rdf, "Posted with permission " + notice), (rdf, notice.replace("#21", "#12"))]:
        with pytest.raises(ValueError):
            verify_edition(21, metadata, text)
