"""CSV cells that spreadsheet programs open as plain text, never as formulas."""
from __future__ import annotations

import re

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
NUMBER = re.compile(r"-?\d+(\.\d+)?")


def csv_safe(text: str) -> str:
    """Prefix ' to text a spreadsheet would run as a formula (same rule as csvCell in dashboard.js).

    Numbers (also negative) and empty cells stay unchanged. The old check,
    ``c[:1] in "=+-@"``, also matched "" and turned every empty cell into a lone "'".
    """
    if text.startswith(FORMULA_START) and not NUMBER.fullmatch(text):
        return "'" + text
    return text
