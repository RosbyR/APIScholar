"""Digest -> PDF with a headless Chromium browser (Chrome or Edge; Edge ships with Windows).

The digest is rendered to a standalone HTML file with the site's stylesheet inlined and
written to a temp folder, then printed with `--print-to-pdf`. Nothing is fetched over the
network and no cookies are involved. The browser gets its own throwaway profile so it never
attaches to a Chrome window Roz has open. Print rules live in style.css (`@media print`):
light theme, no top bar or buttons, cards kept whole where possible.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import ASSETS, DigestPage

CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
NAMES = ["google-chrome", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge"]


class PdfError(RuntimeError):
    pass


def find_browser() -> str | None:
    """$PAPERBOT_CHROME, then the usual install locations, then PATH."""
    env = os.environ.get("PAPERBOT_CHROME")
    if env and Path(env).is_file():
        return env
    for c in CANDIDATES:
        p = Path(os.path.expandvars(c))
        if p.is_file():
            return str(p)
    for n in NAMES:
        found = shutil.which(n)
        if found:
            return found
    return None


def print_html(page: DigestPage) -> str:
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%b %d, %Y %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="light">
<title>{html.escape(page.title)}</title><style>{css}</style></head>
<body class="print"><main>{page.body}
<p class="print-footer">Exported from paperbot · {html.escape(stamp)}</p></main></body></html>"""


def render_pdf(page: DigestPage, browser: str | None = None,
               run: Callable = subprocess.run, timeout_s: float = 90) -> bytes:
    browser = browser or find_browser()
    if not browser:
        raise PdfError("No Chrome or Edge found. Install one, or set PAPERBOT_CHROME to its path.")
    with tempfile.TemporaryDirectory(prefix="paperbot-pdf-") as tmp:
        src, out = Path(tmp) / "digest.html", Path(tmp) / "digest.pdf"
        src.write_text(print_html(page), encoding="utf-8")
        args = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={Path(tmp) / 'profile'}", "--blink-settings=preferredColorScheme=1",
                "--no-pdf-header-footer", f"--print-to-pdf={out}", src.as_uri()]
        try:
            run(args, check=False, capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as e:
            raise PdfError(f"the browser took longer than {timeout_s:.0f} s") from e
        if not out.is_file() or out.stat().st_size == 0:
            raise PdfError("the browser didn't produce a PDF")
        return out.read_bytes()
