"""The configuration folder, as spec/README.md#configuration-folder gives it."""

from pathlib import Path

from ebrains_bucket_sync.auth import TokenStore
from ebrains_bucket_sync.config import CONFIG_DIR_VARIABLE, config_dir

HOME = Path("/home/ada")


def test_macos_uses_application_support():
    folder = config_dir(platform="darwin", environ={}, home=HOME)

    assert folder == HOME / "Library" / "Application Support" / "ebrains-bucket-sync"


def test_windows_uses_local_appdata():
    environ = {"LOCALAPPDATA": "C:/Users/ada/AppData/Local"}

    folder = config_dir(platform="win32", environ=environ, home=HOME)

    assert folder == Path("C:/Users/ada/AppData/Local") / "ebrains-bucket-sync"


def test_windows_without_local_appdata_uses_the_home_folder():
    folder = config_dir(platform="win32", environ={}, home=HOME)

    assert folder == HOME / "AppData" / "Local" / "ebrains-bucket-sync"


def test_other_platforms_use_xdg_config_home():
    folder = config_dir(platform="linux", environ={"XDG_CONFIG_HOME": "/xdg"}, home=HOME)

    assert folder == Path("/xdg") / "ebrains-bucket-sync"


def test_other_platforms_fall_back_to_dot_config_when_xdg_is_unset_or_empty():
    unset = config_dir(platform="linux", environ={}, home=HOME)
    empty = config_dir(platform="linux", environ={"XDG_CONFIG_HOME": ""}, home=HOME)

    assert unset == empty == HOME / ".config" / "ebrains-bucket-sync"


def test_the_environment_variable_names_the_folder_on_every_platform():
    environ = {CONFIG_DIR_VARIABLE: "/custom", "XDG_CONFIG_HOME": "/xdg", "LOCALAPPDATA": "C:/l"}

    for platform in ("darwin", "win32", "linux"):
        assert config_dir(platform=platform, environ=environ, home=HOME) == Path("/custom")


def test_an_empty_environment_variable_is_ignored():
    folder = config_dir(platform="linux", environ={CONFIG_DIR_VARIABLE: ""}, home=HOME)

    assert folder == HOME / ".config" / "ebrains-bucket-sync"


def test_tokens_are_kept_in_the_configuration_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, str(tmp_path))

    assert TokenStore().path == tmp_path / "tokens.json"
