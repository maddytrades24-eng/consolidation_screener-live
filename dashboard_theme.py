"""Persisted dashboard theme presets and the requested Eng-style CSS layer."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
THEME_PATH = ROOT / "data" / "theme.json"
MODE_PATH = ROOT / "data" / "mode.txt"
CONFIG_PATH = ROOT / ".streamlit" / "config.toml"

PRESETS: dict[str, dict[str, str]] = {
    "Eng Dark": {
        "bg": "#171428",
        "panel": "#221E3A",
        "text": "#E6E4F5",
        "accent": "#E94B9B",
        "accent2": "#6C5DD3",
        "bullish": "#2ED8A3",
        "bearish": "#FF4D79",
    },
    "Eng Light": {
        "bg": "#F4F3FA",
        "panel": "#FBFBFE",
        "text": "#1B1A33",
        "accent": "#E0338A",
        "accent2": "#5B4BD1",
        "bullish": "#0E9F6E",
        "bearish": "#E11D48",
    },
    "Obsidian Neon": {
        "bg": "#07080A",
        "panel": "#101216",
        "text": "#E8EAED",
        "accent": "#00FF85",
        "accent2": "#00B35E",
        "bullish": "#00FF85",
        "bearish": "#FF1F4B",
    },
    "Light Crisp": {
        "bg": "#F3F5F8",
        "panel": "#FBFCFD",
        "text": "#0B0F14",
        "accent": "#0057D9",
        "accent2": "#3B7BFF",
        "bullish": "#008A3E",
        "bearish": "#C8102E",
    },
    "Slate Dark": {
        "bg": "#111827",
        "panel": "#1F2937",
        "text": "#E2E8F0",
        "accent": "#60A5FA",
        "accent2": "#818CF8",
        "bullish": "#34D399",
        "bearish": "#F87171",
    },
    "Midnight": {
        "bg": "#0B1020",
        "panel": "#141B2D",
        "text": "#D6DEEB",
        "accent": "#7AA2F7",
        "accent2": "#9D7CD8",
        "bullish": "#4ADE9E",
        "bearish": "#FF6B81",
    },
    "Charcoal": {
        "bg": "#1A1A1A",
        "panel": "#262626",
        "text": "#D4D4D4",
        "accent": "#E0A458",
        "accent2": "#C2863B",
        "bullish": "#6FCF97",
        "bearish": "#EB6B6B",
    },
    "Soft Light": {
        "bg": "#ECE9E1",
        "panel": "#F4F2EC",
        "text": "#2B2B2B",
        "accent": "#3B6FB6",
        "accent2": "#5A8FD6",
        "bullish": "#1B7F4B",
        "bearish": "#B3261E",
    },
}
COLOR_LABELS = {
    "bg": "Background",
    "panel": "Cards and sidebar",
    "text": "Text",
    "accent": "Primary accent",
    "accent2": "Secondary accent",
    "bullish": "Bullish",
    "bearish": "Bearish",
}
_WHITE_FALLBACKS = {
    "bg": "#ECE9E1",
    "panel": "#F4F2EC",
    "text": "#E6E6E6",
}
_COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\Z")


def default_preferences() -> dict[str, Any]:
    return {"preset": "Midnight", "custom": dict(PRESETS["Midnight"])}


def _validate_colors(colors: dict[str, Any]) -> dict[str, str]:
    missing = set(COLOR_LABELS).difference(colors)
    if missing:
        raise ValueError(f"Theme colors are missing: {sorted(missing)}")
    result: dict[str, str] = {}
    for key in COLOR_LABELS:
        value = colors[key]
        if not isinstance(value, str) or not _COLOR_PATTERN.fullmatch(value):
            raise ValueError(f"Theme color '{key}' must be a six-digit hex color.")
        result[key] = value.upper()
    return soften(result)


def load_preferences() -> dict[str, Any]:
    try:
        raw = THEME_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_preferences()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse {THEME_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Theme preferences in {THEME_PATH} must be an object.")
    preset = data.get("preset")
    if preset not in (*PRESETS, "Custom"):
        raise ValueError(f"Unknown theme preset in {THEME_PATH}: {preset!r}")
    custom = data.get("custom")
    if not isinstance(custom, dict):
        raise ValueError(f"Custom theme colors in {THEME_PATH} must be an object.")
    return {"preset": preset, "custom": _validate_colors(custom)}


def save_preferences(preset: str, custom: dict[str, Any]) -> None:
    if preset not in (*PRESETS, "Custom"):
        raise ValueError(f"Unknown theme preset: {preset!r}")
    colors = _validate_colors(custom)
    THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
    THEME_PATH.write_text(
        json.dumps({"preset": preset, "custom": colors}, indent=2),
        encoding="utf-8",
    )


def load_mode() -> str:
    try:
        mode = MODE_PATH.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return "dark"
    if mode not in {"dark", "light"}:
        raise ValueError(f"Theme mode in {MODE_PATH} must be 'dark' or 'light'.")
    return mode


def save_mode(mode: str) -> None:
    normalized = mode.strip().lower()
    if normalized not in {"dark", "light"}:
        raise ValueError("Theme mode must be 'dark' or 'light'.")
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODE_PATH.write_text(normalized, encoding="utf-8")


def write_streamlit_config(theme: dict[str, str], mode: str) -> None:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"dark", "light"}:
        raise ValueError("Theme mode must be 'dark' or 'light'.")
    colors = _validate_colors(theme)
    chart_colors = [
        colors["accent"],
        colors["accent2"],
        colors["bullish"],
        colors["bearish"],
        "#58A6FF",
        "#D7B5F8",
    ]
    config = (
        "[theme]\n"
        f'base = "{normalized_mode}"\n'
        f'primaryColor = "{colors["accent"]}"\n'
        f'backgroundColor = "{colors["bg"]}"\n'
        f'secondaryBackgroundColor = "{colors["panel"]}"\n'
        f'textColor = "{colors["text"]}"\n'
        f'borderColor = "{colors["text"]}33"\n'
        'font = "sans-serif"\n'
        f"chartCategoricalColors = {json.dumps(chart_colors)}\n"
    )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(config, encoding="utf-8")


def soften(theme: dict[str, str]) -> dict[str, str]:
    result = dict(theme)
    for key, fallback in _WHITE_FALLBACKS.items():
        if result[key].lower() in {"#fff", "#ffffff"}:
            result[key] = fallback
    return result


def _luminance(color: str) -> float:
    red, green, blue = (
        int(color[index : index + 2], 16) for index in (1, 3, 5)
    )
    return (0.299 * red + 0.587 * green + 0.114 * blue) / 255


def ink(color: str) -> str:
    return "#0B0B14" if _luminance(color) > 0.55 else "#FFFFFF"


def mode_theme(theme: dict[str, str], light: bool) -> dict[str, str]:
    is_light = _luminance(theme["bg"]) > 0.5
    if light and not is_light:
        return dict(PRESETS["Eng Light"])
    if not light and is_light:
        return dict(PRESETS["Eng Dark"])
    return dict(theme)


def css(theme: dict[str, str]) -> str:
    styles = """<style>
.block-container{max-width:100% !important;padding:1.2rem 1.4rem !important}
.stApp, [data-testid="stHeader"], [data-testid="stAppViewContainer"]{background:__BG__ !important;color:__TEXT__}
[data-testid="stSidebar"], [data-testid="stSidebar"] > div{background:__PANEL__ !important}
[data-testid="stSidebar"]{width:250px !important;min-width:250px !important;border-right:1px solid __BORDER__}
.stApp p, .stApp label, .stApp span, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
[data-testid="stMetricValue"], [data-testid="stMetricLabel"], .stCaption, [data-testid="stCaptionContainer"]{color:__TEXT__ !important}
h1{font-weight:800 !important;letter-spacing:-.01em}
h2, h3{font-weight:700 !important}
[data-testid="stMetric"]{background:__PANEL__;border:1px solid __BORDER__;border-radius:14px;padding:12px 16px}
[data-testid="stMetricValue"]{font-weight:800 !important;font-size:1.5rem}
[data-testid="stMetricLabel"]{font-weight:600;opacity:.8}
div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="base-input"], textarea{background:__PANEL__ !important;color:__TEXT__ !important;border-color:__BORDER__ !important;border-radius:10px !important}
input, textarea{color:__TEXT__ !important}
div[data-baseweb="popover"] ul, div[data-baseweb="popover"] li, [role="listbox"]{background:__PANEL__ !important;color:__TEXT__ !important}
.stButton > button, .stDownloadButton > button{background:linear-gradient(135deg,__ACCENT__,__A2__);border:0;border-radius:10px;font-weight:700}
.stButton > button p, .stDownloadButton > button p{color:__INK__ !important}
.stButton > button:hover, .stDownloadButton > button:hover{filter:brightness(1.1)}
button[data-baseweb="tab"]{color:__TEXT__;font-weight:700}
[data-baseweb="tab-highlight"]{background:__ACCENT__ !important}
details, [data-testid="stFileUploaderDropzone"]{background:__PANEL__ !important;border-color:__BORDER__ !important;border-radius:12px !important}
[data-testid="stDataFrame"]{border:1px solid __BORDER__;border-radius:14px}
div[role="radiogroup"]{gap:8px;flex-wrap:wrap}
div[role="radiogroup"] > label{border:1px solid __BORDER__;border-radius:999px;padding:4px 14px;background:__PANEL__;font-weight:700;margin:0}
div[role="radiogroup"] > label:has(input:checked){background:linear-gradient(135deg,__ACCENT__,__A2__);border-color:transparent}
div[role="radiogroup"] > label:has(input:checked) *{color:__INK__ !important}
div[role="radiogroup"] > label > div:first-child{display:none}
</style>"""
    replacements = {
        "__BG__": theme["bg"],
        "__PANEL__": theme["panel"],
        "__TEXT__": theme["text"],
        "__ACCENT__": theme["accent"],
        "__A2__": theme["accent2"],
        "__BORDER__": theme["text"] + "33",
        "__INK__": ink(theme["accent"]),
    }
    for placeholder, value in replacements.items():
        styles = styles.replace(placeholder, value)
    return styles
