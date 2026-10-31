"""The pages the app writes itself, for when there's nothing else to show: starting up, can't start, can't reach the
household's server, a site that won't open. Their buttons reach the app with sites.OWN_PAGE_TOKEN (see sites.py)."""

from __future__ import annotations

import html
import json

import sites

APP_NAME = "Syntropy Health"

# Kinds of db.StartupProblem a backup can help with. A key that doesn't fit, or a newer version's data, isn't damage.
RESTORABLE = {"damaged", "upgrade_failed", "unavailable"}


def _page(body: str) -> str:
    token = json.dumps(sites.OWN_PAGE_TOKEN)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>{APP_NAME}</title>
<style>
  :root {{ color-scheme: light dark; font: 14px -apple-system, "Segoe UI", system-ui, sans-serif; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
  main {{ max-width: 460px; padding: 24px; text-align: center; overflow-wrap: anywhere; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }} p {{ opacity: .75; line-height: 1.45; }} p.small {{ font-size: 12px; }}
  button {{ font: inherit; padding: 7px 14px; margin: 6px 4px 0; border-radius: 7px; border: 1px solid #8884; background: none; color: inherit; }}
  button.primary {{ background: #d6204a; border-color: #d6204a; color: white; }}
  .wait {{ width: 22px; height: 22px; margin: 0 auto 14px; border-radius: 50%; border: 3px solid #8884; border-top-color: #d6204a; animation: spin 1s linear infinite; }}
  .later {{ opacity: 0; animation: appear .4s 6s forwards; }}
  main.quietly {{ opacity: 0; animation: appear .3s 1s forwards; }}
  @keyframes spin {{ to {{ transform: rotate(1turn); }} }}
  @keyframes appear {{ to {{ opacity: 1; }} }}
</style>
{body}
<script>function send(t) {{
  var m = JSON.stringify({{type: t, token: {token}}});
  if (window.webkit && webkit.messageHandlers.syntropyDesktop) webkit.messageHandlers.syntropyDesktop.postMessage(m);
  else if (window.chrome && chrome.webview) chrome.webview.postMessage(m);
}}</script></html>"""


def _button(label: str, action: str, primary: bool = False) -> str:
    return f"""<button{' class="primary"' if primary else ''} onclick="send('{action}')">{html.escape(label)}</button>"""


def starting_page() -> str:
    """Shown until the server answers. Usually gone within a second or two, so it stays out of sight that long. The
    first start after an update copies and upgrades the database first, which takes minutes with several GB."""
    return _page(f"""<main class="quietly"><div class="wait"></div><h1>Starting {APP_NAME}…</h1>
<p class="later">The first start after an update keeps a copy of your data and then upgrades it. With a lot of data this can take
several minutes. Your records are safe. Leave this open, and keep the computer on and plugged in.</p></main>""")


def working_page(title: str, detail: str) -> str:
    return _page(f"""<main><div class="wait"></div><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></main>""")


def problem_page(problem: str, kind: str, folder: str, log_file: str) -> str:
    buttons = [_button("Try Again", "problem-retry", True)]
    if kind in RESTORABLE:
        buttons.append(_button("Restore from a Backup…", "problem-restore"))
    if kind == "damaged":
        buttons.append(_button("Set It Aside and Start Fresh…", "problem-fresh"))
    if kind == "too_new":
        buttons.append(_button("Get the Latest Version", "problem-latest"))
    buttons.append(_button("Show Data Folder", "problem-folder"))
    buttons.append(_button("Quit", "problem-quit"))
    return _page(f"""<main><h1>{APP_NAME} can't start</h1><p>{html.escape(problem)}</p>
{''.join(buttons)}
<p class="small">Your data folder is {html.escape(folder)}. Details are in {html.escape(log_file)}.</p></main>""")


def unreachable_page(name: str, url: str, reason: str = "") -> str:
    """Shown in the window when the household's server doesn't answer (its computer is asleep, or this Mac is away)."""
    why = f"<p>{html.escape(reason)}</p>" if reason else ""
    return _page(f"""<main><h1>Can't reach {html.escape(name)}</h1>
<p>{APP_NAME} runs on {html.escape(name)} ({html.escape(url)}). Make sure that computer is on and awake, and that this
Mac is on the same network (or on Tailscale).</p>{why}
{_button("Try Again", "retry", True)}{_button("Use a Different Server…", "switch")}</main>""")


def load_failed_page(site: str, reason: str) -> str:
    return _page(f"""<main><h1>{html.escape(site)} didn't open</h1><p>{html.escape(reason)}</p>
{_button(f"Back to {APP_NAME}", "go-home", True)}</main>""")
