"""Entry point for the linuxcru window (``linuxcru-gui``)."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "The linuxcru window needs PySide6: pip install 'linuxcru[gui]'\n"
            "(the command line tool `linuxcru` works without it)",
            file=sys.stderr,
        )
        return 1

    from linuxcru.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("linuxcru")
    app.setDesktopFileName("linuxcru")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
