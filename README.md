# Hunter

Polls a watchlist of job sources (ATS job boards and curated GitHub repos), detects postings never seen before, filters them, and pushes each new match to Telegram.
Runs on a GitHub Actions cron schedule; every run is stateless and persists its dedup state to `seen_jobs.json`.

## How it works

```
sources.yaml -> FETCH -> NORMALIZE -> DEDUP -> FILTER -> NOTIFY (Telegram)
                (adapters)  (Job)   (seen_jobs.json) (predicates)
```

Each run fetches everything currently posted, subtracts everything already in `seen_jobs.json`, and notifies only the remainder.

### Telegram messages

Job messages are plain notifications with no buttons. Everything you do with a job (👍 / 👎, ✅ applied, the application board) happens on the dashboard (see [Voting from the dashboard](#voting-from-the-dashboard)).

Messages sent before that change still show buttons (✅, 👍 / 👎, and ⭐ from a starring feature that has since been removed). Pressing ✅, 👍 or 👎 on one still counts; ⭐ does nothing. Presses are read at the start of the next run, so the ✓ appears after a few minutes.
The bot must not have a webhook set: Telegram refuses to hand presses to `getUpdates` while one exists.

Once a week the bot also sends you a private summary: jobs sent, 👍/👎, and applications.

### Dashboard

A static dashboard is rebuilt hourly and published to GitHub Pages at `https://<owner>.github.io/<repo>/`: search every job Hunter sent you (90 days) and every open posting that passes your filters, new postings per week per board, and how long each board's postings stay open.

The repo is public and so is the dashboard, so its readable part is job data only.
Your votes and applications ship as the **private layer** (below), encrypted.

### Application board

The dashboard has a second page, **Applications** (tab under the header, or `…/#applications` to bookmark it). Once unlocked, it shows your applications as a board: **Saved → Applied → Online assessment → Interview → Offer / Rejected**.

- **📌** on any job row adds it to Saved (a message links you to the board). ✅ puts it in Applied.
- **Drag** a card to another column, or **click** it to pick its stage, edit the date it entered each stage, and keep notes (recruiter, referral, interview rounds…).
- Each card shows when it entered its current stage and how long ago.
- Every stage after Saved counts as applied, and moving back to Saved, or off the board, un-marks it.

Changes sync through the same encrypted inbox as votes (one comment per edit), so the board is the same on every device and stays private. There are no reminder pings: Telegram keeps sending you new jobs as before.

### Telegram alerts

The **Telegram alerts** page (`…/#alerts`, unlocked) chooses which new jobs ping your phone, for example only software internships in Canada:

- **Level:** internships, new grad & junior, full-time.
- **Region:** Canada, US, other. A job listed only as "Remote" always counts.
- **Title contains** and **location contains** any of a few words.
- An on/off switch to pause job alerts entirely.

While you edit, a preview counts how many of the jobs sent to you in the last 90 days would have matched, with examples, using the same rules the bot uses.

Saving sends the setting through the same encrypted inbox as votes. The next run (a few minutes) starts using it and confirms in Telegram. You never edit `sources.yaml` or commit anything: the bot's own state commit carries it.

These settings only narrow what's *sent*. `sources.yaml` filters still decide what Hunter tracks, so a job the alerts skip is still on the Jobs page, under **Not sent**, marked **New** on your next visit. Season alerts follow the settings too. The weekly summary and source suggestions are unaffected.

### New since your last visit

Jobs that reached you since you last opened the dashboard are marked **New**: sent to you on the **Sent to you** tab, first seen by Hunter on **Not sent**. Each tab shows how many are new, and the **New (N)** button filters to just those.

Opening a job's link marks that job seen: its **New** tag goes away and the counts drop. Under the **New** filter it leaves the list, so you can work through new jobs like an inbox.

Your browser remembers when you last looked, so this works per device and without unlocking. The rest stay highlighted for the whole visit, reloads included, until you open them, press **Mark all seen**, or come back another time. The very first visit marks nothing new.

### Private layer

Hunter publishes your personal data on the dashboard *encrypted*: press **🔒 Unlock**, type your passphrase, and the page decrypts it right there in your browser. Anyone else sees only the public job listings.

Once unlocked you get:

- 👍 / 👎 / ✅ buttons on every job row, showing your current marks,
- a **Yours** tab listing every job you voted on or applied to (kept past the 90-day history), filterable by mark,
- totals: applied, 👍 and 👎.

Tick **Remember on this device** and the page unlocks itself on later visits. The browser keeps a derived key that can decrypt but can't be read back out; the passphrase itself is never stored or sent anywhere. **🔓 Lock** forgets it on that device.

One-time setup: add a repo secret `HUNTER_PASSPHRASE` (Settings -> Secrets and variables -> Actions). Any length works, but longer is safer: the encrypted data is public, so a short or guessable passphrase can be cracked offline (runs log a warning under 12 characters). Ideally use 4 or more random words from a password manager's generator. With no secret at all, the dashboard is published with no private layer, and the run log says so. Nothing ever falls back to plaintext.

Forgot it? Set a new secret. Nothing is lost: the next hourly build encrypts under the new passphrase, and browsers that remembered the old one just ask again.

### Voting from the dashboard

Once unlocked, every job row has 👍 / 👎 / ✅ buttons, feeding the relevance model's training data. Telegram messages have no buttons; this is the only place to vote.

Votes are kept in the `feedback.votes` section of `seen_jobs.json` as labelled training data for a future relevance model.

A click shows immediately. It's saved in your browser, then sent to Hunter as an **encrypted comment on a "Hunter inbox" issue** in this repo. That comment starts a run, which records the vote and republishes the dashboard right away instead of waiting for the hourly refresh, so it's on the live site within a few minutes. A dashed outline means a click hasn't reached GitHub yet (offline, or no token) and will be retried. Hunter deletes each comment once it's safely recorded.

Setup, once per browser: click **🗳 Voting setup** and paste a [fine-grained GitHub token](https://github.com/settings/personal-access-tokens/new) with access to **only this repo** and **only Issues: Read and write**. It can't touch code, so a leaked one can only post comments. Hunter creates and locks the inbox issue itself on its first run with `HUNTER_PASSPHRASE` set, so only you can comment there.

**Caveat:** this protects the dashboard, not the repo. `seen_jobs.json` is committed to this public repo and still holds `feedback.votes` in plaintext.

One-time setup: in the repo, **Settings -> Pages -> Build and deployment -> Source: GitHub Actions**. To preview locally from state alone: `python -m scraper.dashboard` and open `site/index.html`.

### Application kit

Every job on the dashboard has a **📝 Kit** button: paste the job description and you get, within seconds:

- **ATS match** - how well your current resume covers the posting's requirements, as a score with the must-have and nice-to-have requirements you have and lack. An estimate: real ATS systems vary.
- **Tailored resume bullets** and a short **cover note**, built only from your real experience, each bullet short enough for one resume line.
- **Skills gap** - reference only: what bullets your past roles could have carried with the skills you're missing.

Set it up once per browser with **📝 Kit setup**: paste your resume as plain text and an Anthropic API key (from console.anthropic.com). Both are stored only in that browser - never on the public page or in the repo. Each kit is one Claude Opus 5 request, typically a few cents. Only save your key on your own devices.

### Direct sources: added automatically

Jobs from aggregator repos (SimplifyJobs etc.) arrive hours to days after the company posts them.
Their apply links usually point at the company's own job board, which Hunter can poll directly: Greenhouse, Ashby, Lever, Workday, Oracle, SmartRecruiters, Workable, Rippling, BambooHR, Eightfold, SuccessFactors and iCIMS-backed ("Jibe") career sites, plus TikTok and Amazon.

Hunter does this on its own:

- Once a company board has fed you **3 matching jobs** late through the feeds, and a live fetch of it works, Hunter starts polling it directly, so new postings arrive within minutes. It adds at most 3 a week (25 in total), checks once a day, and tells you in Telegram ("🧭 Now polling RTX directly").
- A newly added board's existing jobs are recorded silently, like any new source, so adding one never floods your chat.
- A board that fails 5 runs in a row is paused (so it can't slow every run), and Telegram tells you.
- Once Hunter polls a board directly, the same job showing up days later in a feed isn't sent again. That check covers the same company and title in the same region, so a different-city opening still gets through.

The dashboard's **Sources** page (unlocked) lists the boards Hunter added, with how many jobs each has sent you, and **Remove** (never suggested again), **Resume** for a paused one, and **Add** for any other suggestion, without waiting for the threshold. These are kept in Hunter's state, not `sources.yaml`, so there's nothing to commit. Boards you list in `sources.yaml` yourself work as before.

Once a week the bot also still sends the top 5 remaining suggestions, and names the platforms with no adapter yet that most of your other matches came from. To stop a company being suggested from `sources.yaml`, add its `ignore key` under `discovery: ignore:`.

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
