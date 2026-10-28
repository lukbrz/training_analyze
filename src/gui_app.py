"""Native desktop GUI (Qt / PySide6) mirroring the Textual TUI (app.py): the same screens in
the same order with the same keys, drawn with real widgets and vector charts. Run with
run_gui.sh. All behaviour is shared with the TUI (see gui_pages.py)."""

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


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setDesktopFileName("training-analyze")  # lets Linux docks match a .desktop launcher
    app.setWindowIcon(app_icon())
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(STYLESHEET)
    init_bridge()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
