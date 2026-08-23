# 🏈 NFL Pro-Bot

**Author**: Paul Tuccinardi  
**LinkedIn**: [paul-tuccinardi](https://www.linkedin.com/in/paul-tuccinardi/)  
**GitHub**: [PTucc327](https://github.com/PTucc327)

> AI-powered NFL assistant — live scores, injuries, fantasy stats, and more. Just ask.

---

## What it does

NFL Pro-Bot is a production-grade conversational assistant that answers real-time NFL questions in plain English. It's faster than Googling and more accurate than asking a general-purpose chatbot, because it pulls live data — not training-set knowledge.

```
"Is Ja'Marr Chase playing Sunday?"
"Compare Josh Allen to Lamar Jackson"
"Should I trade Travis Kelce for Davante Adams?"
"Who are the best WR waiver pickups right now?"
"Give me a daily briefing on the Eagles"
```

---

## Features

### Natural Language Understanding
- Intent extraction via **Google Gemini 2.5 Flash** — no keyword lists or regex rules
- **Multi-intent**: one query can trigger several parallel data fetches simultaneously
- **Stateful conversation**: trade and comparison discussions persist across turns
- Handles typos, nicknames, and shorthand ("pats", "g-men", "bolts")

### Data Coverage

| What you can ask | Source |
|---|---|
| Live scores (in-progress / final / upcoming) | ESPN API |
| Standings — full league or single team | ESPN API |
| Next game & last game result | ESPN API |
| Betting odds — spread + over/under | ESPN API |
| Team news, ranked by relevance | Google News · Yahoo Sports · PFT RSS |
| Player profiles — active, legends (109 HOF/stars), prospects | Sleeper API + static JSON |
| Injury status — designation, body part, practice participation | Sleeper API (4-hr cache) |
| Weekly per-game stat lines by position | Sleeper API |
| Season PPR fantasy totals | Sleeper API |
| Fantasy sit/start advice with matchup context | Sleeper + Gemini reasoning |
| Head-to-head player comparison | Sleeper + Gemini |
| Trade evaluation — give vs receive verdict | Sleeper + Gemini |
| Waiver wire targets — weighted trend + schedule difficulty | Sleeper + Gemini |
| Team depth chart / "who is the backup QB?" | Static `rosters.json` — no API call |
| League-wide headlines | Yahoo Sports · NBC Sports PFT · Google News RSS |

### Engineering Highlights
- **Streaming responses** — Gemini tokens render live, word-by-word typewriter effect
- **Concurrent fetching** — all intents dispatched in parallel via `ThreadPoolExecutor`
- **Tiered cache TTL** — team metadata 6 hrs · player/injury data 4 hrs (freshens before Fri practice reports)
- **Fuzzy name matching** — `rapidfuzz` token_set_ratio, 2-token guard prevents false positives on bare first names
- **Exponential backoff** — 1 s → 2 s retries on transient network errors
- **Rate limiting** — 10 messages/60 s burst cap + 150 messages/session hard ceiling
- **134-test pytest suite** — covers utils, all API functions, intent routing, and conversation state

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit 1.45 |
| LLM | Google Gemini 2.5 Flash (`google-genai` 2.7) |
| Voice input | `streamlit-mic-recorder` |
| Primary APIs | ESPN Sports API · Sleeper Fantasy API |
| News | `feedparser` — Google News, Yahoo Sports, ProFootballTalk |
| Fuzzy matching | `rapidfuzz` |
| Testing | `pytest` 9 |
| Config | `python-dotenv` |

---

## Quick Start (local)

### 1. Clone
```bash
git clone https://github.com/PTucc327/NFL_Chatbot.git
cd NFL_Chatbot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
Get a **free** Gemini key at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)  
(Free tier: 15 req/min, 1M tokens/day — plenty for personal use.)

```bash
cp template.env .env
# Open .env and set:
# GEMINI_API_KEY=your_key_here
# REPO_URL=https://github.com/PTucc327/NFL_Chatbot   ← optional, enables legal links
```

### 4. Run
```bash
streamlit run app.py
```

---

## Deploying to Streamlit Community Cloud

1. Push the repo to GitHub (make sure `.env` is in `.gitignore` — it is by default).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app** → select this repo.
3. Set **Main file path** to `app.py`.
4. Under **Advanced settings → Secrets**, add:
   ```toml
   GEMINI_API_KEY = "your_key_here"
   REPO_URL = "https://github.com/PTucc327/NFL_Chatbot"
   ```
5. Deploy. The app will be live at a `*.streamlit.app` URL.

> **Never commit `.env` or paste secrets into the code.** The app reads them from environment variables at runtime.

---

## Automated Data Refresh (GitHub Actions)

`rosters.json` — which answers "who is the backup QB?" — is kept fresh by a scheduled GitHub Actions workflow:

- **Runs every Tuesday at 10 AM UTC** (configurable in `.github/workflows/refresh_data.yml`)
- Pulls the latest Sleeper player dump, rebuilds depth charts and injury fields
- Auto-commits the updated file back to `main`
- Streamlit Community Cloud detects the new commit and redeploys automatically
- Also triggerable manually from the **Actions** tab

No cron server, no scheduled task, no infrastructure needed.

---

## Running Tests

```bash
pytest tests/ -v
```

134 tests, all run without a live API key — all HTTP calls are mocked.

```
tests/test_utils.py         # Fuzzy matching, datetime helpers, networking
tests/test_api_client.py    # All data-fetching functions, cache logic
tests/test_chatbot.py       # Intent routing, conversation state, rate limiting
```

---

## Project Structure

```
NFL_Chatbot/
├── app.py                          # Streamlit UI, consent gate, chat rendering
├── requirements.txt                # Pinned dependencies
├── template.env                    # Environment variable reference (copy to .env)
├── PRIVACY_POLICY.md               # Data handling, third-party services
├── TERMS_OF_SERVICE.md             # Usage terms, disclaimers, attribution
│
├── .github/
│   └── workflows/
│       └── refresh_data.yml        # Weekly GitHub Actions roster refresh
│
├── data/
│   ├── legends.json                # 109 HOF / retired / active star profiles
│   ├── prospects.json              # College draft prospect profiles
│   ├── rosters.json                # Active rosters by team (auto-refreshed weekly)
│   └── teams.json                  # 32 team names, abbreviations, IDs (static)
│
├── scripts/
│   └── update_data.py              # Roster + prospect refresh script
│
├── src/
│   ├── api_client.py               # All data-fetching, caching, static data loaders
│   ├── chatbot.py                  # Gemini pipeline, intent routing, conv state
│   └── utils.py                    # Fuzzy matching, HTTP helpers, datetime utils
│
└── tests/
    ├── test_utils.py
    ├── test_api_client.py
    └── test_chatbot.py
```

---

## Legal & Privacy

- **No personal data is collected.** Chat history lives only in your browser session.
- Responses are AI-generated and may be inaccurate — **not for use in sports betting**.
- Data sourced from ESPN, Sleeper, and public RSS feeds. All team names and marks belong to the NFL.
- See [PRIVACY_POLICY.md](PRIVACY_POLICY.md) and [TERMS_OF_SERVICE.md](TERMS_OF_SERVICE.md) for full details.

---

## Roadmap

- [x] Waiver wire recommendations with schedule difficulty context
- [x] Voice input via browser Web Speech API
- [x] Player comparison and trade evaluation
- [x] Stateful multi-turn conversation
- [x] Automated weekly roster refresh (GitHub Actions)
- [x] Privacy Policy, Terms of Service, consent gate
- [ ] Analytical dashboard — WR efficiency, team trend visualisations
- [ ] Historical game log queries ("how did Mahomes do against the Bills last year?")
- [ ] Push notifications for injuries on user's favourite team
