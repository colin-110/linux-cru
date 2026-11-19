import subprocess
import sys
from typing import Any

import pytest


def test_python_dash_m_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "linuxcru", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "linuxcru 0.1.0"


def test_gui_explains_missing_pyside6(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from linuxcru.gui import app

    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", None)  # makes the import fail
    assert app.main() == 1
    assert "pip install 'linuxcru[gui]'" in capsys.readouterr().err


def test_gui_starts_and_shows_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from linuxcru.gui import app, main_window

    events: list[str] = []

    class FakeApp:
        def __init__(self, argv: Any) -> None:
            events.append("app")

        def setApplicationName(self, name: str) -> None:  # noqa: N802 (Qt API name)
            events.append(f"name={name}")

        def setDesktopFileName(self, name: str) -> None:  # noqa: N802 (Qt API name)
            pass

        def exec(self) -> int:
            events.append("exec")
            return 0

    class FakeWindow:
        def show(self) -> None:
            events.append("show")

    monkeypatch.setattr(widgets, "QApplication", FakeApp)
    monkeypatch.setattr(main_window, "MainWindow", FakeWindow)
    assert app.main() == 0
    assert events == ["app", "name=linuxcru", "show", "exec"]
