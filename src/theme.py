"""Colour palette (Tokyo Night) shared by the Qt and web GUIs. Plain data, no toolkit imports."""

COLORS = {
    "bg": "#1a1b26", "bg_dark": "#16161e", "surface": "#1f2335", "surface2": "#24283b",
    "border": "#292e42", "border2": "#3b4261", "fg": "#c0caf5", "muted": "#565f89",
    "comment": "#737aa2", "accent": "#7aa2f7", "green": "#9ece6a", "red": "#f7768e",
    "yellow": "#e0af68", "cyan": "#7dcfff", "magenta": "#bb9af7", "orange": "#ff9e64",
    "selection": "#2e3c64",
}
SERIES_COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#ff9e64", "#f7768e", "#73daca"]
SPORT_COLORS = {
    "running": COLORS["green"], "cycling": COLORS["accent"], "swimming": COLORS["cyan"],
    "training": COLORS["magenta"], "strength_training": COLORS["magenta"],
    "multisport": COLORS["orange"], "hiking": COLORS["yellow"],
}
