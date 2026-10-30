"""
Accessibility checks (WCAG 2.2 AA): axe-core on every screen, plus what a
scanner can't see — keyboard-only use, spoken button names and new answers
being announced to screen readers.

axe-core is downloaded on first run (pinned version, checked against its
SHA-256) and cached next to this file, so the repo doesn't carry 500 KB of
minified third-party code.
"""
import hashlib
import re
import urllib.request
from pathlib import Path

import pytest

AXE_VERSION = "4.10.2"
AXE_SHA256 = "b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3"  # pragma: allowlist secret (public checksum)
AXE_URL = f"https://cdnjs.cloudflare.com/ajax/libs/axe-core/{AXE_VERSION}/axe.min.js"
AXE_CACHE = Path(__file__).with_name(f".axe-{AXE_VERSION}.min.js")
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]


@pytest.fixture(scope="session")
def axe_source() -> str:
    if not AXE_CACHE.exists():
        AXE_CACHE.write_bytes(urllib.request.urlopen(AXE_URL, timeout=30).read())
    data = AXE_CACHE.read_bytes()
    if hashlib.sha256(data).hexdigest() != AXE_SHA256:
        AXE_CACHE.unlink()
        pytest.fail("axe-core download doesn't match its pinned SHA-256")
    return data.decode("utf-8")


def violations(page, axe_source):
    page.wait_for_timeout(800)  # the accessibility patch runs ~300ms after changes
    page.evaluate(axe_source)
    return page.evaluate(
        """async (tags) => (await axe.run(document, {runOnly: {type: 'tag', values: tags}}))
            .violations.map(v => v.id + ': ' + v.nodes.map(n => n.html.slice(0, 120)).join(' || '))""",
        AXE_TAGS,
    )


def test_desktop_screens_pass_axe(app, axe_source):
    app.page.get_by_text("Welcome to Sideline").wait_for(timeout=60_000)
    assert violations(app.page, axe_source) == [], "welcome screen"
    app.accept_consent()
    assert violations(app.page, axe_source) == [], "home"
    app.choose_team("Kansas City")
    app.click(re.compile("Standings"))
    for section in ("League", "Fantasy", "Favorites"):
        app.sidebar_section(section)
    assert violations(app.page, axe_source) == [], "answer with table, all sidebar sections open"


def test_phone_screens_pass_axe(phone, axe_source):
    phone.page.get_by_text("Welcome to Sideline").wait_for(timeout=60_000)
    assert violations(phone.page, axe_source) == [], "welcome screen"
    phone.accept_consent()
    assert violations(phone.page, axe_source) == [], "home"


def test_region_notice_passes_axe(open_app, axe_source):
    visitor = open_app(timezone="Europe/Berlin")
    visitor.page.get_by_text("isn't available in your region").wait_for(timeout=60_000)
    assert violations(visitor.page, axe_source) == []


def test_player_chart_has_text_alternative(app, axe_source):
    app.accept_consent()
    app.ask("Tell me about Justin Jefferson")
    select = app.page.get_by_role("button", name=re.compile(r"Select Justin Jefferson \(MIN\)"))
    if select.count():  # two active players share the name — pick the Vikings WR
        select.first.click()
        # The click causes two reruns (queue the choice, then answer it).
        for _ in range(60):
            app.wait_idle(timeout=30)
            if "MIN" in app.last_answer():
                break
    chart = app.page.locator('[data-testid="stVegaLiteChart"]')
    if not chart.count():
        pytest.skip("no weekly stats to chart right now")
    assert app.page.get_by_text("Chart data as a table").count() >= 1
    assert chart.first.locator('[role="img"][aria-label*="fantasy points"]').count() == 1
    assert violations(app.page, axe_source) == []


def test_keyboard_only_consent(app):
    app.page.get_by_text("Welcome to Sideline").wait_for(timeout=60_000)
    app.page.locator(".st-key-age_confirmed input").focus()
    app.page.keyboard.press("Space")
    app.wait_idle()
    app.page.keyboard.press("Tab")
    assert "I agree" in app.page.evaluate("document.activeElement.innerText")
    app.page.keyboard.press("Enter")
    app.wait_idle()
    assert app.page.locator('[data-testid="stChatInputTextArea"]').count() == 1


def test_button_names_read_cleanly(app):
    """No emoji or icon ligatures ("keyboard_arrow_down") in what screen readers say."""
    app.accept_consent()
    app.page.wait_for_timeout(800)
    names = app.page.evaluate(
        # Only what a screen reader reaches: visible, and not inside a collapsed
        # section (Streamlit's hidden Deploy/menu buttons don't count).
        """[...document.querySelectorAll('button, summary')]
            .filter(e => e.checkVisibility({visibilityProperty: true}))
            .filter(e => e.tagName === 'SUMMARY' || !e.closest('details:not([open])'))
            .map(e => e.getAttribute('aria-label') || e.innerText.trim())""")
    assert "Daily Briefing" in names and "League" in names
    assert not [n for n in names if not n], "a button with no name"
    assert not [n for n in names if "keyboard_arrow" in n or re.match(r"^\W", n)], names
    # Icon-only toggle (shown on hover on desktop): named explicitly.
    close = app.page.locator('[data-testid="stSidebarCollapseButton"] button').first
    assert close.get_attribute("aria-label") == "Close tools sidebar"


def test_new_answers_are_announced(app):
    app.accept_consent()
    live = app.page.locator("#sideline-live")
    assert live.get_attribute("aria-live") == "polite"
    assert live.inner_text() == ""
    app.ask("Who's playing this week?")
    app.page.wait_for_timeout(800)
    assert live.inner_text().startswith("Sideline answered:")
