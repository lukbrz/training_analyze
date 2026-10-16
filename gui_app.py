"""Native desktop GUI (Qt / PySide6) mirroring the Textual TUI (app.py): the same screens in
the same order with the same keys, drawn with real widgets and vector charts. Run with
run_gui.sh. All behaviour is shared with the TUI (see gui_pages.py)."""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QStackedWidget, QStyleFactory, QVBoxLayout, QWidget

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
        self.footer = QLabel()
        self.footer.setObjectName("footer")
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
        keys = "   ".join(
            f"<span style='background:{COLORS['surface2']}; color:{COLORS['accent']}; font-weight:600'>&nbsp;{key}&nbsp;</span> {text}"
            for key, text in page.bindings
        )
        self.footer.setText(keys)
        page.on_shown()
        target = page.focus_widget()
        if target is not None:
            target.setFocus()

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


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(STYLESHEET)
    init_bridge()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
