"""Render a card's plan artifacts into one Notion-style HTML page.

Produced when a card reaches its pipeline's Plan gate (see Board.move_card): the human opens
`plan.html` to review the finished plan as a clean web page instead of raw markdown. The
plan's `## Steps` list is rendered as numbered Notion-style blocks, so the steps that will be
taken read as a checklist rather than a paragraph.

Dependency-free by design — the kanban ships no markdown library, so this carries a small
renderer that covers exactly the subset the plan workers emit: headings, ordered/unordered
lists (one level of nesting), tables, fenced code, blockquotes, horizontal rules, and inline
bold / italic / code / links. Anything it doesn't recognise falls through as a paragraph, so
an unusual line degrades to plain text rather than breaking the page.
"""
from __future__ import annotations

import html
import os
import re

# The plan artifacts, in reading order, mapped to their section titles. Only the ones that
# exist on disk are rendered — a card that skipped a step just omits that section.
PLAN_ARTIFACTS = [
    ("IDEA.md", "Idea"),  # plan pipeline intake
    ("REPORT.md", "Report"),  # maintenance pipeline intake
    ("PLAN.md", "Plan"),  # the plan itself — its "## Steps" render as numbered blocks
]

# The source files whose arrival at the Plan gate should (re)generate plan.html. This is
# exactly the set above; naming it lets the board trigger regeneration without importing the
# tuples, and keeps plan.html itself off the list so setting it never recurses.
PLAN_SOURCE_ARTIFACTS = frozenset(name for name, _title in PLAN_ARTIFACTS)

# ---------------------------------------------------------------------------
# Markdown -> HTML (dependency-free; the subset our plan artifacts use)
# ---------------------------------------------------------------------------
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])[*_]([^*_\n]+)[*_](?![\w*])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """Escape a line, then apply inline markdown. Code spans are protected first so their
    contents are never treated as markup."""
    spans: list[str] = []

    def _stash(m):
        spans.append("<code>" + html.escape(m.group(1)) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_SPAN.sub(_stash, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def render_markdown(md: str) -> str:
    """Render a markdown string to an HTML fragment.

    One special case beyond plain markdown: the ordered list directly under a `## Steps`
    heading is rendered as numbered *blocks* (see `_render_step_blocks`) rather than a plain
    `<ol>`, so the plan's steps read as discrete cards — the "blocks" in the Notion-style page.
    """
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    steps_next = False  # the previous heading was "Steps" — render its list as blocks

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # blank
        if not stripped:
            i += 1
            continue

        # fenced code
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        # horizontal rule
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr />")
            i += 1
            continue

        # heading
        h = re.match(r"(#{1,6})\s+(.*)", stripped)
        if h:
            lvl = len(h.group(1))
            title = h.group(2).strip()
            out.append(f"<h{lvl}>{_inline(title)}</h{lvl}>")
            steps_next = title.lower().rstrip(":") == "steps"
            i += 1
            continue

        # table: header row of pipes followed by a |---| separator
        if "|" in stripped and i + 1 < n and re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[i + 1]) and "-" in lines[i + 1]:
            def cells(row):
                row = row.strip().strip("|")
                return [c.strip() for c in row.split("|")]

            header = cells(lines[i])
            i += 2  # header + separator
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue

        # blockquote (consecutive > lines)
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + _inline(" ".join(buf)) + "</blockquote>")
            continue

        # list (unordered or ordered), with one level of nesting by indent
        if re.match(r"[-*+]\s+", stripped) or re.match(r"\d+\.\s+", stripped):
            ordered = bool(re.match(r"\d+\.\s+", stripped))
            if steps_next and ordered:
                i, block = _render_step_blocks(lines, i)
            else:
                i, block = _render_list(lines, i)
            out.append(block)
            steps_next = False
            continue

        # paragraph: gather consecutive non-blank, non-structural lines
        steps_next = False
        buf = [stripped]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")

    return "\n".join(out)


def _starts_block(s: str) -> bool:
    return (
        s.startswith("```")
        or s.startswith("#")
        or s.startswith(">")
        or bool(re.match(r"[-*+]\s+", s))
        or bool(re.match(r"\d+\.\s+", s))
        or bool(re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s))
    )


def _render_list(lines: list[str], i: int) -> tuple[int, str]:
    """Render a list starting at line i. Handles one level of nested indentation."""
    n = len(lines)
    base = _indent(lines[i])
    ordered = bool(re.match(r"\s*\d+\.\s+", lines[i]))
    tag = "ol" if ordered else "ul"
    items: list[str] = []
    cur: str | None = None
    nested: list[str] = []

    def flush():
        nonlocal cur, nested
        if cur is None:
            return
        inner = ""
        if nested:
            _, sub = _render_list(nested, 0)
            inner = sub
        items.append(f"<li>{cur}{inner}</li>")
        cur, nested = None, []

    while i < n:
        raw = lines[i]
        if not raw.strip():
            # blank inside a list — peek: if the next non-blank is still list at >= base, continue
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _indent(lines[j]) >= base and (re.match(r"\s*[-*+]\s+", lines[j]) or re.match(r"\s*\d+\.\s+", lines[j])):
                i = j
                continue
            break
        ind = _indent(raw)
        m = re.match(r"\s*(?:[-*+]|\d+\.)\s+(.*)", raw)
        if not m or ind < base:
            break
        if ind > base:
            nested.append(raw)
            i += 1
            continue
        flush()
        cur = _inline(m.group(1).strip())
        i += 1
    flush()
    return i, f"<{tag}>" + "".join(items) + f"</{tag}>"


def _render_step_blocks(lines: list[str], i: int) -> tuple[int, str]:
    """Render the ordered list under a `## Steps` heading as numbered blocks.

    Each `N. **title** — detail` item becomes its own card with a number badge, so the plan's
    steps read as a checklist of discrete blocks. Nested bullets under a step become its body.
    """
    n = len(lines)
    base = _indent(lines[i])
    blocks: list[str] = []
    num = 0
    cur: str | None = None
    nested: list[str] = []

    def flush():
        nonlocal cur, nested, num
        if cur is None:
            return
        num += 1
        body = ""
        if nested:
            _, sub = _render_list(nested, 0)
            body = f'<div class="step-more">{sub}</div>'
        blocks.append(
            f'<div class="step"><div class="step-n">{num}</div>'
            f'<div class="step-body">{cur}{body}</div></div>'
        )
        cur, nested = None, []

    while i < n:
        raw = lines[i]
        if not raw.strip():
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _indent(lines[j]) >= base and re.match(r"\s*\d+\.\s+", lines[j]):
                i = j
                continue
            break
        ind = _indent(raw)
        m = re.match(r"\s*\d+\.\s+(.*)", raw)
        if ind < base or (not m and ind == base):
            break
        if ind > base or not m:
            nested.append(raw)
            i += 1
            continue
        flush()
        cur = _inline(m.group(1).strip())
        i += 1
    flush()
    return i, '<div class="steps">' + "".join(blocks) + "</div>"


# ---------------------------------------------------------------------------
# Compose the page
# ---------------------------------------------------------------------------
def build_plan_html(card_title: str, sections: list[tuple[str, str]]) -> str:
    """Wrap rendered sections (list of (title, html)) in a Notion-style page."""
    slug = re.sub(r"[^a-z0-9]+", "-", (card_title or "plan").lower()).strip("-")[:60] or "plan"
    nav = "\n".join(
        f'<a href="#{slug}-{k}">{html.escape(t)}</a>' for k, (t, _) in enumerate(sections)
    )
    body = "\n".join(
        f'<section id="{slug}-{k}"><h2 class="sec">{html.escape(t)}</h2>{h}</section>'
        for k, (t, h) in enumerate(sections)
    )
    title = html.escape(card_title or "Plan")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} — plan</title>
<style>
  :root{{ --text:#37352f; --muted:#787066; --line:#e9e7e2; --bg:#ffffff; --panel:#f7f6f3;
    --accent:#2f6fed; --code:#f1f0ee; --codetext:#3b3a37; }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--text);
    font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased}}
  .wrap{{max-width:820px;margin:0 auto;padding:56px 28px 140px}}
  .eyebrow{{color:var(--muted);font-size:13px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;margin:0 0 6px}}
  h1{{font-size:40px;line-height:1.15;letter-spacing:-.02em;margin:0 0 8px;font-weight:700}}
  .meta{{color:var(--muted);font-size:14px;margin:0 0 28px;padding-bottom:20px;border-bottom:1px solid var(--line)}}
  nav.toc{{position:sticky;top:0;background:var(--bg);padding:10px 0;margin:0 0 20px;display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--line);z-index:5}}
  nav.toc a{{font-size:13px;color:var(--muted);text-decoration:none;padding:4px 10px;border-radius:6px}}
  nav.toc a:hover{{background:var(--panel);color:var(--text)}}
  section{{margin:0 0 10px}}
  h2.sec{{font-size:15px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
    font-weight:700;margin:40px 0 10px;padding-top:14px}}
  h1,h2,h3,h4{{color:var(--text)}}
  section h1{{font-size:26px;margin:22px 0 8px}}
  section h2{{font-size:22px;margin:26px 0 8px;letter-spacing:-.01em;text-transform:none;color:var(--text)}}
  section h3{{font-size:18px;margin:22px 0 6px}}
  section h4{{font-size:15px;margin:18px 0 4px;color:var(--muted)}}
  p{{margin:10px 0}}
  a{{color:var(--accent);text-decoration:none}} a:hover{{text-decoration:underline}}
  ul,ol{{margin:8px 0;padding-left:26px}} li{{margin:4px 0}}
  li>ul,li>ol{{margin:4px 0}}
  code{{background:var(--code);color:var(--codetext);padding:2px 6px;border-radius:5px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13.5px}}
  pre{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;
    overflow-x:auto;margin:14px 0}}
  pre code{{background:none;padding:0;font-size:13px;line-height:1.6;color:var(--codetext)}}
  blockquote{{margin:14px 0;padding:2px 16px;border-left:3px solid var(--line);color:var(--muted)}}
  hr{{border:0;border-top:1px solid var(--line);margin:26px 0}}
  table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px;display:block;overflow-x:auto}}
  th,td{{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}}
  th{{background:var(--panel);font-weight:600}}
  strong{{font-weight:650}}
  /* Steps rendered as Notion-style numbered blocks */
  .steps{{display:flex;flex-direction:column;gap:10px;margin:16px 0}}
  .step{{display:flex;gap:14px;align-items:flex-start;background:var(--panel);
    border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
  .step-n{{flex:0 0 auto;width:26px;height:26px;border-radius:7px;background:var(--accent);
    color:#fff;font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center}}
  .step-body{{flex:1 1 auto;min-width:0}}
  .step-body>strong:first-child{{display:inline}}
  .step-more{{margin-top:6px;color:var(--muted)}}
  .step-more ul,.step-more ol{{margin:4px 0}}
  @media(prefers-color-scheme:dark){{
    :root{{--text:#e6e4df;--muted:#9b968c;--line:#2c2a26;--bg:#191919;--panel:#242220;--code:#2b2926;--codetext:#d9d5cd;--accent:#7aa2ff}}
  }}
</style></head>
<body><div class="wrap">
  <p class="eyebrow">Plan</p>
  <h1>{title}</h1>
  <p class="meta">Generated from the card's plan artifacts when it reached the <b>Plan</b> gate.</p>
  <nav class="toc">{nav}</nav>
  {body}
</div></body></html>"""


def write_plan_doc(abs_dir: str, card_title: str) -> str | None:
    """Read the plan artifacts in `abs_dir`, render them, and write `plan.html` there.

    Returns the path written, or None if there were no plan artifacts to render (so a card
    with nothing planned doesn't get an empty page). Never raises for a render problem — a
    bad artifact degrades to plain text via the renderer.
    """
    sections: list[tuple[str, str]] = []
    for fname, title in PLAN_ARTIFACTS:
        p = os.path.join(abs_dir, fname)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if text.strip():
            sections.append((title, render_markdown(text)))
    if not sections:
        return None
    out = os.path.join(abs_dir, "plan.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_plan_html(card_title, sections))
    return out
