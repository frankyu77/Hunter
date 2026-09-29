# Hunter

Polls a watchlist of job sources (ATS job boards and curated GitHub repos), detects postings never seen before, filters them, and pushes each new match to Telegram.
Runs on a GitHub Actions cron schedule; every run is stateless and persists its dedup state to `seen_jobs.json`.

## How it works

```
sources.yaml -> FETCH -> NORMALIZE -> DEDUP -> FILTER -> NOTIFY (Telegram)
                (adapters)  (Job)   (seen_jobs.json) (predicates)
```

Each run fetches everything currently posted, subtracts everything already in `seen_jobs.json`, and notifies only the remainder.

### Buttons

Every job message has ⭐ / 👍 / 👎 / ✅ buttons (digests get one numbered row per entry).

- ⭐ **Star** a job to be alerted if its posting closes.
- ✅ **Applied** marks a job you applied to.
- 👍 / 👎 record whether a job was relevant. Votes are kept in the `feedback.votes` section of `seen_jobs.json` as labelled training data for a future relevance model.

Presses are read at the start of the next run (every ~5-15 minutes), so the ✓ on a button appears after that delay, not instantly.
The bot must not have a webhook set: Telegram refuses to hand presses to `getUpdates` while one exists.

Once a week the bot also sends you a private summary: jobs sent, 👍/👎, stars, and applications.

### Dashboard

A static dashboard is rebuilt hourly and published to GitHub Pages at `https://<owner>.github.io/<repo>/`: search every job Hunter sent you (90 days) and every open posting that passes your filters, new postings per week per board, and how long each board's postings stay open.

The repo is public and so is the dashboard, so it shows job data only - never your votes, stars or applications (those stay in the private Telegram summary).

One-time setup: in the repo, **Settings -> Pages -> Build and deployment -> Source: GitHub Actions**. To preview locally from state alone: `python -m scraper.dashboard` and open `site/index.html`.

### Application kit

Every job on the dashboard has a **📝 Kit** button: paste the job description and you get, within seconds:

- **ATS match** - how well your current resume covers the posting's requirements, as a score with the must-have and nice-to-have requirements you have and lack. An estimate: real ATS systems vary.
- **Tailored resume bullets** and a short **cover note**, built only from your real experience, each bullet short enough for one resume line.
- **Skills gap** - reference only: what bullets your past roles could have carried with the skills you're missing.

Set it up once per browser with **📝 Kit setup**: paste your resume as plain text and an Anthropic API key (from console.anthropic.com). Both are stored only in that browser - never on the public page or in the repo. Each kit is one Claude Opus 5 request, typically a few cents. Only save your key on your own devices.

### Direct-source suggestions

Jobs from aggregator repos (SimplifyJobs etc.) arrive hours to days after the company posts them.
Their apply links usually point at the company's own job board, which Hunter can poll directly: Greenhouse, Ashby, Lever, Workday, Oracle, SmartRecruiters, Workable, Rippling, BambooHR, Eightfold, SuccessFactors and iCIMS-backed ("Jibe") career sites, plus TikTok and Amazon.
Once a week the bot sends the top 5 such companies that aren't in `sources.yaml` yet, each checked live and given as a ready-to-paste entry.
The same message names the platforms with no adapter yet that most of your other matches came from.
To stop a company being suggested, add its `ignore key` under `discovery: ignore:` in `sources.yaml`.

## Setup

### 1. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (pick a name and a unique username ending in `bot`).
3. BotFather replies with an HTTP API token that looks like `123456789:AAE...xyz`.
   This is your `TELEGRAM_BOT_TOKEN`. Treat it like a password.

### 2. Get your chat ID

1. Send any message (for example `hi`) to your new bot so a chat exists.
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
3. Find `"chat":{"id":...}` in the JSON response.
   That number is your `TELEGRAM_CHAT_ID` (for a group chat it is negative).

### 3. Set the GitHub Secrets

In the repo: Settings -> Secrets and variables -> Actions -> New repository secret.

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the chat id from `getUpdates` |

Secrets are injected into runs as environment variables.
Never put them in `sources.yaml` or any other tracked file.

### 4. Add sources

Edit `sources.yaml`.
Adding a company on a supported ATS is one entry, for example:

```yaml
sources:
  - type: ashby
    company: wealthsimple
```

## Running locally

```bash
pip install -r requirements.txt
python -m scraper.main --dry-run
```

`--dry-run` prints would-be notifications to stdout instead of sending to Telegram; everything before the notify stage behaves exactly like a real run.

## Development

```bash
ruff check .   # lint
pytest         # tests run fully offline (HTTP is mocked)
```
