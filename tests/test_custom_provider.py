import json

import pytest
from click.testing import CliRunner

from yt_pdf import cli, config, llm, pdf, video
from yt_pdf.errors import YtPdfError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://localhost:11434/v1", "http://localhost:11434/v1"),
        ("  https://api.example.com/v1/  ", "https://api.example.com/v1"),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
        ("http://192.168.1.5:8000/v1/chat/completions/", "http://192.168.1.5:8000/v1"),
    ],
)
def test_base_urls_are_normalised(raw, expected):
    assert config.normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "localhost:11434", "ftp://x/v1", "file:///etc/passwd", "https://"])
def test_bad_base_urls_are_rejected(raw):
    with pytest.raises(YtPdfError):
        config.normalize_base_url(raw)


def test_plain_http_to_another_machine_is_flagged_but_local_is_not():
    assert config.is_insecure_remote("http://10.0.0.7:8000/v1")
    assert not config.is_insecure_remote("http://localhost:11434/v1")
    assert not config.is_insecure_remote("http://127.0.0.1:1234/v1")
    assert not config.is_insecure_remote("https://api.example.com/v1")


def test_custom_settings_need_a_url_and_a_model():
    cfg = {"active_provider": "custom", "providers": {"custom": {"api_key": config.NO_KEY, "active_model": "llama3"}}}
    with pytest.raises(YtPdfError, match="URL"):
        config.active_settings(cfg)
    cfg["providers"]["custom"].update(base_url="http://localhost:11434/v1", active_model="")
    with pytest.raises(YtPdfError, match="model"):
        config.active_settings(cfg)
    cfg["providers"]["custom"]["active_model"] = "llama3"
    assert config.active_settings(cfg) == ("custom", config.NO_KEY, "llama3")
    assert config.active_base_url(cfg) == "http://localhost:11434/v1"


def test_base_url_is_only_returned_for_the_custom_provider():
    cfg = {"active_provider": "openai", "providers": {"custom": {"base_url": "http://x/v1"}}}
    assert config.active_base_url(cfg) is None


def test_custom_endpoint_is_called_with_its_url_and_max_tokens(monkeypatch):
    seen = {}

    class Msg:
        content = "hi"

    class Choice:
        message = Msg()
        finish_reason = "stop"

    class Completions:
        def create(self, **kw):
            seen["request"] = kw
            return type("R", (), {"choices": [Choice()]})()

    class FakeOpenAI:
        def __init__(self, **kw):
            seen["client"] = kw
            self.chat = type("C", (), {"completions": Completions()})()

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    text, truncated = llm.complete("custom", "k", "llama3", "sys", "usr", max_tokens=99, base_url="http://localhost:11434/v1")
    assert (text, truncated) == ("hi", False)
    assert seen["client"]["base_url"] == "http://localhost:11434/v1"
    assert seen["request"]["model"] == "llama3" and seen["request"]["max_tokens"] == 99
    assert "max_completion_tokens" not in seen["request"]


def test_custom_without_a_url_is_an_error():
    with pytest.raises(YtPdfError, match="URL"):
        llm.complete("custom", "k", "m", "s", "u", base_url=None)


def test_list_models_survives_a_server_with_no_model_list(monkeypatch):
    import openai

    class Boom:
        def __init__(self, **kw):
            raise RuntimeError("no /models here")

    monkeypatch.setattr(openai, "OpenAI", Boom)
    assert llm.list_models("k", "http://localhost:1/v1") == []


# ---- the login and run flows ------------------------------------------------------------------


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


@pytest.fixture
def cfg_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PDF_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_login_saves_the_url_and_works_without_a_key(cfg_dir, monkeypatch):
    verified = {}
    monkeypatch.setattr(cli.questionary, "select", lambda msg, choices: Answer(
        config.PROVIDERS["custom"]["label"] if "provider" in msg else "llama3"))
    monkeypatch.setattr(cli.questionary, "text", lambda msg, default="": Answer("http://localhost:11434/v1/"))
    monkeypatch.setattr(cli.questionary, "password", lambda msg: Answer(""))
    monkeypatch.setattr(llm, "list_models", lambda key, url: ["llama3", "qwen3"])
    monkeypatch.setattr(llm, "verify_key", lambda *a, **k: verified.update(args=a, kw=k))
    result = CliRunner().invoke(cli.cli, ["auth", "login"])
    assert result.exit_code == 0, result.output
    saved = json.loads((cfg_dir / "config.json").read_text())
    assert saved["active_provider"] == "custom"
    assert saved["providers"]["custom"] == {
        "api_key": config.NO_KEY, "active_model": "llama3", "base_url": "http://localhost:11434/v1"
    }
    assert verified["args"][:3] == ("custom", config.NO_KEY, "llama3")
    assert verified["args"][3] == "http://localhost:11434/v1"
    assert "no key" in result.output


def test_login_warns_about_plain_http_to_another_host(cfg_dir, monkeypatch):
    monkeypatch.setattr(cli.questionary, "select", lambda msg, choices: Answer(
        config.PROVIDERS["custom"]["label"] if "provider" in msg else "m"))
    monkeypatch.setattr(cli.questionary, "text", lambda msg, default="": Answer("http://10.0.0.7:8000/v1"))
    monkeypatch.setattr(cli.questionary, "password", lambda msg: Answer("secret-key-123456"))
    monkeypatch.setattr(llm, "list_models", lambda key, url: ["m"])
    monkeypatch.setattr(llm, "verify_key", lambda *a, **k: None)
    result = CliRunner().invoke(cli.cli, ["auth", "login"])
    assert result.exit_code == 0 and "unencrypted" in result.output


def test_run_passes_the_custom_url_to_the_model_call(cfg_dir, monkeypatch):
    config.save_config({
        "active_provider": "custom",
        "providers": {"custom": {"api_key": config.NO_KEY, "active_model": "llama3", "base_url": "http://localhost:11434/v1"}},
    })
    seen = {}
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(video, "get_video_info", lambda url, vid: video.VideoInfo(video_id=vid, title="T"))
    monkeypatch.setattr(video, "fetch_snippets", lambda vid: [(0.0, "hello")])
    monkeypatch.setattr(llm, "generate_guide", lambda *a, **k: seen.update(a=a, k=k) or ("## H\nbody", False))
    monkeypatch.setattr(pdf, "render_pdf", lambda doc, out, log=None: None)
    result = CliRunner().invoke(cli.cli, ["https://youtu.be/jNQXAC9IVRw", "-o", str(cfg_dir / "o.pdf")])
    assert result.exit_code == 0, result.output
    assert seen["a"][:3] == ("custom", config.NO_KEY, "llama3")
    assert seen["k"]["base_url"] == "http://localhost:11434/v1"


def test_auth_status_shows_the_endpoint(cfg_dir):
    config.save_config({
        "active_provider": "custom",
        "providers": {"custom": {"api_key": config.NO_KEY, "active_model": "llama3", "base_url": "http://localhost:11434/v1"}},
    })
    out = CliRunner().invoke(cli.cli, ["auth"]).output
    assert "Endpoint:" in out and "http://localhost:11434/v1" in out
