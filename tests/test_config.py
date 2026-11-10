import dataclasses

import pytest

from bestyet import create_app
from bestyet.config import Config, ConfigError, Secret, load_dotenv

KEY = "test-key-123"
VALID = {
    "GRIST_URL": "https://grist.example.com",
    "GRIST_DOC_ID": "DOC_ID_HERE",
    "GRIST_API_KEY": KEY,
}


def env(**changes):
    merged = {**VALID, **changes}
    return {k: v for k, v in merged.items() if v is not None}


def test_valid_load():
    cfg = Config.from_env(env(PORT="9000", TZ="Europe/London"))
    assert cfg.grist_url == "https://grist.example.com"
    assert cfg.grist_doc_id == "DOC_ID_HERE"
    assert cfg.grist_api_key.reveal() == KEY
    assert cfg.port == 9000
    assert cfg.tz == "Europe/London"


def test_defaults():
    cfg = Config.from_env(env())
    assert cfg.port == 8585
    assert cfg.tz == "UTC"


def test_trailing_slash_stripped():
    assert Config.from_env(env(GRIST_URL="http://grist.example.com:8484//")).grist_url == (
        "http://grist.example.com:8484"
    )


@pytest.mark.parametrize("name", list(VALID))
def test_each_missing_var(name):
    with pytest.raises(ConfigError) as err:
        Config.from_env(env(**{name: None}))
    assert name in str(err.value)


def test_blank_counts_as_missing():
    with pytest.raises(ConfigError, match="GRIST_DOC_ID is missing"):
        Config.from_env(env(GRIST_DOC_ID="  "))


@pytest.mark.parametrize(
    "url",
    [
        "grist.example.com",
        "ftp://grist.example.com",
        "https://",
        "https:///path",
        "https://grist.example.com?a=1",
        "https://grist.example.com/#frag",
        "https://grist.example.com:abc",
    ],
)
def test_bad_url(url):
    with pytest.raises(ConfigError, match="GRIST_URL"):
        Config.from_env(env(GRIST_URL=url))


@pytest.mark.parametrize(
    "port", ["abc", "0", "70000", "-1", "+80", "8_000", "80 80", " 8000", "\u0668\u0660"]
)
def test_bad_port(port):
    with pytest.raises(ConfigError, match="PORT"):
        Config.from_env(env(PORT=port))


def test_bad_tz():
    with pytest.raises(ConfigError, match="TZ"):
        Config.from_env(env(TZ="Not/AZone"))


def test_all_problems_in_one_error_without_values():
    bad = {"GRIST_URL": "nope-secret-url", "PORT": "port-secret", "TZ": "tz-secret"}
    with pytest.raises(ConfigError) as err:
        Config.from_env(bad)
    text = str(err.value)
    for name in ("GRIST_URL", "GRIST_DOC_ID", "GRIST_API_KEY", "PORT", "TZ"):
        assert name in text
    for value in bad.values():
        assert value not in text


def test_key_never_in_repr_str_or_error():
    cfg = Config.from_env(env())
    assert KEY not in repr(cfg)
    assert KEY not in str(cfg)
    assert KEY not in f"{cfg}"
    assert KEY not in repr(create_app(cfg).config["BESTYET"])
    assert KEY not in repr(dataclasses.asdict(cfg))
    assert KEY not in repr(vars(cfg))
    assert KEY not in f"{cfg.grist_api_key}"
    with pytest.raises(ConfigError) as err:
        Config.from_env(env(PORT="bad"))
    assert KEY not in str(err.value)


def test_health_without_config_and_config_stored():
    assert create_app().test_client().get("/health").status_code == 200
    cfg = Config.from_env(env())
    assert create_app(cfg).config["BESTYET"] is cfg


def test_dotenv_loads_and_does_not_override(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nGRIST_URL=https://from-file.example.com\n"
        'GRIST_DOC_ID="QUOTED_ID"\nGRIST_API_KEY=file-key\n'
    )
    environ = {"GRIST_URL": "https://already.example.com"}
    load_dotenv(path, environ)
    assert environ["GRIST_URL"] == "https://already.example.com"
    assert environ["GRIST_DOC_ID"] == "QUOTED_ID"
    assert environ["GRIST_API_KEY"] == "file-key"


def test_dotenv_missing_file_is_fine(tmp_path):
    environ = {}
    load_dotenv(tmp_path / "absent.env", environ)
    assert environ == {}


def test_dotenv_export_prefix_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "export GRIST_URL=https://grist.example.com  # note\n"
        'GRIST_DOC_ID="DOC#1" # kept hash\n'
        "GRIST_API_KEY=abc#def\n"
    )
    environ = {}
    load_dotenv(path, environ)
    assert environ["GRIST_URL"] == "https://grist.example.com"
    assert environ["GRIST_DOC_ID"] == "DOC#1"
    assert environ["GRIST_API_KEY"] == "abc#def"


def test_dotenv_empty_existing_is_replaced(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GRIST_URL=https://from-file.example.com\nPORT=9000\n")
    environ = {"GRIST_URL": "", "PORT": "8000"}
    load_dotenv(path, environ)
    assert environ["GRIST_URL"] == "https://from-file.example.com"
    assert environ["PORT"] == "8000"


def test_secret_masks_and_reveals():
    secret = Secret(KEY)
    assert KEY not in repr(secret) and KEY not in str(secret)
    assert secret.reveal() == KEY
