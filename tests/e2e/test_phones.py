"""
Phone checks on real device profiles (screen size, touch, pixel density).

iPhones run in WebKit (Safari's engine), Androids in Chromium. Each device
walks the main flow — consent, home, sidebar tools, answer tables, legal
links — and fails on sideways scrolling, tiny text or tap targets, or a
sidebar that stays over the answer.
"""
import re
import time

import pytest

from .conftest import App

DEVICES = [
    "iPhone SE", "iPhone SE (3rd gen)", "iPhone 12 Mini", "iPhone 13", "iPhone 14 Plus",
    "iPhone 15 Pro", "iPhone 15 Pro Max",
    "Galaxy S9+", "Pixel 4", "Galaxy S24", "Pixel 5", "Pixel 7", "Galaxy A55",
    "iPhone 15 landscape", "Pixel 7 landscape",
]

def is_phone_layout(viewport) -> bool:
    """Mirrors app.py: narrow (Streamlit's own cutoff) or short (phone held sideways)."""
    return viewport["width"] < 768 or viewport["height"] < 500

OVERFLOW_JS = """() => {
    const vw = document.documentElement.clientWidth;
    const wide = [...document.querySelectorAll('[data-testid=stMainBlockContainer] *')]
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > vw + 1; })
      .map(e => e.tagName.toLowerCase() + (e.dataset.testid ? '[' + e.dataset.testid + ']' : ''));
    return {pageScroll: document.documentElement.scrollWidth - vw, wide: [...new Set(wide)].slice(0, 6)};
}"""

SMALL_TEXT_JS = """(scope) => [...document.querySelectorAll(scope + ' *')]
    .filter(e => e.offsetParent && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()))
    .map(e => [e.textContent.trim().slice(0, 30), parseFloat(getComputedStyle(e).fontSize)])
    .filter(([, s]) => s < 11)"""

SMALL_TARGETS_JS = """(scope) => [...document.querySelectorAll(scope + ' button')]
    .filter(b => b.offsetParent && b.innerText.trim())
    .map(b => [b.innerText.trim().slice(0, 20), Math.round(b.getBoundingClientRect().height)])
    .filter(([, h]) => h < 40)"""

TABLES_JS = """() => { const vw = document.documentElement.clientWidth;
    return [...document.querySelectorAll('[data-testid=stMainBlockContainer] table')]
      .map(t => [t.rows[0].cells[0].innerText.trim(), Math.round(t.getBoundingClientRect().right)])
      .filter(([, right]) => right > vw); }"""


@pytest.fixture(params=DEVICES, ids=lambda d: d.replace(" ", "-"))
def device(request, playwright, browser, webkit, app_url):
    name = request.param
    spec = dict(playwright.devices[name])
    engine = webkit if spec.pop("default_browser_type") == "webkit" else browser
    ctx = engine.new_context(**spec, timezone_id="America/Chicago")
    page = ctx.new_page()
    page.goto(app_url)
    yield App(page, mobile=True), spec["viewport"]
    ctx.close()


def _no_overflow(page, where):
    o = page.evaluate(OVERFLOW_JS)
    assert o["pageScroll"] <= 1 and not o["wide"], f"{where}: content wider than the screen {o}"


def test_phone_flow(device):
    a, viewport = device
    page, width = a.page, viewport["width"]
    phone_layout = is_phone_layout(viewport)

    # Consent screen: fits, 18+ box unlocks the agree button.
    page.get_by_text("Welcome to Sideline").wait_for(timeout=90_000)
    _no_overflow(page, "consent")
    page.get_by_text("I'm 18 or older and not located").tap()
    a.wait_idle()
    agree = page.get_by_role("button", name=re.compile("I agree"))
    assert agree.is_enabled()
    agree.tap()
    a.wait_idle()

    # Home: fits, readable, thumb-sized buttons, chat box on screen.
    _no_overflow(page, "home")
    if phone_layout:
        assert not page.locator('[data-testid="stSidebarUserContent"]').is_visible(),             "sidebar should start closed on phones (including sideways)"
    assert page.locator('[data-testid="stChatInputTextArea"]').is_visible()
    main = '[data-testid="stMainBlockContainer"]'
    assert not page.evaluate(SMALL_TEXT_JS, main), "text under 11px on home"
    assert not page.evaluate(SMALL_TARGETS_JS, main), "buttons under 40px on home"

    # Sidebar tools: each tap answers and (on phones) gets the sidebar out of the way.
    content = page.locator('[data-testid="stSidebarUserContent"]')
    for i, tool in enumerate(("Last Game", "Standings")):
        if phone_layout:
            page.evaluate("document.querySelectorAll('*').forEach(e => { if (e.scrollTop) e.scrollTop = 0 })")
            page.locator('[data-testid="stExpandSidebarButton"]').first.tap(force=True)
            content.wait_for(state="visible")
            time.sleep(0.8)
        if i == 0:
            a.choose_team("Kansas City")
        a.sidebar.get_by_role("button", name=re.compile(tool)).tap()
        a.wait_idle()
        if phone_layout:
            time.sleep(1.0)
            assert not content.is_visible(), f"sidebar still covers the answer after {tool}"
    assert not page.evaluate(TABLES_JS), "answer table wider than the screen"
    _no_overflow(page, "answers")

    # Sidebar itself: fits, buttons tappable, legal links reachable at the bottom.
    if phone_layout:
        page.evaluate("document.querySelectorAll('*').forEach(e => { if (e.scrollTop) e.scrollTop = 0 })")
        page.locator('[data-testid="stExpandSidebarButton"]').first.tap(force=True)
        content.wait_for(state="visible")
        time.sleep(0.8)
    assert a.sidebar.bounding_box()["width"] <= width
    assert not page.evaluate(SMALL_TARGETS_JS, '[data-testid="stSidebar"]'), "sidebar buttons under 40px"
    legal = a.sidebar.locator(".sb-legal").first
    legal.scroll_into_view_if_needed()
    assert legal.is_visible()
    assert legal.get_by_role("link", name="Terms").is_visible()
    assert legal.get_by_role("link", name="Privacy").is_visible()

    assert not a.errors, a.errors
