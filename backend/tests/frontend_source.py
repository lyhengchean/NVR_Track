"""The web pages in frontend/ as one text each (HTML + CSS + JS), for tests
that check what a page shows or does."""
from nvr_track.core.paths import FRONTEND_DIR


def page_source(page: str) -> str:
    folder = FRONTEND_DIR / page
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(folder.iterdir()) if path.is_file())


DASHBOARD_UI = page_source("dashboard")
BUILDER_UI = page_source("report-builder")
