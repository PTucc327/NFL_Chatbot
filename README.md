# 🏈 NFL Chatbot: Advanced Natural Language Sports Assistant

**Project Maintainer**: Paul Tuccinardi

**LinkedIn**: [LinkedIn](https://www.linkedin.com/in/paul-tuccinardi/)

**GitHub**: [PTucc327](https://github.com/PTucc327)

## 🌟 Overview
An advanced, production-grade NFL companion bot built with **Streamlit** that provides real-time scores, validated team news, and player statistics using custom-built **Natural Language Understanding (NLU)**. This project serves as a cornerstone of my Data Science portfolio, demonstrating the ability to bridge raw sports APIs with human-like conversation.

---

## 🚀 Key Features

### 🧠 "Smart" Resolution NLU
Unlike basic keyword matchers, this assistant uses a specialized NLU engine to handle natural conversation:
- **Filler Word Stripping:** Resolves entity names from complex phrases like *"Who is Josh Allen QB for the Bills?"*.
- **Token-Based Hinting:** Automatically extracts team and position "hints" to disambiguate common names.
- **Fuzzy Typo Logic:** Integrated fallback system handling player name misspellings using advanced string similarity algorithms.

### 📰 Multi-Source Validated News
Aggregates news from **ESPN**, **DuckDuckGo**, and **RSS feeds** with a custom relevance-scoring algorithm:
- Filters results using canonical team metadata to ensure 100% relevance.
- Provides clickable Markdown links directly in the chat interface.

### 📊 Comprehensive Data Integration
- **Live Scores:** Real-time categorized scoreboard (In Progress, Final, Scheduled).
- **Fantasy Intelligence:** PPR season stats via the Sleeper API.
- **Schedule Management:** Precise logic for upcoming and previous games with automatic ET conversion.

---

## 🛠️ Technical Stack
- **Framework:** [Streamlit](https://streamlit.io/)
- **APIs:** ESPN Sports API, Sleeper Fantasy API
- **Logic:** Custom Python-based NLU engine & Fuzzy string matching
- **Data Management:** Pandas for internal profile tracking and analytics

---

## 📦 Quick Start

1. **Clone the repository:**
   ```bash
    git clone [https://github.com/PTucc327/NFL_Chatbot.git](https://github.com/PTucc327/NFL_Chatbot.git)
    cd NFL_Chatbot
   ```

2. **install dependencies**
    ```bash
        pip install requirements.txt

    ```

3. **Run the program**
    ```python
        streamlit run app.py
    ```
*Current Iteration uses commands with this format*


[insert what you want fantasy, last game, next game] + [Team name / Player name]

For example:

As of 1/7/26

Next game Packers returns Next game for Green Bay Packers: away vs Chicago Bears on 08:00 PM EST.


### 📅 Roadmap
1. Contextual Memory: Implementing turn-based state to handle follow-up questions (e.g., "What is his record?").

2. Analytical Dashboard: Adding deep wide-receiver efficiency metrics and visualizations.

3. Voice Integration: Experimental support for speech-to-text queries.