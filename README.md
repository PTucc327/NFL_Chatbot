# 🏈 NFL Pro-Bot: Production NFL AI Assistant

**Project Maintainer**: Paul Tuccinardi  
**LinkedIn**: [paul-tuccinardi](https://www.linkedin.com/in/paul-tuccinardi/)  
**GitHub**: [PTucc327](https://github.com/PTucc327)

---

## Overview

A production-grade NFL conversational assistant built with **Streamlit** and powered by **Google Gemini 2.5 Flash**. The bot handles natural language queries about scores, standings, player stats, injuries, fantasy football, trade advice, and more — all backed by real-time data from the ESPN and Sleeper APIs.

This project is a cornerstone of my Data Science portfolio, demonstrating end-to-end AI application development: LLM-powered NLU, concurrent API orchestration, stateful multi-turn conversation, and a full pytest test suite.

---

## Features

### 🧠 Gemini-Powered NLU
- Intent extraction via **Google Gemini 2.5 Flash** — no regex, no keyword lists
- Handles natural phrasing: *"Who should I start this week?"*, *"Compare Josh Allen to Lamar Jackson"*
- **Multi-intent support**: a single query can trigger multiple parallel data fetches
- **Stateful multi-turn conversation**: trade and comparison discussions persist across turns

### 📊 Data Coverage
| Capability | Source |
|---|---|
| Live scores (in-progress / final / scheduled) | ESPN API |
| Standings (full league or single team) | ESPN API |
| Next game & last game per team | ESPN API |
| Betting odds (spread + over/under) | ESPN API |
| Team news (ranked by relevance) | Google News, Yahoo, PFT RSS |
| Player profiles (active, legends, prospects) | Sleeper API + static JSON |
| Injury status (status, body part, practice participation, depth chart) | Sleeper API |
| Weekly per-game stat lines by position | Sleeper API |
| Season PPR fantasy totals | Sleeper API |
| Fantasy sit/start advice with matchup context | Sleeper API + Gemini |
| **Player comparison (head-to-head stats)** | Sleeper API + Gemini |
| **Trade advice (give vs receive analysis)** | Sleeper API + Gemini |
| **Waiver wire recommendations (trend + matchup)** | Sleeper API + Gemini |

### ⚡ Engineering Highlights
- **Streaming responses** — Gemini tokens render live via `st.write_stream()`
- **Concurrent data fetching** — all intents fetched in parallel with `ThreadPoolExecutor`
- **Fuzzy name matching** — `rapidfuzz` token_set_ratio with a 2-token guard against false positives
- **6-hour TTL caching** — team and player caches reduce API load
- **Exponential backoff** — retries with 1s → 2s → 4s backoff on network errors
- **64-test pytest suite** — covers utils, API functions, intent routing, and conversation state

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit 1.54 |
| LLM | Google Gemini 2.5 Flash (`google-genai`) |
| Voice input | `streamlit-mic-recorder` (browser Web Speech API) |
| Primary APIs | ESPN Sports API, Sleeper Fantasy API |
| News | RSS via `feedparser` (Google News, Yahoo Sports, ProFootballTalk) |
| Fuzzy matching | `rapidfuzz` |
| Testing | `pytest` |
| Config | `python-dotenv` |

---

## Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/PTucc327/NFL_Chatbot.git
cd NFL_Chatbot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure your Gemini API key
Get a **free** key at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) (free tier: 15 req/min, 1M tokens/day).

```bash
cp template.env .env
# Edit .env and add your key:
# GEMINI_API_KEY=your_key_here
```

### 4. Run
```bash
streamlit run app.py
```

---

## Example Queries

```
"How did the Bills do last week?"
"What are the AFC standings?"
"Is Patrick Mahomes playing Sunday?"
"Should I start Tyreek Hill or CeeDee Lamb?"
"Compare Josh Allen to Lamar Jackson"
"Should I trade Travis Kelce for Davante Adams?"
"What are the odds for the Chiefs game?"
"Give me the latest Patriots news"
```

---

## Running Tests

```bash
pytest tests/ -v
```

All 71 tests run without a live API key — HTTP calls are mocked.

---

## Project Structure

```
NFL_Chatbot/
├── app.py              # Streamlit UI and response rendering
├── requirements.txt    # Pinned dependencies
├── template.env        # API key template (copy to .env)
├── data/
│   ├── legends.json    # 15 retired legend profiles (add more here)
│   └── prospects.json  # Draft prospect profiles (add more here)
├── src/
│   ├── api_client.py   # All data fetching functions
│   ├── chatbot.py      # Gemini pipeline, intent routing, conversation state
│   └── utils.py        # Fuzzy matching, networking, datetime helpers
└── tests/
    ├── test_utils.py       # 19 tests
    ├── test_api_client.py  # 25 tests
    └── test_chatbot.py     # 27 tests
```

---

## Roadmap

- [x] Waiver wire recommendations — ranked pickups with schedule difficulty context
- [x] Voice input — speech-to-text via `streamlit_mic_recorder`
- [ ] Analytical dashboard — WR efficiency metrics, team trend visualizations
- [ ] Expanded legends database — automate updates from Pro Football Reference
