# Privacy Policy

**NFL Pro-Bot** ("the App", "we", "us")  
Last updated: August 2026

---

## 1. Who we are

NFL Pro-Bot is a free, AI-powered NFL information assistant built with Streamlit and Google Gemini. It is an independent fan tool and is not affiliated with the NFL, ESPN, Sleeper, or any professional sports organisation.

---

## 2. What data we collect

**We do not collect, store, or transmit any personally identifiable information (PII).**

Specifically:

| Data type | Collected? | Notes |
|-----------|-----------|-------|
| Name, email, phone | ❌ No | We have no account or login system |
| IP address | ❌ No | We do not log requests |
| Location | ❌ No | |
| Conversation history | ❌ No | Chat exists only in your browser session and is deleted when you close the tab |
| Cookies | ❌ No | Streamlit may use a technical session cookie scoped to your browser tab; no tracking cookies are set |
| Favourite team / player preference | ⚠️ Local only | Stored in a JSON file on **your own machine** (`~/.nfl_chatbot_prefs.json`). Never sent to us. |

---

## 3. Third-party services

The App makes outbound requests to these services on your behalf to answer your questions:

| Service | Purpose | Their privacy policy |
|---------|---------|----------------------|
| **Google Gemini API** | Natural language understanding and response generation | [Google Privacy Policy](https://policies.google.com/privacy) |
| **ESPN APIs** | Live scores, standings, schedules | [ESPN Privacy Policy](https://www.espn.com/espn/privacypolicy) |
| **Sleeper API** | Player profiles, injury status, fantasy stats | [Sleeper Privacy Policy](https://sleeper.com/privacy) |
| **RSS feeds** (Yahoo Sports, NBC Sports PFT, Google News) | NFL news headlines | Subject to each publisher's policy |

Your query text is sent to Google Gemini to extract intent and generate responses. Google's data handling is governed by their API terms. We do not send your queries to ESPN or Sleeper — only structured API calls are made.

---

## 4. Data retention

Because we store nothing server-side, there is nothing to retain or delete. Your session data disappears when you close the browser tab.

---

## 5. Children's privacy

This App is intended for general audiences. It does not knowingly collect information from children under 13. If you believe a child has provided personal information through this App, please contact us so we can address it.

---

## 6. Changes to this policy

We may update this policy as the App evolves. The "Last updated" date at the top will reflect any changes. Continued use of the App after changes constitutes acceptance.

---

## 7. Contact

Questions about this policy? Open an issue on the project's GitHub repository.
