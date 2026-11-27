import pytest

from pipeline.source.draft import draft
from pipeline.story import validate_story


def source(text):
    return dict(id="abc123", title="An invented clock", url="https://example.org/abc123",
                author="Invented Author", subreddit="shortstories", retrieved_at="2026-11-27",
                route="cache", text=text)


def test_long_story_selects_chronological_excerpts_including_turn_and_ending():
    sentences = [f"Keeper {i} stepped onto the bridge and rang the cracked bell." for i in range(80)]
    sentences[53] = "Only then did the keeper hear footsteps returning over the bridge."
    text = " ".join(sentences)
    story = draft(source(text))
    assert story == draft(source(text))
    assert validate_story(story) == story
    assert 200 <= len(story["script"].split()) <= 230
    assert story["script"].startswith(sentences[0]) and story["script"].endswith(sentences[-1])
    assert sentences[53] in story["script"]
    assert story["script"] == " ".join(line["text"] for line in story["lines"])
    pos = 0
    for line in story["lines"]:
        assert line["speaker"] == "narrator"
        pos = text.index(line["text"], pos) + len(line["text"])
    assert story["source"]["private_use_only"] and "internal demo only" in story["source"]["note"]
    assert story["source"]["author"] == "Invented Author"
    assert story["voice"]["provider"] == "say"


@pytest.mark.parametrize("count", [200, 215, 230, 231, 1000])
def test_bounds_when_source_has_no_sentence_breaks(count):
    words = [f"word{i}" for i in range(count)]
    got = draft(source(" ".join(words)))["script"].split()
    assert 200 <= len(got) <= 230
    assert got[0] == words[0] and got[-1] == words[-1]
    if count <= 230:
        assert got == words


def test_short_sources_are_not_padded_and_urls_are_not_spoken():
    with pytest.raises(ValueError, match="insufficient source: found 199"):
        draft(source("word " * 198 + "[credit](https://example.org/author) https://example.org/tags ---"))


def test_removed_posts_cannot_be_drafted():
    with pytest.raises(ValueError, match="removed"):
        draft(dict(source("word " * 300), removed=True))
