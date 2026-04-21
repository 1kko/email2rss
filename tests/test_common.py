import stat

import pytest


def test_validate_reader_config_ok_when_baseurl_set(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", True)
    monkeypatch.setitem(common.config, "server_baseurl", "http://localhost:8000")
    common.validate_reader_config()  # no exception


def test_validate_reader_config_ok_when_reader_disabled(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", False)
    monkeypatch.setitem(common.config, "server_baseurl", None)
    common.validate_reader_config()  # no exception


def test_validate_reader_config_raises_when_reader_enabled_without_baseurl(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", True)
    monkeypatch.setitem(common.config, "server_baseurl", None)
    with pytest.raises(RuntimeError, match="server_baseurl"):
        common.validate_reader_config()


def test_img_proxy_secret_is_generated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("img_proxy_secret", raising=False)
    import common
    monkeypatch.setitem(common.config, "data_dir", str(tmp_path))
    # clear any cached value
    common.config["img_proxy_secret"] = None

    first = common.get_img_proxy_secret()
    assert len(first) >= 32  # urlsafe token_urlsafe(32) yields ~43 chars

    # File exists with mode 0600
    secret_file = tmp_path / "img_proxy_secret"
    assert secret_file.exists()
    perms = stat.S_IMODE(secret_file.stat().st_mode)
    assert perms == 0o600

    # Second call returns same bytes (cached OR read from file)
    common.config["img_proxy_secret"] = None  # clear cache
    second = common.get_img_proxy_secret()
    assert first == second


def test_img_proxy_secret_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("img_proxy_secret", "fixed-test-secret")
    import common
    monkeypatch.setitem(common.config, "data_dir", str(tmp_path))
    common.config["img_proxy_secret"] = None

    assert common.get_img_proxy_secret() == b"fixed-test-secret"
    # File not created when env var set
    assert not (tmp_path / "img_proxy_secret").exists()
