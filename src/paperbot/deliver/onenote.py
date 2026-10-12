"""Deliver a digest as a page in the OneNote desktop app (Windows), which then syncs it.

Why the desktop app and not Microsoft Graph: Graph's OneNote API needs a registered Entra
app with delegated user consent (app-only auth was retired 31 Mar 2025), and university
tenants often block user consent. The desktop app is already signed in to the notebook's
account, so its local COM API can write pages with no extra auth; OneNote's own sync then
carries the page to every device.

Split in two so the conversion is testable anywhere:
- `markdown_to_page_xml` turns digest Markdown into OneNote 2013 page XML (pure Python).
- `onenote_push.ps1` (next to this file) does the COM calls (find/create section, create page, fill it).
  PowerShell's COM binding handles OneNote's [out] parameters reliably; pywin32 is flaky here.
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

NS = "http://schemas.microsoft.com/office/onenote/2013/onenote"
PAGE_ID_PLACEHOLDER = "__PAGE_ID__"

# OneNote recognises these quick-style names as its built-in heading styles.
_STYLES = [
    ("PageTitle", 'font="Calibri Light" fontSize="20.0" spaceBefore="0.0" spaceAfter="0.0"'),
    ("h1", 'font="Calibri Light" fontSize="16.0" fontColor="#1e4e79" spaceBefore="0.0" spaceAfter="0.0"'),
    ("h2", 'font="Calibri" fontSize="14.0" fontColor="#2e75b5" spaceBefore="0.0" spaceAfter="0.0"'),
    ("h3", 'font="Calibri" fontSize="12.0" fontColor="#1f3763" bold="true" spaceBefore="0.0" spaceAfter="0.0"'),
    ("p", 'font="Calibri" fontSize="11.0" spaceBefore="0.0" spaceAfter="0.0"'),
]
_STYLE_INDEX = {name: i for i, (name, _) in enumerate(_STYLES)}


class OneNoteError(RuntimeError):
    pass


def _inline(text: str) -> str:
    """Markdown inline -> the small HTML subset OneNote accepts inside <one:T> CDATA."""
    s = html.escape(text, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', s)
    s = re.sub(r"`([^`]+)`", r'<span style="font-family:Consolas">\1</span>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", s)
    return s


def _cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _oe(text_html: str, style: str = "p", bullet: bool = False) -> str:
    lst = '<one:List><one:Bullet bullet="2" /></one:List>' if bullet else ""
    return f'<one:OE quickStyleIndex="{_STYLE_INDEX[style]}">{lst}<one:T>{_cdata(text_html)}</one:T></one:OE>'


def markdown_to_page_xml(md: str, title: str) -> str:
    """Convert a paperbot digest to OneNote page XML. The page ID is left as a placeholder
    for the push script to fill in after it creates the page."""
    body: list[str] = []
    lines = md.splitlines()
    if lines and lines[0].startswith("# "):  # the H1 becomes the page title
        lines = lines[1:]
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or re.fullmatch(r"-{3,}", line.strip()):
            continue
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            body.append(_oe(_inline(m.group(2)), f"h{len(m.group(1))}"))
        elif re.match(r"\s*[-*]\s+", line) and not line.lstrip().startswith("**"):
            body.append(_oe(_inline(re.sub(r"^\s*[-*]\s+", "", line)), bullet=True))
        else:
            body.append(_oe(_inline(line.strip())))
    styles = "".join(f'<one:QuickStyleDef index="{i}" name="{n}" {attrs} />' for i, (n, attrs) in enumerate(_STYLES))
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<one:Page xmlns:one="{NS}" ID="{PAGE_ID_PLACEHOLDER}">{styles}'
        f'<one:Title><one:OE quickStyleIndex="0"><one:T>{_cdata(html.escape(title, quote=False))}</one:T></one:OE></one:Title>'
        f'<one:Outline><one:OEChildren>{"".join(body)}</one:OEChildren></one:Outline>'
        f"</one:Page>"
    )


def push_page(xml: str, notebook: str, section: str, script: Path,
              run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Create the page via the PowerShell script. Returns the new page's OneNote ID."""
    if sys.platform != "win32" and run is subprocess.run:
        raise OneNoteError("OneNote desktop delivery only works on Windows")
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml)
        xml_path = f.name
    try:
        proc = run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-Notebook", notebook, "-Section", section, "-XmlFile", xml_path],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
    finally:
        Path(xml_path).unlink(missing_ok=True)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise OneNoteError(f"onenote_push.ps1 failed ({proc.returncode}): {(proc.stderr or out).strip()[:500]}")
    m = re.search(r"PAGE_ID=(\S+)", out)
    if not m:
        raise OneNoteError(f"onenote_push.ps1 gave no page id: {out[:300]}")
    return m.group(1)
