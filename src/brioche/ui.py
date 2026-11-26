"""Qt desktop shell facade exporting MainWindow, SessionWindow, RemoteWindow and FrameMailbox."""

from pathlib import Path
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .remote_session import (
    Bridge,
    FrameMailbox,
    RemoteCanvas,
    RemoteSessionPage,
    RemoteWindow,
    SessionWindow,
)
from .theme import build_stylesheet

STYLE = build_stylesheet("light")


def run():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Brioche")
    app.setOrganizationName("Brioche")
    assets_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    app.setWindowIcon(QIcon(str(assets_root / "assets/brioche.svg")))
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec()


__all__ = [
    "Bridge",
    "FrameMailbox",
    "MainWindow",
    "RemoteCanvas",
    "RemoteSessionPage",
    "RemoteWindow",
    "STYLE",
    "SessionWindow",
    "run",
]
