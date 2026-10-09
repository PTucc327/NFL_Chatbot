"""
User-flow tests in a real browser. Each guards a bug found by hand:
the unreachable sidebar, the second welcome step, the 'Done' box left on
answers, stale examples, broken Select buttons, server-clock timestamps,
overflowing sidebar labels, and the phone header under the sidebar toggle.
"""
import datetime
import re
from zoneinfo import ZoneInfo


# ─── First visit ──────────────────────────────────────────────────

def test_consent_then_home_screen(app):
    app.page.get_by_text("Welcome to NFL Pro-Bot").wait_for(timeout=60_000)  # cold start
    app.accept_consent()
    assert app.page.get_by_role("button", name="Skip").count() == 0  # one welcome step
    assert app.page.get_by_text("NFL Pro-Bot", exact=True).count() >= 1
    examples = app.page.get_by_role("button", name=re.compile("What's the playoff picture"))
    assert examples.count() == 1
    assert not app.errors


def test_sidebar_open_on_desktop(app):
    app.accept_consent()
    assert app.page.locator('[data-testid="stSidebarUserContent"]').is_visible()


def test_sidebar_collapsed_on_phone_and_openable(phone):
    phone.accept_consent()
    content = phone.page.locator('[data-testid="stSidebarUserContent"]')
    assert not content.is_visible()
    phone.open_sidebar()  # regression: hiding the whole header removed this control
    text = content.inner_text()
    assert text.index("TEAM LOOKUP") < text.index("Set your favorite team")


def test_phone_header_clear_of_sidebar_toggle(phone):
    phone.accept_consent()
    phone.page.evaluate("document.querySelectorAll('*').forEach(e => { if (e.scrollTop) e.scrollTop = 0 })")
    toggle = phone.page.locator('[data-testid="stExpandSidebarButton"]').first.bounding_box()
    hero = phone.page.locator(".hero").first.bounding_box()
    assert toggle["y"] + toggle["height"] <= hero["y"]


# ─── Asking questions ─────────────────────────────────────────────

def test_example_question_answers_and_examples_hide(app):
    app.accept_consent()
    app.click(re.compile("Who's playing this week"))
    answer = app.last_answer()
    assert "Schedule" in answer or "Week" in answer
    assert app.page.get_by_role("button", name=re.compile("best rookies")).count() == 0
    assert app.page.get_by_text("Done", exact=True).count() == 0  # no leftover status box
    assert not app.errors


def test_timestamps_use_viewer_timezone(open_app):
    pacific = open_app(timezone="America/Los_Angeles")
    pacific.accept_consent()
    pacific.click(re.compile("What's the playoff picture"))
    stamps = [s.strip() for s in pacific.page.locator(".msg-time").all_inner_texts()]
    now = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    recent = {(now - datetime.timedelta(minutes=m)).strftime("%I:%M %p").lstrip("0") for m in range(4)}
    # Every timestamp — the question's and the answer's — in the viewer's zone.
    assert len(stamps) >= 2 and all(s in recent for s in stamps), (stamps, sorted(recent))


def test_typed_question_and_player_selection(app):
    app.accept_consent()
    app.ask("Tell me about Justin Jefferson")
    select = app.page.get_by_role("button", name=re.compile(r"Select Justin Jefferson \(MIN\)"))
    if select.count():  # two active players share the name — pick the Vikings WR
        select.first.click()
        app.wait_idle()
        assert app.page.get_by_role("button", name=re.compile("Select Justin Jefferson")).count() == 0
    answer = app.last_answer()
    assert "Justin Jefferson" in answer and "MIN" in answer


# ─── Sidebar tools ────────────────────────────────────────────────

def test_sidebar_team_stats_button(app):
    app.accept_consent()
    app.sidebar.get_by_role("button", name=re.compile("Team Stats")).click()
    app.wait_idle()
    answer = app.last_answer()
    assert "Team Rankings" in answer and "Defense" in answer


def test_sidebar_labels_fit_on_one_line(app):
    app.accept_consent()
    overflow = app.page.evaluate("""() => [...document.querySelectorAll(
        'section[data-testid=stSidebar] div.stButton > button')]
        .map(b => [b.innerText.trim(), b.scrollWidth - b.clientWidth])
        .filter(([, extra]) => extra > 1)""")
    assert overflow == []


def test_favorite_team_becomes_lookup_team(app):
    app.accept_consent()
    sb = app.sidebar
    sb.locator("summary").filter(has_text="Set your favorite team").click()
    sb.get_by_label("Favorite team").click()
    app.page.keyboard.type("Philadelphia")
    app.page.keyboard.press("Enter")
    sb.get_by_role("button", name="Save Profile").click()
    app.wait_idle()
    assert "Get My Updates" in sb.inner_text()
    lookup = sb.locator('[data-testid="stSelectbox"]').first.inner_text().strip()
    assert lookup.startswith("Philadelphia Eagles")
    assert app.page.locator('[data-testid="stException"]').count() == 0
