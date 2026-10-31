"""Native desktop GUI (Qt / PySide6) mirroring the Textual TUI (app.py): the same screens in
the same order with the same keys, drawn with real widgets and vector charts. Run with
run_gui.sh. All behaviour is shared with the TUI (see gui_pages.py)."""

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

import garmin_client
from gui_common import COLORS, STYLESHEET, Page, init_bridge, label, run_background
from gui_pages import LoginPage, MainPage

APP_TITLE = "Training Analyze"
SEVERITY_COLORS = {"information": COLORS["green"], "warning": COLORS["yellow"], "error": COLORS["red"]}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 860)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = label(APP_TITLE, "appHeader")
        self.stack = QStackedWidget()
        # Like Textual's Footer: one button per key binding — press the key or click it.
        self.footer = QWidget()
        self.footer.setObjectName("footer")
        self.footer_layout = QHBoxLayout(self.footer)
        self.footer_layout.setContentsMargins(8, 4, 8, 4)
        self.footer_layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.footer)
        self.setCentralWidget(central)
        self.statusBar().setSizeGripEnabled(False)
        self._clear_timer = QTimer(self, singleShot=True, timeout=self.statusBar().clearMessage)

        connecting = Page(self)
        connecting.layout_.addWidget(label("Connecting to Garmin Connect...", "pageTitle"))
        connecting.layout_.addStretch(1)
        self.push(connecting)
        run_background(garmin_client.try_cached_login, self._cached_login_result, lambda e: self._cached_login_result(None))

    # --- navigation (mirrors Textual's screen stack) ---------------------------------

    def push(self, page: Page) -> None:
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self._page_changed()

    def pop(self) -> None:
        if self.stack.count() <= 1:
            return
        page = self.stack.currentWidget()
        self.stack.removeWidget(page)
        page.deleteLater()
        self._page_changed()

    def replace_all(self, page: Page) -> None:
        while self.stack.count():
            old = self.stack.widget(0)
            self.stack.removeWidget(old)
            old.deleteLater()
        self.push(page)

    def _page_changed(self) -> None:
        page: Page = self.stack.currentWidget()
        self.header.setText(f"{APP_TITLE}  ·  {page.title}" if page.title else APP_TITLE)
        self._rebuild_footer(page)
        page.on_shown()
        target = page.focus_widget()
        if target is not None:
            target.setFocus()

    def _rebuild_footer(self, page: Page) -> None:
        while self.footer_layout.count():
            widget = self.footer_layout.takeAt(0).widget()
            if widget is not None:
                # Detach now — deleteLater alone leaves the old buttons drawn until the event
                # loop gets round to deleting them, overlapping the new ones.
                widget.setParent(None)
                widget.deleteLater()
        for key, text, action in page.bindings:
            entry = QPushButton()
            entry.setObjectName("footerKey")
            inner = QHBoxLayout(entry)
            inner.setContentsMargins(6, 2, 8, 2)
            inner.setSpacing(6)
            for part, name in ((key, "footerCap"), (text, "footerText")):
                part_label = QLabel(part)
                part_label.setObjectName(name)
                part_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                inner.addWidget(part_label)
            entry.setMinimumWidth(inner.sizeHint().width())
            entry.setCursor(Qt.CursorShape.PointingHandCursor)
            entry.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep focus (and key bindings) on the page
            entry.setToolTip(f"Press {key}")
            entry.clicked.connect(lambda _=False, act=action: act())
            self.footer_layout.addWidget(entry)
        self.footer_layout.addStretch(1)

    # --- notifications (Textual's notify) ------------------------------------------

    def notify(self, message: str, severity: str = "information") -> None:
        color = SEVERITY_COLORS.get(severity, COLORS["fg"])
        self.statusBar().setStyleSheet(f"color: {color}; padding-left: 8px;")
        self.statusBar().showMessage(message)
        self._clear_timer.start(8000 if severity == "information" else 15000)

    # --- login ---------------------------------------------------------------------

    def _cached_login_result(self, garmin) -> None:
        self.replace_all(MainPage(self, garmin) if garmin is not None else LoginPage(self))

    def logged_in(self, garmin) -> None:
        self.replace_all(MainPage(self, garmin))


ASSETS_DIR = Path(__file__).parent / "assets"


def app_icon() -> QIcon:
    """The app icon at every rendered size (assets/build_icons.py), so each place it shows
    — title bar, taskbar, alt-tab — gets a crisp one."""
    icon = QIcon()
    for path in sorted(ASSETS_DIR.glob("icon-*.png")):
        icon.addFile(str(path))
    return icon


def server_window(url: str) -> QMainWindow:
    """Client mode: the server's web app (server/SETUP.md) in an app window instead of the
    local pages — same data as every other device using that server. Its sign-in is kept
    between runs; downloads (exports) go to the Downloads folder."""
    from PySide6.QtCore import QStandardPaths, QUrl
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView

    window = QMainWindow()
    window.setWindowTitle(f"{APP_TITLE} — {url}")
    window.resize(1400, 900)
    # A named profile keeps cookies on disk, so the server's sign-in survives a restart. It
    # belongs to the application, which outlives the window: a profile must outlive its pages.
    profile = QWebEngineProfile("training-analyze", QApplication.instance())
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)

    def download(item) -> None:
        item.setDownloadDirectory(downloads)
        item.accept()
        window.statusBar().showMessage(f"Saved {item.downloadFileName()} to {downloads}", 8000)

    profile.downloadRequested.connect(download)
    view = QWebEngineView(window)
    view.setPage(QWebEnginePage(profile, view))
    view.load(QUrl(url))
    window.setCentralWidget(view)
    return window


def main() -> None:
    parser = argparse.ArgumentParser(description="Training Analyze desktop app")
    parser.add_argument("--server", default=os.environ.get("TA_SERVER_URL", ""),
                        help="open a Training Analyze server (e.g. http://192.168.88.10:8080) instead of local data")
    args, qt_args = parser.parse_known_args()
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_TITLE)
    app.setDesktopFileName("training-analyze")  # lets Linux docks match a .desktop launcher
    app.setWindowIcon(app_icon())
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(STYLESHEET)
    if args.server:
        window = server_window(args.server)
    else:
        init_bridge()
        window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
