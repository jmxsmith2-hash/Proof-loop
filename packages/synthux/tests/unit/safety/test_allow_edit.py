import pytest

from synthux.core.config import ConfigError, load_settings
from synthux.safety.allow_edit import add_to_allowlist, host_entry
from synthux.safety.allowlist import check_url
from tests.unit.cli.test_cli import _config


def test_host_entry():
    assert host_entry("https://Staging.Example.com/shop?x=1") == ("staging.example.com", [])
    assert host_entry("http://127.0.0.1:8080/") == ("127.0.0.1", [8080])
    with pytest.raises(ConfigError):
        host_entry("https://user:pw@example.com/")
    with pytest.raises(ConfigError):
        host_entry("javascript:alert(1)")


def test_adding_a_site_keeps_comments_and_is_not_a_sandbox(tmp_path):
    cfg = _config(tmp_path)
    path, added = add_to_allowlist(cfg, "https://staging.example.com/shop")
    assert added and path == cfg / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    assert "# Only these hosts may ever be opened" in text           # comments survive
    settings = load_settings(cfg)
    entry = check_url("https://staging.example.com/cart", settings.safety.allowlist)
    assert entry.sandbox is False
    _, again = add_to_allowlist(cfg, "https://staging.example.com/other")
    assert not again and text == path.read_text(encoding="utf-8")   # no duplicates
