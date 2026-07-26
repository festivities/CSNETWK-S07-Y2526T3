#!/usr/bin/env python3
"""Turn a Markdown file into a PDF, using the Python standard library only.

    python tools/md2pdf.py README.md README.pdf

This exists so that README.pdf -- a required deliverable -- can be regenerated
from README.md at any time.  It is not part of the MTGNP implementation.

The conversion happens in two steps:

  1. The Markdown is rendered to a single self-contained HTML file.  The renderer
     below covers the subset of Markdown the README actually uses: headings,
     paragraphs, lists, fenced code blocks, tables with column alignment, thematic
     breaks, and the inline forms (code, bold, italic, links).
  2. That HTML file is printed to PDF by a headless Chromium browser -- Microsoft
     Edge or Google Chrome, both of which ship with a PDF printer.  Nothing is
     installed and nothing is downloaded; we only drive a browser that is already
     on the machine.

Set the MD2PDF_BROWSER environment variable to force a particular browser
executable.  Pass --html-only to stop after step 1, which is useful when checking
the layout in a normal browser window.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Inline markup
# --------------------------------------------------------------------------- #

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")
_CODE_SPAN = re.compile(r"`([^`]+)`")


def _format_text(text: str) -> str:
    """Apply the inline forms to a run of text that holds no code span."""
    out = html.escape(text, quote=False)
    out = _LINK.sub(r'<a href="\2">\1</a>', out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return out


def inline(text: str) -> str:
    """Render inline Markdown.

    Code spans are taken out first and escaped verbatim, so that a `*` inside
    `code` is never mistaken for emphasis.  Each one leaves a NUL-delimited marker
    behind, which keeps the rest of the line in one piece -- bold that opens
    before a code span and closes after it still has to be seen as one run.
    """
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append("<code>" + html.escape(match.group(1), quote=False) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _format_text(_CODE_SPAN.sub(stash, text))
    for number, span in enumerate(spans):
        out = out.replace(f"\x00{number}\x00", span)
    return out


# --------------------------------------------------------------------------- #
# Block markup
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*```+\s*(\S*)\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _starts_block(line: str) -> bool:
    """True when a line begins a block, so it cannot be a lazy continuation."""
    return bool(
        not line.strip()
        or _HEADING.match(line)
        or _RULE.match(line)
        or _FENCE.match(line)
        or _ITEM.match(line)
        or line.lstrip().startswith(">")
    )


def _split_row(line: str) -> list[str]:
    """Split one table row into its cells, dropping the outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _alignments(rule: str) -> list[str]:
    aligns = []
    for cell in _split_row(rule):
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns


def _render_list(items: list[tuple[int, str, str]], index: int) -> tuple[str, int]:
    """Render items[index:] as one list, recursing for any deeper indent."""
    indent, tag, _ = items[index]
    parts = [f"<{tag}>"]
    while index < len(items) and items[index][0] >= indent:
        parts.append("<li>" + inline(items[index][2]))
        index += 1
        # Anything indented further belongs inside the item we just opened.
        while index < len(items) and items[index][0] > indent:
            nested, index = _render_list(items, index)
            parts.append(nested)
        parts.append("</li>")
    parts.append(f"</{tag}>")
    return "".join(parts), index


def render_markdown(text: str) -> str:
    """Render a Markdown document to an HTML fragment."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            language = fence.group(1)
            body = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # skip the closing fence
            css = f' class="lang-{html.escape(language, quote=True)}"' if language else ""
            out.append(f"<pre{css}><code>" + html.escape("\n".join(body), quote=False) + "</code></pre>")
            continue

        # A table is a header row whose next line is the alignment rule.
        if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            headers = _split_row(line)
            aligns = _alignments(lines[i + 1])
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1

            def cell(tag: str, value: str, column: int) -> str:
                align = aligns[column] if column < len(aligns) else "left"
                return f'<{tag} style="text-align:{align}">{inline(value)}</{tag}>'

            table = ["<table><thead><tr>"]
            table += [cell("th", h, n) for n, h in enumerate(headers)]
            table.append("</tr></thead><tbody>")
            for row in rows:
                table.append("<tr>")
                table += [cell("td", value, n) for n, value in enumerate(row)]
                table.append("</tr>")
            table.append("</tbody></table>")
            out.append("".join(table))
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if _RULE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if _ITEM.match(line):
            items: list[tuple[int, str, str]] = []
            while i < len(lines):
                item = _ITEM.match(lines[i])
                if item:
                    depth = len(item.group(1).expandtabs(4))
                    tag = "ol" if item.group(2)[0].isdigit() else "ul"
                    items.append((depth, tag, item.group(3)))
                elif items and not _starts_block(lines[i]):
                    # A wrapped line continues the item above it.
                    depth, tag, body = items[-1]
                    items[-1] = (depth, tag, body + " " + lines[i].strip())
                else:
                    break
                i += 1
            rendered, _ = _render_list(items, 0)
            out.append(rendered)
            continue

        # Anything left is a paragraph: gather until a blank line or a new block.
        body = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            if "|" in lines[i] and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
                break
            body.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(body)) + "</p>")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Page template
# --------------------------------------------------------------------------- #

STYLESHEET = """
@page { size: A4; margin: 18mm 16mm; }
body {
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.55; color: #16191d; margin: 0;
}
h1, h2, h3, h4 { line-height: 1.25; margin: 1.4em 0 0.5em; page-break-after: avoid; }
h1 { font-size: 20pt; margin-top: 0; }
h2 { font-size: 14pt; border-bottom: 1px solid #d5d9de; padding-bottom: 0.2em; }
h3 { font-size: 11.5pt; }
h4 { font-size: 10.5pt; }
p, ul, ol, table, pre { margin: 0 0 0.75em; }
ul, ol { padding-left: 1.5em; }
li { margin: 0.2em 0; }
li > ul, li > ol { margin: 0.2em 0 0.2em; }
a { color: #12457a; text-decoration: none; }
code {
  font-family: Consolas, "SF Mono", "DejaVu Sans Mono", monospace;
  font-size: 0.9em; background: #f2f3f5; border: 1px solid #e2e5e9;
  border-radius: 3px; padding: 0 3px;
}
pre {
  background: #f7f8f9; border: 1px solid #e2e5e9; border-radius: 4px;
  padding: 0.7em 0.9em; overflow-x: auto; page-break-inside: avoid;
}
pre code { background: none; border: none; padding: 0; font-size: 0.88em; }
table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
th, td { border: 1px solid #d5d9de; padding: 0.35em 0.6em; vertical-align: top; }
th { background: #f2f3f5; text-align: left; }
tr { page-break-inside: avoid; }
hr { border: none; border-top: 1px solid #d5d9de; margin: 1.6em 0; }
strong { font-weight: 600; }
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def build_html(markdown_text: str, title: str) -> str:
    return PAGE.format(title=html.escape(title, quote=False),
                       css=STYLESHEET,
                       body=render_markdown(markdown_text))


# --------------------------------------------------------------------------- #
# Printing
# --------------------------------------------------------------------------- #

def find_browser() -> str | None:
    """Locate a Chromium-based browser that can print to PDF."""
    forced = os.environ.get("MD2PDF_BROWSER")
    if forced:
        return forced if Path(forced).exists() or shutil.which(forced) else None

    program_files = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LocalAppData", ""),
    ]
    suffixes = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"Chromium\Application\chrome.exe",
    ]
    for root in program_files:
        for suffix in suffixes:
            if root:
                candidate = Path(root) / suffix
                if candidate.exists():
                    return str(candidate)

    # macOS and Linux, plus anything already on PATH.
    for candidate in (
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(candidate).exists():
            return candidate
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def print_to_pdf(browser: str, html_path: Path, pdf_path: Path) -> None:
    """Drive the browser in headless mode to print the HTML file."""
    if pdf_path.exists():
        pdf_path.unlink()

    # A throwaway profile keeps this from clashing with a browser the user already
    # has open, which would otherwise make the headless run exit immediately.
    with tempfile.TemporaryDirectory(prefix="md2pdf-") as profile:
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",   # current flag name
            "--print-to-pdf-no-header",  # older builds; unknown switches are ignored
            "--virtual-time-budget=5000",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True)

    if not pdf_path.exists():
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "the browser did not produce a PDF"
            + (f"\n{detail}" if detail else "")
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert Markdown to PDF.")
    parser.add_argument("source", nargs="?", default="README.md",
                        help="the Markdown file to convert (default: README.md)")
    parser.add_argument("output", nargs="?", default=None,
                        help="the PDF to write (default: the source with a .pdf suffix)")
    parser.add_argument("--html-only", action="store_true",
                        help="write the intermediate HTML next to the output and stop")
    parser.add_argument("--keep-html", action="store_true",
                        help="also keep the intermediate HTML file")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    if not source.is_file():
        print(f"md2pdf: cannot find {source}", file=sys.stderr)
        return 1

    output = Path(args.output).resolve() if args.output else source.with_suffix(".pdf")
    title = source.stem
    page = build_html(source.read_text(encoding="utf-8"), title)

    html_path = output.with_suffix(".html")
    html_path.write_text(page, encoding="utf-8")

    if args.html_only:
        print(f"md2pdf: wrote {html_path}")
        return 0

    browser = find_browser()
    if browser is None:
        print("md2pdf: no Chromium-based browser was found.\n"
              "        Install Microsoft Edge or Google Chrome, or set MD2PDF_BROWSER\n"
              f"        to the path of one.  The HTML is at {html_path}.",
              file=sys.stderr)
        return 2

    try:
        print_to_pdf(browser, html_path, output)
    except RuntimeError as error:
        print(f"md2pdf: {error}", file=sys.stderr)
        return 3
    finally:
        if not args.keep_html:
            html_path.unlink(missing_ok=True)

    print(f"md2pdf: wrote {output} ({output.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
