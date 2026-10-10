"""
Browser (end-to-end) tests: start the real app, drive it in Chromium.

Run with:  RUN_E2E=1 python -m pytest tests/e2e -v
Needs:     pip install -r requirements-dev.txt && python -m playwright install chromium
           (or PW_CHANNEL=msedge / chrome to use an installed browser instead)

The app runs with NFL_BOT_FAKE_LLM=1: Gemini is replaced by stand-ins that
echo the fetched data, so no API key or quota is used. Everything else is
real — the UI, routing, and live ESPN/Sleeper data.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

if os.getenv("RUN_E2E") != "1":
    # Keep the default unit-test run browser-free.
    collect_ignore_glob = ["test_*.py"]

REPO = Path(__file__).resolve().parents[2]
RUNNING = re.compile("Running|Stop", re.I)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_url():
    port = _free_port()
    env = {**os.environ, "NFL_BOT_FAKE_LLM": "1", "GEMINI_API_KEY": "e2e-placeholder",  # pragma: allowlist secret
           "ENABLE_LOCAL_PREFS": "0", "PYTHONIOENCODING": "utf-8"}
    log = open(REPO / "tests" / "e2e" / "app_server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true",
         "--server.address", "127.0.0.1", "--server.port", str(port),
         "--browser.gatherUsageStats", "false"],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"{url}/_stcore/health", timeout=2).status == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("app server did not start; see tests/e2e/app_server.log")
    yield url
    proc.terminate()
    proc.wait(timeout=15)
    log.close()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        channel = os.getenv("PW_CHANNEL") or None
        b = p.chromium.launch(channel=channel)
        yield b
        b.close()


class App:
    """A page on the app plus the actions a user takes."""

    def __init__(self, page, mobile: bool):
        self.page, self.mobile = page, mobile
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def wait_idle(self, timeout: float = 120):
        time.sleep(1.0)
        end = time.time() + timeout
        while time.time() < end:
            if self.page.locator('[data-testid="stStatusWidget"]').filter(has_text=RUNNING).count() == 0:
                time.sleep(0.5)
                return
            time.sleep(0.25)
        raise TimeoutError("app still running")

    def accept_consent(self):
        self.page.get_by_text("I confirm I'm 18 or older").click(timeout=60_000)
        self.wait_idle()
        self.page.get_by_role("button", name=re.compile("I agree")).click(timeout=60_000)
        self.wait_idle()

    def ask(self, text: str):
        box = self.page.locator('[data-testid="stChatInputTextArea"]')
        box.fill(text)
        box.press("Enter")
        self.wait_idle()

    def click(self, name, **kw):
        self.page.get_by_role("button", name=name, **kw).first.click()
        self.wait_idle()

    @property
    def sidebar(self):
        return self.page.locator('[data-testid="stSidebar"]')

    def open_sidebar(self):
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.locator('[data-testid="stExpandSidebarButton"]').first.click(force=True)
        self.page.locator('[data-testid="stSidebarUserContent"]').wait_for(state="visible")

    def sidebar_section(self, name: str):
        """Open a collapsible sidebar section (Team, League, Fantasy, Favorites)."""
        details = self.sidebar.locator("details").filter(
            has=self.page.locator("summary", has_text=name)).first
        if details.get_attribute("open") is None:
            details.locator("summary").first.click()
            time.sleep(0.6)

    def choose_team(self, team: str):
        """Pick a team in the sidebar's Team tab (typing filters the list)."""
        self.sidebar_section("Team")
        self.sidebar.get_by_role("combobox", name="Team", exact=True).click()
        self.page.keyboard.type(team)
        self.page.keyboard.press("Enter")
        self.wait_idle()

    def messages(self):
        return self.page.locator('[data-testid="stChatMessage"]')

    def last_answer(self) -> str:
        msgs = self.messages()
        return msgs.nth(msgs.count() - 1).inner_text()


def _open(browser, app_url, *, mobile=False, timezone="America/New_York"):
    size = {"width": 390, "height": 844} if mobile else {"width": 1400, "height": 900}
    ctx = browser.new_context(viewport=size, is_mobile=mobile, has_touch=mobile,
                              timezone_id=timezone)
    page = ctx.new_page()
    page.goto(app_url)
    return ctx, App(page, mobile)


@pytest.fixture
def app(browser, app_url):
    """Desktop visitor (Eastern time), consent not yet accepted."""
    ctx, a = _open(browser, app_url)
    yield a
    ctx.close()


@pytest.fixture
def phone(browser, app_url):
    """Phone-sized visitor, consent not yet accepted."""
    ctx, a = _open(browser, app_url, mobile=True)
    yield a
    ctx.close()


@pytest.fixture
def open_app(browser, app_url):
    """Factory for custom visitors, e.g. another timezone."""
    contexts = []

    def make(**kw):
        ctx, a = _open(browser, app_url, **kw)
        contexts.append(ctx)
        return a
    yield make
    for ctx in contexts:
        ctx.close()


def pytest_runtest_logreport(report):
    """
    On GitHub Actions, also report each failure as a workflow annotation.
    Annotations are visible without signing in (unlike the job log), so
    a failing browser test can be diagnosed from the public API/UI.
    """
    if not os.getenv("GITHUB_ACTIONS") or not report.failed:
        return
    lines = [l.strip() for l in str(report.longrepr).splitlines()]
    detail = " | ".join(l for l in lines if l.startswith("E "))[:900] or lines[-1][:900]
    esc = lambda s: s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = esc(report.nodeid).replace(":", "%3A").replace(",", "%2C")
    print(f"\n::error title={title} ({report.when})::{esc(detail)}", flush=True)
