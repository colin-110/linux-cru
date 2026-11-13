"""Run the root helper from an unprivileged process."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence

HELPER_MODULE = "linuxcru.system.helper"


class PrivilegeError(RuntimeError):
    pass


def helper_command(args: Sequence[str], *, as_root: bool | None = None) -> list[str]:
    """Command line that runs the helper with root rights.

    ``-I`` keeps Python from picking up PYTHONPATH or the user's site-packages
    while running as root.
    """
    base = [sys.executable, "-I", "-m", HELPER_MODULE, *args]
    if as_root is None:
        as_root = os.geteuid() == 0
    if as_root:
        return base
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise PrivilegeError("pkexec was not found; install policykit-1 or run with sudo")
    return [pkexec, *base]


def run_helper(args: Sequence[str], *, escalate: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the helper, asking for the admin password when ``escalate`` is set.

    Dry runs only read files, so they are run without escalating.
    """
    command = helper_command(args, as_root=None if escalate else True)
    return subprocess.run(command, capture_output=True, text=True)


def password_was_cancelled(result: subprocess.CompletedProcess[str]) -> bool:
    # pkexec exits with 126 when the dialog is dismissed and 127 when not authorized.
    return result.returncode in (126, 127) and not result.stdout
