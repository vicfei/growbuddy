import os
import stat

from naga.storage import LAYOUT_DIRS, PRIVATE_DIRS, init_home, resolve_home


def test_layout_created_idempotent(tmp_path):
    home = init_home(str(tmp_path / "home"))
    for d in LAYOUT_DIRS:
        assert os.path.isdir(os.path.join(home, d)), d
    # 幂等:重复初始化不报错、布局不变
    assert init_home(home) == home
    assert os.path.isdir(os.path.join(home, "ledger/segments"))


def test_private_dirs_are_0700(tmp_path):
    home = init_home(str(tmp_path / "home"))
    for d in PRIVATE_DIRS:
        mode = stat.S_IMODE(os.stat(os.path.join(home, d)).st_mode)
        assert mode == 0o700, d


def test_home_priority(tmp_path, monkeypatch):
    explicit = str(tmp_path / "explicit")
    env_home = str(tmp_path / "envhome")
    monkeypatch.setenv("SAMSARA_HOME", env_home)
    assert resolve_home(explicit) == explicit
    assert resolve_home() == env_home
    monkeypatch.delenv("SAMSARA_HOME")
    assert resolve_home().endswith(".samsara")
