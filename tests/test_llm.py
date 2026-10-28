from yt_pdf import config, llm


def test_budget_has_floor_and_cap():
    assert llm.output_token_budget("x" * 400) == llm.MIN_OUTPUT_TOKENS
    assert llm.output_token_budget("x" * 4_000_000) == llm.MAX_OUTPUT_TOKENS


def test_budget_scales_with_transcript_length():
    short, medium, long = (llm.output_token_budget("x" * n) for n in (10_000, 20_000, 30_000))
    assert llm.MIN_OUTPUT_TOKENS <= short < medium < long <= llm.MAX_OUTPUT_TOKENS


def test_short_video_requests_less_than_the_maximum():
    # A ~7 minute video (about 7k characters) must fit a key that can only afford ~4k tokens.
    assert llm.output_token_budget("x" * 7_000) < 4_000


def _fake_complete(replies, calls):
    def fake(provider, key, model, system, user, max_tokens=0, retries=0):
        calls.append(max_tokens)
        return replies[len(calls) - 1]

    return fake


def test_truncated_reply_is_retried_once_at_the_cap(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(llm, "complete", _fake_complete([("part", True), ("full guide", False)], calls))
    text, truncated = llm.generate_guide("openai", "k", "m", "t", "x" * 400, 5)
    assert (text, truncated) == ("full guide", False)
    assert calls == [llm.MIN_OUTPUT_TOKENS, llm.MAX_OUTPUT_TOKENS]


def test_no_retry_when_already_at_the_cap(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(llm, "complete", _fake_complete([("part", True)], calls))
    text, truncated = llm.generate_guide("openai", "k", "m", "t", "x" * 4_000_000, 5)
    assert (text, truncated) == ("part", True)
    assert calls == [llm.MAX_OUTPUT_TOKENS]


def test_default_models_are_in_the_curated_lists():
    for name, meta in config.PROVIDERS.items():
        assert meta["default_model"] in meta["models"], name


def test_openrouter_default_is_not_the_retired_model():
    assert config.PROVIDERS["openrouter"]["default_model"] != "anthropic/claude-3.7-sonnet"
    assert "anthropic/claude-3.7-sonnet" not in config.PROVIDERS["openrouter"]["models"]
