# Answer-quality evals

27 real fan questions run through the real pipeline (Gemini + live ESPN/Sleeper
data), with automatic checks on every answer. Run them **before and after**
changing prompts, models or data formatting, and compare the reports.

```bash
python -m evals.run                  # all cases (~55 Gemini requests, ~2 min)
python -m evals.run --only tnf,draft # selected cases
python -m evals.run --list           # list cases
```

Reports are written to `evals/results/` (gitignored): a Markdown summary with
every answer, and the raw JSON.

## What is checked

The data changes every week, so checks compare each answer with **the data the
pipeline fetched for that same question**, not with fixed expected answers.

| Layer | Examples |
|---|---|
| **Understanding** | question type, team, player, opponent, week, stat as extracted by Gemini |
| **Grounding** (all cases) | numbers in the answer appear in the data or the question; totals of listed stats are accepted (4+7+6+5 receptions → 22) |
| **Safety** (all cases) | no error text, no stray non-Latin script, 20–450 words |
| **Case-specific** | names the Super Bowl winner from the data; playoff verdict matches the team's seed; draft answers present no NFL player as a prospect; injury status stated; trade verdict given |

## Cost

About two free-tier Gemini requests per case. `--pause` (default 2s) spaces
cases to stay under per-minute limits; when a model's quota runs out the app's
model chain falls through, exactly as in production. Not part of CI for this
reason.

## Adding a case

Add a `Case` to `cases.py`: the question, what Gemini should understand
(`expect`), and checks built from `checks.py` helpers or a small function
`(answer, data, parsed) -> (ok, message)`. Prefer checks derived from the
data over hard-coded facts. If you write a new check, add a test for it in
`tests/test_eval_checks.py` showing a good answer passing and a bad one failing.

## Findings so far

- **Draft prospects:** asked to name prospects from its own knowledge, the
  model listed players drafted in 2025 (its memory predates recent drafts).
  The app now names only curated prospects and points to a current big board.
- **Same-name players:** "jalen hurts" asked *which* Jalen Hurts because
  Sleeper lists unsigned free agents with the name; rostered players now win.
- Several first-run failures were check bugs (a score split across the data,
  a correct receptions total, a joke about pizza). The checks were fixed and
  have their own tests.
