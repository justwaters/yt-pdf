import io
import json
import subprocess

import pytest
from click.testing import CliRunner

from yt_pdf import __version__, cli, updater
from yt_pdf.errors import YtPdfError


@pytest.mark.parametrize(
    "a,b",
    [("0.6.1", "0.7.0"), ("0.9.0", "0.10.0"), ("v0.7", "0.7.1"), ("0.6.1", "1.0.0")],
)
def test_versions_compare_numerically(a, b):
    assert updater.parse_version(a) < updater.parse_version(b)


def test_missing_parts_count_as_zero():
    assert updater.parse_version("v0.7") == updater.parse_version("0.7.0") == (0, 7, 0)


def test_garbage_version_is_an_error():
    with pytest.raises(YtPdfError):
        updater.parse_version("latest")


def test_latest_version_reads_the_release_tag(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(json.dumps({"tag_name": "v0.8.2"}).encode()))
    assert updater.latest_version() == "0.8.2"


def test_unreachable_github_is_a_friendly_error(monkeypatch):
    def boom(req, timeout=0):
        raise updater.urllib.error.URLError("offline")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    with pytest.raises(YtPdfError, match="Could not check for updates"):
        updater.latest_version()


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("/opt/homebrew/Cellar/yt-pdf/0.6.1/libexec", "brew"),
        ("/home/linuxbrew/.linuxbrew/Cellar/yt-pdf/0.6.1/libexec", "brew"),
        ("/Users/x/.local/pipx/venvs/yt-pdf", "pipx"),
        ("/Users/x/.local/share/uv/tools/yt-pdf", "uv"),
        ("/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13", "pip"),  # brew's python, not yt-pdf's keg
        ("/Users/x/proj/.venv", "pip"),
    ],
)
def test_install_method_from_the_environment_path(monkeypatch, prefix, expected):
    monkeypatch.setattr(updater.sys, "prefix", prefix)
    monkeypatch.setattr(updater.metadata, "distribution", lambda name: type("D", (), {"read_text": lambda self, f: None})())
    assert updater.install_method() == expected


def test_editable_install_is_a_dev_checkout(monkeypatch):
    direct = json.dumps({"url": "file:///x", "dir_info": {"editable": True}})
    monkeypatch.setattr(updater.metadata, "distribution", lambda name: type("D", (), {"read_text": lambda self, f: direct})())
    assert updater.install_method() == "dev"


def test_upgrade_commands_pin_the_release_tag():
    assert updater.upgrade_command("brew", "0.7.0") == ["brew", "upgrade", "justwaters/tap/yt-pdf"]
    for method in ("pipx", "uv", "pip"):
        assert updater.upgrade_command(method, "0.7.0")[-1] == "git+https://github.com/justwaters/yt-pdf@v0.7.0"


@pytest.fixture
def newer(monkeypatch):
    monkeypatch.setattr(updater, "latest_version", lambda timeout=10: "99.0.0")


def run_update(*args):
    return CliRunner().invoke(cli.cli, ["update", *args])


def test_up_to_date_does_nothing(monkeypatch):
    monkeypatch.setattr(updater, "latest_version", lambda timeout=10: __version__)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    result = run_update()
    assert "up to date" in result.output and not ran


def test_check_reports_without_installing(newer, monkeypatch):
    monkeypatch.setattr(updater, "install_method", lambda: "brew")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    result = run_update("--check")
    assert "99.0.0 is available" in result.output and "yt-pdf update" in result.output and not ran


def test_update_runs_the_install_method_command(newer, monkeypatch):
    monkeypatch.setattr(updater, "install_method", lambda: "brew")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    result = run_update()
    assert result.exit_code == 0, result.output
    assert ran == [["brew", "upgrade", "justwaters/tap/yt-pdf"]] and "Updated" in result.output


def test_failed_update_command_is_reported_with_the_manual_command(newer, monkeypatch):
    monkeypatch.setattr(updater, "install_method", lambda: "pipx")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 3))
    result = run_update()
    assert result.exit_code != 0 and "exit 3" in str(result.exception) and "pipx install --force" in str(result.exception)


def test_missing_installer_tool_is_explained(newer, monkeypatch):
    monkeypatch.setattr(updater, "install_method", lambda: "uv")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result = run_update()
    assert "`uv` was not found" in str(result.exception)


def test_dev_checkout_is_not_touched(newer, monkeypatch):
    monkeypatch.setattr(updater, "install_method", lambda: "dev")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    result = run_update()
    assert "development checkout" in result.output and not ran
