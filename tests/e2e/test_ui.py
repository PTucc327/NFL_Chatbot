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
    app.page.get_by_text("Welcome to Sideline").wait_for(timeout=60_000)  # cold start
    app.accept_consent()
    assert app.page.get_by_role("button", name="Skip").count() == 0  # one welcome step
    assert app.page.get_by_text("Sideline", exact=True).count() >= 1
    examples = app.page.get_by_role("button", name=re.compile("What's the playoff picture"))
    assert examples.count() == 1
    assert not app.errors


def test_agree_requires_age_confirmation(app):
    """Gemini API terms: under-18s may not use apps built on it."""
    app.page.get_by_text("Welcome to Sideline").wait_for(timeout=60_000)
    agree = app.page.get_by_role("button", name=re.compile("I agree"))
    assert agree.is_disabled()
    assert app.page.get_by_text("You must be 18 or older").count() == 1
    app.page.get_by_text("I confirm I'm 18 or older").click()
    app.wait_idle()
    assert agree.is_enabled()


def test_sidebar_open_on_desktop(app):
    app.accept_consent()
    assert app.page.locator('[data-testid="stSidebarUserContent"]').is_visible()


def test_sidebar_collapsed_on_phone_and_openable(phone):
    phone.accept_consent()
    content = phone.page.locator('[data-testid="stSidebarUserContent"]')
    assert not content.is_visible()
    phone.open_sidebar()  # regression: hiding the whole header removed this control
    sections = [t.strip() for t in phone.sidebar.locator("summary").all_inner_texts()]
    assert [t.split()[-1] for t in sections] == ["Team", "League", "Fantasy", "Favorites"]
    assert phone.sidebar.get_by_role("tab").count() == 0  # no tab bar


def test_phone_sidebar_closes_after_a_tool_and_grid_stays_two_wide(phone):
    phone.accept_consent()
    phone.open_sidebar()
    phone.sidebar_section("League")
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
        # The click causes two reruns (queue the choice, then answer it); a
        # single idle wait can land between them, so wait for the answer.
        for _ in range(60):
            app.wait_idle(timeout=30)
            if "MIN" in app.last_answer():
                break
        assert app.page.get_by_role("button", name=re.compile("Select Justin Jefferson")).count() == 0
    answer = app.last_answer()
    assert "Justin Jefferson" in answer and "MIN" in answer


# ─── Sidebar tools ────────────────────────────────────────────────

def test_team_buttons_wait_for_a_team(app):
    app.accept_consent()
    for name in ("Daily Briefing", "Stats", "Roster"):
        assert app.sidebar.get_by_role("button", name=re.compile(name)).first.is_disabled()


def test_team_card_and_team_stats_button(app):
    app.accept_consent()
    app.choose_team("Kansas City")
    card = app.sidebar.locator(".team-card").first.inner_text()
    assert "Kansas City Chiefs" in card
    assert re.search(r"\d+-\d+", card) and "AFC West" in card  # record and division
    app.sidebar.get_by_role("button", name=re.compile("📈 Stats")).click()
    app.wait_idle()
    answer = app.last_answer()
    assert "Team Rankings" in answer and "Defense" in answer


def test_empty_fantasy_input_explains_itself(app):
    app.accept_consent()
    app.sidebar_section("Fantasy")
    before = app.messages().count()
    app.sidebar.get_by_role("button", name=re.compile("Outlook")).click()
    app.page.locator('[data-testid="stToast"]').first.wait_for(timeout=10_000)
    assert "player" in app.page.locator('[data-testid="stToast"]').first.inner_text().lower()
    assert app.messages().count() == before


def test_sidebar_labels_fit(app):
    app.accept_consent()
    app.choose_team("Kansas City")  # enabled buttons
    for tab in ("Team", "League", "Fantasy", "Favorites"):
        app.sidebar_section(tab)
        bad = app.page.evaluate("""() => [...document.querySelectorAll(
            'section[data-testid=stSidebar] div.stButton button')]
            .filter(b => b.offsetParent && b.innerText.trim())
            .filter(b => b.scrollWidth - b.clientWidth > 1 || b.getBoundingClientRect().height > 46)
            .map(b => b.innerText.trim())""")
        assert bad == [], (tab, bad)  # one line each, nothing cut off


def test_favorite_team_becomes_lookup_team(app):
    app.accept_consent()
    sb = app.sidebar
    app.sidebar_section("Favorites")
    sb.get_by_role("combobox", name=re.compile("Favorite team")).click()
    app.page.keyboard.type("Philadelphia")
    app.page.keyboard.press("Enter")
    sb.get_by_role("button", name="Save Profile").click()
    app.wait_idle()
    app.sidebar_section("Favorites")
    assert sb.get_by_role("button", name=re.compile("Get My Updates")).is_visible()
    app.sidebar_section("Team")
    lookup = sb.locator('[data-testid="stSelectbox"]').first.inner_text().strip()
    assert lookup.startswith("Philadelphia Eagles")
    assert app.page.locator('[data-testid="stException"]').count() == 0



def test_legal_text_in_sidebar_not_mid_page(app):
    app.accept_consent()
    main = app.page.locator('[data-testid="stMainBlockContainer"]').first.inner_text()
    assert "not affiliated" not in main and "Privacy" not in main
    legal = app.sidebar.locator(".sb-legal").first
    assert "not affiliated" in legal.inner_text()
    assert legal.get_by_role("link", name="Terms").count() == 1
    assert legal.get_by_role("link", name="Privacy").count() == 1



def test_export_and_clear_enable_right_after_first_answer(app):
    app.accept_consent()
    export = app.sidebar.get_by_role("button", name=re.compile("Export"))
    assert export.is_disabled()
    app.click(re.compile("Who's playing this week"))
    # No extra click needed: the buttons reflect the new answer immediately.
    assert app.sidebar.get_by_role("button", name=re.compile("Export")).is_enabled()
    assert app.sidebar.get_by_role("button", name=re.compile("Clear")).is_enabled()
