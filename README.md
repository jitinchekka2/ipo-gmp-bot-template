# IPO GMP Telegram alerts

Checks the Mainboard IPO grey-market premium (GMP) table every 30 minutes (about 8:30 AM–10 PM IST)
and messages you on Telegram when an open or upcoming Mainboard IPO's estimated listing gain
crosses your threshold (default 15%). SME IPOs are ignored.

It runs entirely on GitHub Actions, so nothing has to stay switched on at home. Data comes from
investorgain.com.

## Alerts you get
- 🟢 **GMP alert**: an IPO first crosses the threshold
- 📈/📉 **Update**: it moves by `REALERT_DELTA` points or more after alerting
- 🔻 **Dropped below**: it falls back under the threshold
- ⏰ **Closes today**: last day of an open IPO that's above the threshold
- ☀️ **Morning digest** at 9:15 AM IST
- ⚠️ **Parser warning** (once a day) if the page stops returning rows, meaning its layout changed

GMP figures are unofficial grey-market estimates. Treat an alert as "worth a look", not investment advice.

## Setup
1. **Make your copy.** On this repo's page click **Use this template → Create a new repository**.
   Choose **Private**. (You need read access to this template; ask its owner to invite you.)
2. **Create a Telegram bot.** In Telegram, message **@BotFather**, send `/newbot`, follow the prompts,
   and copy the token it gives you.
3. **Find your chat ID.** Open your new bot in Telegram and tap **Start** (send it any message).
   Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number in
   `"chat":{"id": ...}`. If the result is empty, send the bot another message and refresh.
4. **Add secrets.** In your repo: Settings → Secrets and variables → Actions → **New repository secret**:
   - `TELEGRAM_BOT_TOKEN`: the token from step 2
   - `TELEGRAM_CHAT_ID`: the number from step 3
5. **Allow the bot to save its state.** Settings → Actions → General → Workflow permissions →
   **Read and write permissions** → Save.
6. **Test it.** Actions tab → **IPO GMP alerts** → **Run workflow**, pick mode `digest`, run.
   A morning-digest message should arrive within a minute. (If Actions shows as disabled in a new repo,
   click the button to enable it.)

After that the schedule takes over. You'll only hear from the bot when something crosses your threshold.

## Settings (all optional)
Add these under Settings → Secrets and variables → Actions → **Variables**:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAINBOARD_THRESHOLD` | 15 | Alert when the estimated gain is at least this many percent |
| `REALERT_DELTA` | 5 | After alerting, send an update when the gain moves this many points |

## Run it on your own computer (optional)
```bash
python3 -m pip install --user -r requirements.txt
DRY_RUN=1 MODE=digest STATE_FILE=/tmp/s.json python3 bot.py   # prints messages instead of sending
```
Use a throwaway `STATE_FILE`, otherwise a test run writes into the real `state.json`.

## Adding ipowatch.in as a second source (advanced, optional)
ipowatch.in refuses requests from GitHub's cloud servers, so it can only be read from a machine on a
home or office network. `optional/ipowatch-snapshot.yml` does this from a self-hosted GitHub runner
and saves the table for the cloud job to merge in. Only attempt this if you have an always-on computer.
1. Settings → Actions → Runners → **New self-hosted runner**, and follow the commands it shows.
   Run it as a service (`./svc.sh install && ./svc.sh start`) and check it shows **Idle**.
2. Move `optional/ipowatch-snapshot.yml` to `.github/workflows/`.
3. Add the repository variable `SNAPSHOT_SOURCES` with the value `ipowatch`.
4. Run the "ipowatch snapshot" workflow once from the Actions tab.

Never attach a self-hosted runner to a public repository.

## Troubleshooting
Open the failed run in the Actions tab and expand **Check GMP and send alerts**.

| What you see | What it means |
| --- | --- |
| `[error] Telegram 400/401/403` | Wrong token or chat ID, or you haven't pressed Start in the bot |
| `HTTPError` for investorgain | The site is down or blocking; the run fails and GitHub emails you |
| ⚠️ "found 0 rows" message | The site's layout changed and the parser needs updating |
| Failure in **Save alert state** | Workflow permissions are not set to Read and write (setup step 5) |

## Notes
- GitHub's scheduled runs can start 5–20 minutes late at busy times.
- GitHub pauses schedules on repos with no activity for 60 days. The bot's state commits usually count
  as activity, but if alerts stop, re-enable the workflow in the Actions tab.
- A private repo gets 2,000 free Actions minutes a month. This bot uses roughly 850.
