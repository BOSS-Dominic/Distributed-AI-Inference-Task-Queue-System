"""Keyword guard contract (shared/jailbreak_words.py)."""

from shared.jailbreak_words import JAILBREAK_KEYWORDS, JAILBREAK_PAYLOADS


def test_keywords_non_empty_and_lowercase():
    assert JAILBREAK_KEYWORDS
    assert all(k == k.lower() for k in JAILBREAK_KEYWORDS)


def test_every_payload_triggers_a_keyword():
    for payload in JAILBREAK_PAYLOADS:
        text = payload.lower()
        assert any(k in text for k in JAILBREAK_KEYWORDS), payload


def test_benign_text_passes():
    benign = "The weather is nice today."
    assert not any(k in benign.lower() for k in JAILBREAK_KEYWORDS)
