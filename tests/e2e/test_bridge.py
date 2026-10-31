"""What the shipped phone apps rely on. An installed app can't be updated with the server, so removing or renaming anything
here breaks people's phones: add to these lists, never take away."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import ROOT, Session

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 SyntropyHealthApp/2.1 SyntropyNav/1"
STAND_IN = """
window.__sent = [];
window.webkit = { messageHandlers: { syntropy: { postMessage: (m) => window.__sent.push(m) } } };
window.SyntropyApp = { capabilities: ["toolbar", "composer", "sections", "scroll", "haptics", "menu"] };
"""
SHELL_METHODS = {"visit", "setProfile", "refresh", "openAlerts", "setAccent", "action", "menuChosen", "composer"}
# Each message the app handles, with the fields it reads.
MESSAGES = {
    "booted": set(),
    "sections": {"sections"},
    "preferences": {"units", "accent"},
    "page": {"route", "hash", "title", "label"},
    "profiles": {"current", "profiles"},
    "alerts": {"unread"},
}


@pytest.fixture
def app(browser, server):
    s = Session(browser, server, width="phone", user_agent=UA)
    s.ctx.add_init_script(STAND_IN)
    s.open("#/overview")
    yield s
    s.close()


def _sent(page, kind=None):
    sent = page.evaluate("window.__sent")
    return [m for m in sent if m["type"] == kind] if kind else sent


def test_the_shell_keeps_every_method_the_apps_call(app):
    methods = app.page.evaluate("Object.keys(window.SyntropyShell)")
    assert SHELL_METHODS <= set(methods), f"Gone: {SHELL_METHODS - set(methods)}"


def test_the_page_keeps_sending_what_the_apps_handle(app):
    for kind, fields in MESSAGES.items():
        found = _sent(app.page, kind)
        assert found, f"No {kind} message was sent"
        assert fields <= set(found[-1]), f"{kind} lost {fields - set(found[-1])}"
    section = _sent(app.page, "sections")[-1]["sections"][0]
    assert {"route", "label", "symbol", "tab"} <= set(section)
    person = _sent(app.page, "profiles")[-1]["profiles"][0]
    assert {"id", "name", "color"} <= set(person)
    assert {"overview", "trends", "ask", "records", "journal", "sources", "settings"} <= {s["route"] for s in _sent(app.page, "sections")[-1]["sections"]}


def test_visit_shows_a_section_and_says_so(app):
    page = app.page
    page.evaluate("SyntropyShell.visit('#/journal')")
    page.wait_for_selector("#view:not([aria-busy])")
    app.settle()
    assert _sent(page, "page")[-1]["route"] == "journal"
    assert page.evaluate("document.documentElement.hasAttribute('data-native-nav')")


def test_a_link_to_another_section_asks_the_app_to_open_it(app):
    page = app.page
    page.evaluate("""() => {
      const a = Object.assign(document.createElement('a'), { href: '#/records' });
      document.getElementById('view').appendChild(a);
      a.click();
    }""")
    assert _sent(page, "visit")[-1]["hash"] == "#/records"


def test_a_confirmation_goes_to_the_app_and_comes_back(app):
    page = app.page
    result = page.evaluate("""async () => {
      const { confirmDialog } = await import('/static/js/ui.js');
      const answer = confirmDialog('Remove?', 'Gone for good.', { confirmLabel: 'Remove', danger: true });
      const sent = window.__sent.filter((m) => m.type === 'confirm').at(-1);
      window.SyntropyConfirm.resolve(sent.id, true);
      return { answer: await answer, sent };
    }""")
    assert result["answer"] is True
    assert {"id", "title", "message", "confirmLabel", "danger"} <= set(result["sent"])


def test_a_menu_goes_to_the_app_and_comes_back(app):
    page = app.page
    result = page.evaluate("""async () => {
      const { dropdown } = await import('/static/js/ui.js');
      window.__chosen = null;
      dropdown(document.body, [{ label: 'One', run: () => { window.__chosen = 1; } }]);
      const sent = window.__sent.filter((m) => m.type === 'menu').at(-1);
      window.SyntropyShell.menuChosen(sent.id, 0);
      await new Promise((r) => setTimeout(r, 20));
      return { chosen: window.__chosen, sent };
    }""")
    assert result["chosen"] == 1
    assert {"id", "items", "rect"} <= set(result["sent"])


def test_status_keeps_every_field_the_apps_read(server):
    with server.client() as c:
        assert c.post("/api/auth/login", json={"password": "correct horse battery"}).status_code == 200
        status = c.get("/api/status").json()
    assert {"version", "setup_complete", "auth_required", "authenticated", "profiles", "account", "preferences", "onboarding",
            "terms_accepted"} <= set(status)


def test_every_script_loads(session):
    page = session.open("#/overview")
    js = ROOT / "app" / "static" / "js"
    for path in sorted(js.rglob("*.js")):
        rel = path.relative_to(js).as_posix()
        page.evaluate("(p) => import(p)", f"/static/js/{rel}")
    assert session.errors == []
