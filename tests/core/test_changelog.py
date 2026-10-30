"""The changelog: what's new to someone on an older version, and each version's release notes."""

from pathlib import Path

import pytest

import proofloop
from proofloop.core.changelog import as_text, main, newer_than, notes, sections

CHANGELOG = (Path(__file__).resolve().parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
TOML = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")


def test_this_version_has_its_changelog_section():
    assert sections(CHANGELOG)[0]["version"] == proofloop.__version__  # the newest section is this version
    assert f'version = "{proofloop.__version__}"' in TOML  # and pyproject.toml says the same
    assert "**Orders.**" in notes(CHANGELOG, proofloop.__version__)


def test_whats_new_since_an_older_version():
    text = "# Changelog\n\n## 0.10.0 · 2027-01-02\n- **Big.** thing\n\n## 0.9.1\n- fix\n\n## 0.9.0\n- first\n"
    assert [s["version"] for s in newer_than(text, "0.9.0")] == ["0.10.0", "0.9.1"]  # 0.10 > 0.9, not text order
    assert newer_than(text, "0.10.0") == []
    assert as_text(newer_than(text, "0.9.1")) == "0.10.0 (2027-01-02)\n  - Big. thing"
    with pytest.raises(KeyError):
        notes(text, "1.0.0")


def test_release_notes_from_the_command_line(capsys, tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text("## 1.2.0 · 2027-03-04\n- one\n- two\n", encoding="utf-8")
    assert main(["v1.2.0", str(f)]) == 0
    assert capsys.readouterr().out == "- one\n- two\n"
