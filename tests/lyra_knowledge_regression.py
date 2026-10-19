#!/usr/bin/env python3
"""Regression coverage for Lyra's complete, bounded local retrieval."""

from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from panelyra.assistant import CONVERSATIONS, PageContext, PanelyraKnowledgeService


ROOT = Path(os.environ.get("PANELYRA_PRIVATE_TEST_ROOT", str(Path(__file__).resolve().parents[1])))


def main() -> None:
    service = PanelyraKnowledgeService(ROOT)
    snapshot = service.snapshot()
    assert snapshot.comment_count >= 500
    assert snapshot.reader_series_count >= 1
    assert snapshot.run_count >= 1

    questions = (
        "What comments have I generated?",
        "Which comment styles fail most?",
        "What books and chapters are in my library?",
        "Why is chapter 8 missing from Clean?",
        "What happened in the latest Capture run?",
        "How does Studio work?",
        "What page am I on and what should I do next?",
    )
    for question in questions:
        bundle = service.retrieve(question, PageContext(page="Assistant"))
        assert bundle.relevant
        assert bundle.prompt_text
        assert len(bundle.prompt_text) <= 6_500
        assert "built-in assistant" in bundle.prompt_text
        assert bundle.source_labels
        if any(term in question.lower() for term in ("book", "chapter", "library", "clean")):
            assert "Reader catalog" in bundle.source_labels

    general = service.retrieve("Write a short birthday message", PageContext(page="Assistant"))
    assert not general.relevant and not general.prompt_text
    greeting = service.retrieve("hi", PageContext(page="Dashboard"))
    assert not greeting.relevant and not greeting.prompt_text

    first = CONVERSATIONS.new_token()
    second = CONVERSATIONS.new_token()
    CONVERSATIONS.append(first, {"role": "user", "content": "chapter 8"})
    assert len(CONVERSATIONS.messages(first)) == 1
    assert CONVERSATIONS.messages(second) == []
    CONVERSATIONS.clear(first)
    assert CONVERSATIONS.messages(first) == []
    print("Lyra knowledge regression passed.")


if __name__ == "__main__":
    main()
