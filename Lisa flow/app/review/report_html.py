"""The report as a page you would actually open to decide what to fix next.

Deliberately the same visual language as the implementation plan — one system, two documents.
`render(rep)` returns the page CONTENT (style + markup, no document wrapper) so it can be
published as an artifact directly; `standalone(inner)` wraps it in a minimal document for
writing to a file you can double-click.

Everything is inline: no CDN, no font URL, no external asset. Both colour themes are defined at
the token level, and the viewer's theme toggle stamps data-theme on the root, which must win over
the media query in both directions."""
from __future__ import annotations

from html import escape
from typing import Any

CSS = """
:root{--ground:#F3F5F3;--surface:#FFF;--surface-2:#EDF0EE;--ink:#17201C;--ink-2:#3C4842;
--muted:#6E7B74;--faint:#9BA7A0;--rule:#DDE3DF;--rule-strong:#C6D0CA;--accent:#1E5B4F;
--accent-soft:#DDEAE5;--pass:#2E7D5B;--pass-bg:#E2F0E8;--gap:#A9640F;--gap-bg:#F7EBDA;
--crit:#A33A2E;--crit-bg:#F7E3E0;--code-bg:#1B2420;--code-ink:#D6E0DA;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--ground:#111614;--surface:#181F1C;--surface-2:#1F2723;
--ink:#E7ECE9;--ink-2:#C2CCC7;--muted:#8E9C95;--faint:#6B7973;--rule:#2A332E;--rule-strong:#3A453F;
--accent:#63C4AC;--accent-soft:#1C2E2A;--pass:#66C295;--pass-bg:#172B22;--gap:#D9A24F;
--gap-bg:#2E2519;--crit:#DE7565;--crit-bg:#2E1B18;--code-bg:#0C100E;--code-ink:#CBD6D0}}
:root[data-theme=dark]{--ground:#111614;--surface:#181F1C;--surface-2:#1F2723;--ink:#E7ECE9;
--ink-2:#C2CCC7;--muted:#8E9C95;--faint:#6B7973;--rule:#2A332E;--rule-strong:#3A453F;
--accent:#63C4AC;--accent-soft:#1C2E2A;--pass:#66C295;--pass-bg:#172B22;--gap:#D9A24F;
--gap-bg:#2E2519;--crit:#DE7565;--crit-bg:#2E1B18;--code-bg:#0C100E;--code-ink:#CBD6D0}
:root[data-theme=light]{--ground:#F3F5F3;--surface:#FFF;--surface-2:#EDF0EE;--ink:#17201C;
--ink-2:#3C4842;--muted:#6E7B74;--faint:#9BA7A0;--rule:#DDE3DF;--rule-strong:#C6D0CA;
--accent:#1E5B4F;--accent-soft:#DDEAE5;--pass:#2E7D5B;--pass-bg:#E2F0E8;--gap:#A9640F;
--gap-bg:#F7EBDA;--crit:#A33A2E;--crit-bg:#F7E3E0;--code-bg:#1B2420;--code-ink:#D6E0DA}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink-2);font-family:var(--sans);
font-size:16px;line-height:1.68;-webkit-font-smoothing:antialiased}
.shell{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;height:100vh;overflow-y:auto;background:var(--surface);
border-right:1px solid var(--rule);padding:32px 18px 44px;display:flex;flex-direction:column;gap:24px}
.brand .eyebrow{font-size:.64rem;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);font-weight:650}
.brand .name{font-family:var(--serif);font-size:1.2rem;color:var(--ink);line-height:1.25;display:block}
.brand .meta{font-size:.72rem;color:var(--faint);font-variant-numeric:tabular-nums}
nav{display:flex;flex-direction:column;gap:2px}
nav a{display:block;padding:5px 9px;border-radius:5px;color:var(--muted);text-decoration:none;
font-size:.85rem;border-left:2px solid transparent}
nav a:hover{color:var(--ink);background:var(--surface-2)}
nav a.on{color:var(--accent);background:var(--accent-soft);border-left-color:var(--accent);font-weight:600}
nav a:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
main{padding:54px 52px 110px;max-width:920px}
h1{font-family:var(--serif);font-weight:600;font-size:2.35rem;line-height:1.14;color:var(--ink);
margin:0 0 12px;letter-spacing:-.012em;text-wrap:balance}
h2{font-family:var(--serif);font-weight:600;font-size:1.5rem;color:var(--ink);margin:0 0 6px;
letter-spacing:-.008em;text-wrap:balance}
h3{font-size:1rem;font-weight:680;color:var(--ink);margin:28px 0 8px}
p{margin:0 0 14px;max-width:66ch}
strong{color:var(--ink);font-weight:650}
ul{margin:0 0 14px;padding-left:20px;max-width:66ch}
li{margin-bottom:6px}li::marker{color:var(--faint)}
code{font-family:var(--mono);font-size:.85em;background:var(--surface-2);color:var(--ink);
padding:1px 5px;border-radius:4px;border:1px solid var(--rule)}
.lede{font-family:var(--serif);font-size:1.13rem;line-height:1.6;color:var(--ink-2);
max-width:60ch;margin-bottom:24px}
section{padding-top:54px;scroll-margin-top:20px}section:first-of-type{padding-top:0}
.sechead{margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid var(--rule)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(116px,1fr));gap:1px;
background:var(--rule);border:1px solid var(--rule);border-radius:9px;overflow:hidden;margin:0 0 24px}
.stat{background:var(--surface);padding:14px 16px;display:flex;flex-direction:column;gap:2px}
.stat b{font-family:var(--serif);font-size:1.66rem;color:var(--ink);font-weight:600;
font-variant-numeric:tabular-nums;line-height:1}
.stat span{font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);font-weight:640}
.stat.good b{color:var(--pass)}.stat.warn b{color:var(--gap)}.stat.bad b{color:var(--crit)}
.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:9px;margin:0 0 18px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.845rem;min-width:520px}
th{text-align:left;font-size:.65rem;letter-spacing:.11em;text-transform:uppercase;color:var(--faint);
font-weight:680;padding:11px 14px;border-bottom:1px solid var(--rule-strong);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--rule);vertical-align:top;color:var(--ink-2)}
tbody tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;font-size:.67rem;font-weight:680;padding:2px 8px;border-radius:20px;
letter-spacing:.04em;white-space:nowrap;border:1px solid transparent}
.chip.pass{background:var(--pass-bg);color:var(--pass)}
.chip.gapc{background:var(--gap-bg);color:var(--gap)}
.chip.crit{background:var(--crit-bg);color:var(--crit)}
.chip.neu{background:var(--surface-2);color:var(--muted);border-color:var(--rule)}
.bar{display:block;height:5px;border-radius:3px;background:var(--accent);opacity:.8;min-width:2px}
.bar.maj{background:var(--crit)}
.note{border-left:3px solid var(--accent);background:var(--accent-soft);padding:12px 16px;
border-radius:0 7px 7px 0;margin:0 0 18px;font-size:.88rem}
.note.warn{border-left-color:var(--gap);background:var(--gap-bg)}
.note>:last-child{margin-bottom:0}
.ex{background:var(--surface);border:1px solid var(--rule);border-radius:9px;padding:16px 18px;margin:0 0 12px}
.ex h4{margin:0 0 8px;font-size:.84rem;font-weight:700;color:var(--ink);font-family:var(--mono)}
.quote{font-family:var(--mono);font-size:.79rem;line-height:1.55;background:var(--code-bg);
color:var(--code-ink);padding:11px 13px;border-radius:6px;margin:0 0 8px;white-space:pre-wrap;
overflow-x:auto;border:1px solid var(--rule-strong)}
.why{font-size:.83rem;color:var(--muted);margin:0}
.empty{color:var(--faint);font-style:italic}
.foot{margin-top:52px;padding-top:18px;border-top:1px solid var(--rule);font-size:.77rem;color:var(--faint)}
@media (max-width:880px){.shell{grid-template-columns:1fr}
.side{position:static;height:auto;border-right:none;border-bottom:1px solid var(--rule);padding:20px}
nav{flex-direction:row;flex-wrap:wrap}main{padding:32px 20px 70px}h1{font-size:1.85rem}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
html{scroll-behavior:smooth}
"""

SCRIPT = """
(function(){var ls=[].slice.call(document.querySelectorAll('#toc a')),m={};
ls.forEach(function(a){var e=document.querySelector(a.getAttribute('href'));if(e)m[e.id]=a;});
function pick(){var best=null,bt=-1e9;Object.keys(m).forEach(function(id){
var t=document.getElementById(id).getBoundingClientRect().top;
if(t<=140&&t>bt){bt=t;best=id;}});
if(!best)best=Object.keys(m)[0];
ls.forEach(function(a){a.classList.remove('on');});if(m[best])m[best].classList.add('on');}
document.addEventListener('scroll',pick,{passive:true});pick();})();
"""

NAV = [
    ("overview", "Overview"),
    ("gaps", "Ranked gaps"),
    ("examples", "Examples"),
    ("latency", "Latency"),
    ("nothing", "Waited, got nothing"),
    ("sessions", "Worst sessions"),
    ("domains", "By domain"),
    ("proposals", "Open proposals"),
    ("trend", "Trend"),
]


def render(rep: dict, *, generated_at: str = "") -> str:
    h = rep["headline"]
    turns = int(h["turns"] or 0)
    graded = max(turns - int(h["errored"] or 0), 1)
    parts = [f"<title>Lisa Session Review — {escape(rep['judge_version'])}</title>",
             f"<style>{CSS}</style>", '<div class="shell">']

    parts.append('<aside class="side"><div class="brand">'
                 '<span class="eyebrow">Session review</span>'
                 '<span class="name">Lisa</span>'
                 f'<span class="meta">{escape(rep["judge_version"])}'
                 + (f' · {escape(rep["scope_version"])}' if rep.get("scope_version") else "")
                 + '</span></div><nav id="toc">'
                 + "".join(f'<a href="#{i}">{escape(t)}</a>' for i, t in NAV)
                 + "</nav></aside><main>")

    # --- overview ---
    span = ""
    if h.get("first_seen") and h.get("last_seen"):
        span = f"{h['first_seen']:%-d %b} – {h['last_seen']:%-d %b %Y}"
    parts.append('<section id="overview"><h1>Lisa Session Review</h1>'
                 '<p class="lede">Every turn graded twice — once by a model reading only the chat, '
                 'once by arithmetic over the clock. Ranked by what went wrong most.</p>')
    parts.append('<div class="stats">'
                 + _stat(h["sessions"], "Sessions")
                 + _stat(h["chats"], "Chats")
                 + _stat(turns, "Turns graded")
                 + _stat(h["replies"], "Replies")
                 + _stat(h["silences"], "Silences")
                 + _stat(f'{100*int(h["good"] or 0)/graded:.0f}%', "Good", "good")
                 + _stat(h["bad"], "Bad", "bad" if int(h["bad"] or 0) else "")
                 + "</div>")
    if span:
        parts.append(f'<p>Covering <strong>{escape(span)}</strong>. '
                     f'{h["good"]} good · {h["acceptable"]} acceptable · {h["bad"]} bad'
                     + (f' · {h["errored"]} unjudged' if h["errored"] else "") + ".</p>")

    parts.append('<h3>Replies and silences, kept apart</h3>'
                 '<p>Different questions. A healthy bad-rate on replies can hide a poor one on '
                 'silences, and slow silence is not a user wait — it is lock time charged to '
                 'whoever speaks next in that chat.</p>')
    parts.append(_table(
        ["Kind", "Turns", "Good", "Bad", "p50 wait", "p90 wait", "Worst"],
        [[r["turn_kind"], _n(r["turns"]), _n(r["good"]), _n(r["bad"]),
          _secs(r["p50"]), _secs(r["p90"]), _secs(r["worst"])] for r in rep["by_kind"]],
        nums={1, 2, 3, 4, 5, 6}))
    if int(h["overtaken"] or 0):
        parts.append(f'<div class="note"><p><b>{h["overtaken"]} turns were overtaken</b> — newer '
                     'human messages landed while she was still working. Reported as context, not '
                     'counted as a fault: in a group chat that is usually just conversation.</p></div>')
    parts.append("</section>")

    # --- ranked gaps ---
    gaps = rep["gaps"]
    top = max([int(g["hits"]) for g in gaps], default=1)
    parts.append('<section id="gaps"><div class="sechead"><h2>Ranked gaps</h2></div>'
                 '<p>Major-severity count first, then frequency — so a swarm of minor notes cannot '
                 'bury a handful of real incidents. Quality and timing findings rank together '
                 'because a fix budget is shared.</p>')
    if gaps:
        rows = []
        for g in gaps:
            w = int(100 * int(g["hits"]) / top)
            bar = (f'<span class="bar{" maj" if int(g["major"]) else ""}" '
                   f'style="width:{w}%"></span>')
            rows.append([
                f'<code>{escape(g["code"])}</code><br><span class="why">{escape(g["description"])}</span>',
                f'<span class="chip {"neu" if g["source"]=="judge" else "gapc"}">{escape(g["source"])}</span>',
                _n(g["major"]), _n(g["hits"]), _n(g["sessions"]), bar,
            ])
        parts.append(_table(["Code", "Source", "Major", "Hits", "Sessions", ""], rows,
                            nums={2, 3, 4}, raw=True))
    else:
        parts.append('<p class="empty">No findings at this judge version.</p>')
    parts.append("</section>")

    # --- examples ---
    parts.append('<section id="examples"><div class="sechead"><h2>Examples</h2></div>'
                 '<p>The most severe, most recent instance of each gap, with what the judge said. '
                 'Start a fix from the actual failure, not from the label.</p>')
    if rep["examples"]:
        for g in gaps:
            code = g["code"]
            for e in rep["examples"].get(code, [])[:2]:
                body = e.get("reply_text") or "(stayed silent — sent nothing)"
                parts.append(
                    f'<div class="ex"><h4>{escape(code)} '
                    f'<span class="chip neu">{escape(e["turn_kind"])}</span></h4>'
                    f'<div class="quote">{escape(_clip(body, 600))}</div>'
                    + (f'<p class="why"><strong>Evidence:</strong> {escape(_clip(e["evidence"] or "", 300))}</p>'
                       if e.get("evidence") else "")
                    + (f'<p class="why">{escape(_clip(e["rationale"] or "", 400))}</p>'
                       if e.get("rationale") else "")
                    + "</div>")
    else:
        parts.append('<p class="empty">Nothing to show.</p>')
    parts.append("</section>")

    # --- latency ---
    parts.append('<section id="latency"><div class="sechead"><h2>Latency</h2></div>'
                 '<p>Measured from the <strong>last human message</strong> before the reply to the '
                 'reply going out — not end-to-end, which blurs the moment a task spans several '
                 'sessions. <code>model p50</code> is what <code>latency_ms</code> reported; the '
                 'gap between the two is everything the turn did outside its last model call.</p>')
    parts.append(_table(
        ["Task class", "Turns", "p50", "p90", "Worst", "Over budget", "model p50"],
        [[f'<code>{escape(t["task_class"])}</code>', _n(t["turns"]), _secs(t["p50"]),
          _secs(t["p90"]), _secs(t["worst"]), _n(t["over_budget"]),
          _secs(float(t["model_p50"]) / 1000 if t["model_p50"] else None)]
         for t in rep["by_task"]], nums={1, 2, 3, 4, 5, 6}, raw=True))
    parts.append('<div class="note"><p><b>60 seconds is the hard ceiling.</b> The listening window '
                 'is refreshed only after a turn finishes, so anything slower leaves the session '
                 'expired at the moment it answers — the reply lands, but the follow-up it invites '
                 'falls outside the window.</p></div></section>')

    # --- waited, got nothing ---
    ss = rep["slow_silent"]
    parts.append('<section id="nothing"><div class="sechead"><h2>Waited, got nothing</h2></div>'
                 '<p>Turns past 30 seconds that delivered no message. Every row is someone who '
                 'waited and received no answer at all — the worst cell in the whole report.</p>')
    if ss:
        parts.append(_table(
            ["When", "Wait", "Kind", "Delivery", "Error", "Session"],
            [[f'{r["ts"]:%-d %b %H:%M}', _secs(r["wait_seconds"]), r["turn_kind"],
              _chip(r["delivery"]), _chip(r["turn_error"]),
              f'<code>{escape((r["loop_id"] or "")[-12:])}</code>'] for r in ss],
            nums={1}, raw=True))
    else:
        parts.append('<p class="empty">None. Every slow turn still delivered something.</p>')
    parts.append("</section>")

    # --- worst sessions ---
    parts.append('<section id="sessions"><div class="sechead"><h2>Worst sessions</h2></div>'
                 '<p>Ranked by major findings. Pull the full transcript with the session id.</p>')
    parts.append(_table(
        ["When", "Session", "Turns", "Major", "Findings"],
        [[f'{w["ts"]:%-d %b %H:%M}', f'<code>{escape((w["loop_id"] or "")[-16:])}</code>',
          _n(w["turns"]), _n(w["major"]), _n(w["hits"])] for w in rep["worst"]],
        nums={2, 3, 4}, raw=True) if rep["worst"] else '<p class="empty">No session drew a finding.</p>')
    parts.append("</section>")

    # --- domains ---
    parts.append('<section id="domains"><div class="sechead"><h2>By domain</h2></div>'
                 '<p>Which skill the session was running. Calendar failures and web failures point '
                 'at different files.</p>')
    parts.append(_table(["Domain", "Turns", "Findings", "Major"],
                        [[f'<code>{escape(d["domain"])}</code>', _n(d["turns"]), _n(d["hits"]),
                          _n(d["major"])] for d in rep["by_domain"]], nums={1, 2, 3}, raw=True))
    parts.append("</section>")

    # --- proposals ---
    parts.append('<section id="proposals"><div class="sechead"><h2>Open proposals</h2></div>'
                 '<p>Real faults the judge could not fit into the vocabulary. A phrase that keeps '
                 'recurring here is a candidate for a new code — a deliberate edit to '
                 '<code>taxonomy.py</code> plus a judge-version bump, never automatic.</p>')
    parts.append(_table(["Proposed gap", "Hits"],
                        [[escape(p["proposed_gap"]), _n(p["hits"])] for p in rep["proposals"]],
                        nums={1}, raw=True) if rep["proposals"]
                 else '<p class="empty">The vocabulary absorbed everything this run.</p>')
    parts.append("</section>")

    # --- trend ---
    parts.append('<section id="trend"><div class="sechead"><h2>Trend</h2></div>'
                 '<p>Only meaningful once a rubric has been stable for a few weeks — a judge-version '
                 'bump resets the comparison.</p>')
    parts.append(_table(["Week", "Turns", "Bad", "Major findings"],
                        [[f'{t["week"]:%-d %b %Y}', _n(t["turns"]), _n(t["bad"]), _n(t["major"])]
                         for t in rep["trend"]], nums={1, 2, 3}, raw=True))
    parts.append(f'<div class="foot">Judge {escape(rep["judge_version"])}'
                 + (f' · scope {escape(rep["scope_version"])}' if rep.get("scope_version") else "")
                 + (f' · generated {escape(generated_at)}' if generated_at else "")
                 + "</div></section>")

    parts.append(f"</main></div><script>{SCRIPT}</script>")
    return "\n".join(parts)


def standalone(inner: str) -> str:
    """Wrap the artifact-ready content in a minimal document you can open from disk.

    `render` emits <title> and <style> first, then the page markup — so the split point is the
    shell div: everything before it is head material, everything from it is body."""
    marker = '<div class="shell">'
    i = inner.index(marker)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + inner[:i]
        + "</head><body>"
        + inner[i:]
        + "</body></html>"
    )


# --- small helpers ----------------------------------------------------------
def _stat(value: Any, label: str, kind: str = "") -> str:
    return (f'<div class="stat{" " + kind if kind else ""}"><b>{escape(str(value))}</b>'
            f"<span>{escape(label)}</span></div>")


def _table(headers: list, rows: list, *, nums: set | None = None, raw: bool = False) -> str:
    nums = nums or set()
    if not rows:
        return '<p class="empty">No rows.</p>'
    th = "".join(f"<th>{escape(str(x))}</th>" for x in headers)
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            cls = ' class="num"' if i in nums else ""
            tds.append(f"<td{cls}>{c if raw else escape(str(c))}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _n(v: Any) -> str:
    return "0" if v is None else str(int(v))


def _secs(v: Any) -> str:
    return "—" if v is None else f"{float(v):.1f}s"


def _chip(v: Any) -> str:
    if not v or v in ("none",):
        return '<span class="chip neu">—</span>'
    cls = "crit" if v in ("failed", "provider") else "neu"
    return f'<span class="chip {cls}">{escape(str(v))}</span>'


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
