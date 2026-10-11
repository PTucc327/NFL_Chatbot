# Privacy Policy

**Sideline** ("the App", "we", "us")  
Last updated: October 2026

---

## 1. Who we are

Sideline is a free, AI-powered assistant for NFL fans built with Streamlit and Google Gemini. It is an independent fan tool and is not affiliated with the NFL, ESPN, Sleeper, or any professional sports organisation.

---

## 2. What data we collect

**We do not collect or store personal information.** To answer your questions, their text is sent to Google Gemini (see section 3) — so please don't type personal information into the chat.

Specifically:

| Data type | Collected? | Notes |
|-----------|-----------|-------|
| Name, email, phone | ❌ No | We have no account or login system |
| IP address | ❌ Not by the App | The App does not log IP addresses or message text. The hosting provider (e.g. Streamlit Community Cloud) may keep standard server logs under its own privacy policy. |
| Location | ❌ No | The App reads your device's time zone (e.g. "America/Chicago") to show times in your local time and to check that you are not in a region where the App isn't offered. It is not stored or logged. |
| Conversation history | ❌ No | Chat exists only in your browser session and is deleted when you close the tab |
| Cookies | ⚠️ One, on your device | When you agree to the Terms, the App saves a cookie named `sideline_consent` in your browser so you aren't asked again on every visit. It holds only the version of the Terms you agreed to, expires after 30 days, and never leaves your browser except back to the App. Clear it in your browser settings to be asked again. Streamlit may also use a technical session cookie. No tracking or advertising cookies are set. |
| Voice input (optional) | ⚠️ Browser only | "Ask by voice" uses your browser's built-in speech recognition. In Chrome and Edge, the browser sends your audio to Google's or Microsoft's speech service to turn it into text; the App only receives the text. |
| Favourite team / player preference | ⚠️ Session only | Kept in your browser session and cleared when you close the tab. (If you run the App yourself with `ENABLE_LOCAL_PREFS=1`, it is saved to `~/.nfl_chatbot_prefs.json` on the machine running the App.) |

---

## 3. Third-party services

The App makes outbound requests to these services on your behalf to answer your questions:

| Service | Purpose | Their privacy policy |
|---------|---------|----------------------|
| **Google Gemini API** | Natural language understanding and response generation | [Google Privacy Policy](https://policies.google.com/privacy) |
| **ESPN APIs** | Live scores, standings, schedules | [ESPN Privacy Policy](https://www.espn.com/espn/privacypolicy) |
| **Sleeper API** | Player profiles, injury status, fantasy stats | [Sleeper Privacy Policy](https://sleeper.com/privacy) |
| **RSS feeds** (Yahoo Sports, NBC Sports PFT, Google News) | NFL news headlines | Subject to each publisher's policy |

Your query text is sent to Google Gemini to extract intent and generate responses. Google's data handling is governed by the [Gemini API terms](https://ai.google.dev/gemini-api/terms). If the App is running on Gemini's free tier, Google may use submitted questions and generated answers to improve its products, and **human reviewers at Google may read them** — please don't include personal information in your questions. We do not send your queries to ESPN or Sleeper — only structured API calls are made.

---

## 4. Data retention

Because we store nothing server-side, there is nothing to retain or delete. Your session data disappears when you close the browser tab. The `sideline_consent` cookie described above stays in your browser until it expires after 30 days or you clear it.

---

## 5. Children's privacy

This App is for adults aged 18 and older (a requirement of the Gemini API terms) and is not directed at children; users confirm their age before using it. It does not knowingly collect information from anyone under 18. If you believe a minor has used the App or provided personal information through it, please contact us so we can address it.

---

## 6. Changes to this policy

We may update this policy as the App evolves. The "Last updated" date at the top will reflect any changes. Continued use of the App after changes constitutes acceptance.

---

## 7. Contact

Questions about this policy? Open an issue on the project's GitHub repository.
