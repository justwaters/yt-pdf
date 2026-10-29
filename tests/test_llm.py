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
    def fake(provider, key, model, system, user, max_tokens=0, retries=0, base_url=None):
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
        if name == config.CUSTOM:
            continue  # its model depends on the server the user points it at
        assert meta["default_model"] in meta["models"], name


def test_openrouter_default_is_not_the_retired_model():
    assert config.PROVIDERS["openrouter"]["default_model"] != "anthropic/claude-3.7-sonnet"
    assert "anthropic/claude-3.7-sonnet" not in config.PROVIDERS["openrouter"]["models"]


def test_prompt_asks_for_a_section_time_under_every_heading_and_for_screenshots():
    assert "{{AT: <seconds>}}" in llm.SYSTEM_PROMPT
    assert "{{SCREENSHOT: <seconds>}}" in llm.SYSTEM_PROMPT


def test_every_style_reaches_the_system_prompt(monkeypatch):
    seen = {}

    def fake(provider, key, model, system, user, max_tokens=0, retries=0, base_url=None):
        seen.update(system=system, max_tokens=max_tokens)
        return "text", False

    monkeypatch.setattr(llm, "complete", fake)
    for name, spec in llm.STYLES.items():
        llm.generate_guide("openai", "k", "m", "t", "x" * 4_000_000, 20, style=name)
        assert spec.rules in seen["system"]
        assert seen["max_tokens"] == spec.max_tokens
        assert "{" not in seen["system"].replace("{{", "").replace("}}", "")  # no unfilled placeholders


def test_summary_is_limited_to_a_few_shots_and_a_small_reply(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "complete", lambda p, k, m, system, u, max_tokens=0, retries=0, base_url=None: seen.update(s=system) or ("t", False))
    llm.generate_guide("openai", "k", "m", "t", "x" * 4_000_000, 20, style="summary")
    assert "at most 3 tags" in seen["s"]
    assert llm.output_token_budget("x" * 4_000_000, llm.STYLES["summary"].max_tokens) == 4000
