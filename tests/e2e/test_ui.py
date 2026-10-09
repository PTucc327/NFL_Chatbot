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
    tabs = [t.strip() for t in phone.sidebar.get_by_role("tab").all_inner_texts()]
    assert [t.split()[-1] for t in tabs] == ["Team", "League", "Fantasy", "Me"]


def test_phone_sidebar_closes_after_a_tool_and_grid_stays_two_wide(phone):
    phone.accept_consent()
    phone.open_sidebar()
    phone.sidebar_tab("League")
    tops = phone.page.evaluate("""[...document.querySelectorAll(
        'section[data-testid=stSidebar] div.stButton button')]
        .filter(b => b.offsetParent && /This Week|Playoffs/.test(b.innerText))
        .map(b => Math.round(b.getBoundingClientRect().top))""")
    assert len(tops) == 2 and tops[0] == tops[1]  # side by side, not stacked
    phone.sidebar.get_by_role("button", name=re.compile("This Week")).click()
    phone.wait_idle()
    phone.page.wait_for_timeout(1500)
    # The answer must not be hidden behind the sidebar.
    assert not phone.page.locator('[data-testid="stSidebarUserContent"]').is_visible()
    assert "Week" in phone.last_answer()


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

def test_team_buttons_wait_for_a_team(app):
    app.accept_consent()
    for name in ("Daily Briefing", "Team Stats", "Roster"):
        assert app.sidebar.get_by_role("button", name=re.compile(name)).first.is_disabled()


def test_team_card_and_team_stats_button(app):
    app.accept_consent()
    app.choose_team("Kansas City")
    card = app.sidebar.locator(".team-card").first.inner_text()
    assert "Kansas City Chiefs" in card
    assert re.search(r"\d+-\d+", card) and "AFC West" in card  # record and division
    app.sidebar.get_by_role("button", name=re.compile("Team Stats")).click()
    app.wait_idle()
    answer = app.last_answer()
    assert "Team Rankings" in answer and "Defense" in answer


def test_empty_fantasy_input_explains_itself(app):
    app.accept_consent()
    app.sidebar_tab("Fantasy")
    before = app.messages().count()
    app.sidebar.get_by_role("button", name=re.compile("Outlook")).click()
    app.page.locator('[data-testid="stToast"]').first.wait_for(timeout=10_000)
    assert "player" in app.page.locator('[data-testid="stToast"]').first.inner_text().lower()
    assert app.messages().count() == before


def test_sidebar_tabs_and_labels_fit(app):
    app.accept_consent()
    app.choose_team("Kansas City")  # enabled buttons
    tab_overflow = app.page.evaluate("""() => { const t = document.querySelector(
        'section[data-testid=stSidebar] [data-baseweb=tab-list]');
        return t.scrollWidth - t.clientWidth; }""")
    assert tab_overflow <= 1, "all four tabs must fit without scrolling"
    for tab in ("Team", "League", "Fantasy", "Me"):
        app.sidebar_tab(tab)
        bad = app.page.evaluate("""() => [...document.querySelectorAll(
            'section[data-testid=stSidebar] div.stButton button')]
            .filter(b => b.offsetParent && b.innerText.trim())
            .filter(b => b.scrollWidth - b.clientWidth > 1 || b.getBoundingClientRect().height > 46)
            .map(b => b.innerText.trim())""")
        assert bad == [], (tab, bad)  # one line each, nothing cut off


def test_favorite_team_becomes_lookup_team(app):
    app.accept_consent()
    sb = app.sidebar
    app.sidebar_tab("Me")
    sb.get_by_role("combobox", name=re.compile("Favorite team")).click()
    app.page.keyboard.type("Philadelphia")
    app.page.keyboard.press("Enter")
    sb.get_by_role("button", name="Save Profile").click()
    app.wait_idle()
    app.sidebar_tab("Me")
    assert sb.get_by_role("button", name=re.compile("Get My Updates")).is_visible()
    app.sidebar_tab("Team")
    lookup = sb.locator('[data-testid="stSelectbox"]').first.inner_text().strip()
    assert lookup.startswith("Philadelphia Eagles")
    assert app.page.locator('[data-testid="stException"]').count() == 0
