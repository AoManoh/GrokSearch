from grok_search.sources import split_answer_and_sources


# ---------- inline [[N]](url) citations (grok2api v2.0.4+) ----------

def test_inline_citation_single():
    text = "AIGC detection mainly analyzes perplexity [[1]](https://example.com/a) and burstiness."
    answer, sources = split_answer_and_sources(text)
    assert answer == text.strip()
    assert sources == [{"url": "https://example.com/a"}]


def test_inline_citation_multiple_in_order():
    text = (
        "First fact [[1]](https://a.com/x) then "
        "second fact [[2]](https://b.com/y) and finally "
        "third [[3]](https://c.com/z)."
    )
    answer, sources = split_answer_and_sources(text)
    assert answer == text.strip()
    assert sources == [
        {"url": "https://a.com/x"},
        {"url": "https://b.com/y"},
        {"url": "https://c.com/z"},
    ]


def test_inline_citation_deduplicates_same_url():
    text = (
        "Fact A [[1]](https://a.com/x) then "
        "same source again [[1]](https://a.com/x), "
        "plus a new one [[2]](https://b.com/y)."
    )
    answer, sources = split_answer_and_sources(text)
    assert answer == text.strip()
    assert sources == [
        {"url": "https://a.com/x"},
        {"url": "https://b.com/y"},
    ]


def test_inline_citation_preserves_full_answer_body():
    """Unlike heading/tail strategies, inline citations must not truncate answer."""
    text = "This is the full answer body with a citation [[1]](https://example.com) embedded."
    answer, sources = split_answer_and_sources(text)
    assert "[[1]](https://example.com)" in answer
    assert len(sources) == 1


def test_inline_citation_cjk_context():
    text = "困惑度分析 [[1]](https://cloud.tencent.com/developer/article/2638817) 和突发性 [[2]](https://zhuanlan.zhihu.com/p/2016090694893205381) 共同决定检测结果。"
    answer, sources = split_answer_and_sources(text)
    assert answer == text.strip()
    assert sources == [
        {"url": "https://cloud.tencent.com/developer/article/2638817"},
        {"url": "https://zhuanlan.zhihu.com/p/2016090694893205381"},
    ]


# ---------- priority: earlier strategies still win ----------

def test_heading_sources_takes_precedence_over_inline():
    """When explicit '## Sources' heading exists, heading strategy wins and truncates answer."""
    text = (
        "Answer body with [[1]](https://a.com) inline.\n\n"
        "## Sources\n\n"
        "- [Title A](https://a.com)\n"
        "- [Title B](https://b.com)"
    )
    answer, sources = split_answer_and_sources(text)
    assert "## Sources" not in answer
    urls = {s["url"] for s in sources}
    assert "https://a.com" in urls
    assert "https://b.com" in urls


def test_plain_answer_without_any_citation_yields_empty_sources():
    text = "Plain answer, no citations, no URLs, nothing to harvest."
    answer, sources = split_answer_and_sources(text)
    assert answer == text.strip()
    assert sources == []
