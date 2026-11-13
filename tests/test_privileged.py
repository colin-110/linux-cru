import sys

import pytest

from linuxcru.system import privileged


def test_command_as_root_runs_helper_directly() -> None:
    cmd = privileged.helper_command(["uninstall"], as_root=True)
    assert cmd == [sys.executable, "-I", "-m", "linuxcru.system.helper", "uninstall"]


def test_command_as_user_uses_pkexec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privileged.shutil, "which", lambda _: "/usr/bin/pkexec")
    cmd = privileged.helper_command(["uninstall"], as_root=False)
    assert cmd[:2] == ["/usr/bin/pkexec", sys.executable]


def test_missing_pkexec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privileged.shutil, "which", lambda _: None)
    with pytest.raises(privileged.PrivilegeError, match="pkexec"):
        privileged.helper_command(["uninstall"], as_root=False)
