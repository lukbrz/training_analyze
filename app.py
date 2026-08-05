from textual import work
from textual.app import App

import garmin_client
from screens import LoginScreen, MainScreen


class TrainingAnalyzeApp(App):
    TITLE = "Training Analyze"
    BINDINGS = [("q", "quit", "Quit")]

    def on_mount(self) -> None:
        self._try_cached_login()

    @work(thread=True)
    def _try_cached_login(self) -> None:
        garmin = garmin_client.try_cached_login()
        self.call_from_thread(self._on_cached_login_result, garmin)

    def _on_cached_login_result(self, garmin) -> None:
        if garmin is not None:
            self.push_screen(MainScreen(garmin))
        else:
            self.push_screen(LoginScreen())


if __name__ == "__main__":
    TrainingAnalyzeApp().run()
