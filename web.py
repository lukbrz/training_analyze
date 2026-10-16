"""Browser GUI: serves the exact same Textual app (app.py) in a web page via textual-serve,
with its own theme. Every browser tab runs its own app process; they share the data folder
and the cached Garmin session like separate terminal launches would.

Bound to localhost only on purpose — the app holds a logged-in Garmin session, so it must
not be reachable from the network.
"""

import argparse
import shlex
import sys
import webbrowser
from pathlib import Path

from textual_serve.server import Server

APP_PATH = Path(__file__).parent / "app.py"
WEB_THEME = "tokyo-night"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Training Analyze in the browser")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--theme", default=WEB_THEME)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    command = shlex.join([sys.executable, str(APP_PATH), "--theme", args.theme])
    server = Server(command, host="localhost", port=args.port, title="Training Analyze")
    if not args.no_browser:
        webbrowser.open(server.public_url)
    server.serve()


if __name__ == "__main__":
    main()
