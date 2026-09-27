# Magicpin Vera challenge bot

This project implements the challenge API in `bot.py` with deterministic, context-grounded composition. It prioritizes urgent triggers, validates customer identity and message-specific consent, uses refreshed merchant and category context, suppresses repeated merchant outreach, avoids interrupting an unanswered conversation, and includes a reply state machine for canned replies, opt-outs, intent transitions, hostility, and off-topic messages. The bot does not need an LLM key or call an external AI API.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m uvicorn bot:app --host 127.0.0.1 --port 8080
```

If PowerShell says `Python was not found`, install Python 3.12 with **Add Python to PATH** enabled, then reopen PowerShell. If the Python launcher is installed, `py -3 -m pip install -r requirements.txt` and `py -3 -m uvicorn bot:app --host 127.0.0.1 --port 8080` are equivalent.

Open `http://127.0.0.1:8080/` for the responsive Vera dashboard. It shows live API/context status, links to the API docs, and a preview lab with synthetic challenge scenarios. The preview calls `POST /v1/preview`; it runs the same composer as the challenge API without changing stored contexts or conversation state. The dashboard is included in the Render deployment at the service root.

By default, the application keeps state in memory for the challenge run. For a single-process deployment that needs restart persistence, set `VERA_STATE_DB` to a SQLite file path; for example, locally set `$env:VERA_STATE_DB = "$PWD\vera-state.sqlite3"` before starting the API. The bot reloads contexts, conversation state, suppression lists, and recent-send history from that file at startup. `POST /v1/teardown` clears both in-memory state and the saved SQLite state. Database files are ignored by Git.

On Render, SQLite only survives a restart when the file is inside a **mounted persistent disk**, such as `/var/data/vera-state.sqlite3`. Attach a disk in the Render service settings and add `VERA_STATE_DB=/var/data/vera-state.sqlite3` to the service environment. Render persistent disks require a paid service and support one running instance; keep the app at one Uvicorn worker. The current `render.yaml` uses the Free plan, so leave `VERA_STATE_DB` blank there unless the service plan and disk are changed. Render's [free web services use an ephemeral filesystem](https://render.com/docs/free), and [persistent disks are available on paid services](https://render.com/docs/disks). The Render and Docker start commands use the platform-provided port. To show a contact email in `/v1/metadata`, add `TEAM_CONTACT_EMAIL` under Render's **Environment** settings; the project does not guess or hard-code an address.

## Challenge materials and submission

`magicpin-ai-challenge` is the original challenge ZIP. Its complete briefs, simulator, seeds, and dataset generator are unpacked under `challenge_bundle/`. The expanded 50-merchant, 200-customer, 100-trigger dataset is in `challenge_bundle/expanded/`.

Regenerate the dataset and 30 canonical messages with:

```powershell
python challenge_bundle/dataset/generate_dataset.py --seed-dir challenge_bundle/dataset --out challenge_bundle/expanded
python generate_submission.py
```

`submission.jsonl` contains the canonical T01–T30 outputs. The judge simulator reads `GEMINI_API_KEY` from the environment and uses Gemini Flash by default. Start the bot in one PowerShell window:

```powershell
cd "C:\Users\sjham\OneDrive\Desktop\Magicpin"
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m uvicorn bot:app --host 127.0.0.1 --port 8080
```

In a second PowerShell window, enter the API key at a hidden prompt, then start the simulator:

```powershell
cd "C:\Users\sjham\OneDrive\Desktop\Magicpin"
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "Gemini API key" -AsSecureString)).Password
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\challenge_bundle\judge_simulator.py
```

The key stays out of the source file. Simulator scoring makes API requests that are subject to your Google AI Studio project's model access, quota, and billing tier; check its Usage/Billing page first. The endpoint self-check does not require a key: with the bot running, open a second PowerShell window in the project folder and run `python self_test.py`. It checks all 100 generated trigger previews as well as the live API flows.

For a deployed service, set `$env:BOT_URL = "https://YOUR-SERVICE.onrender.com"` before running the judge simulator. The default remains `http://localhost:8080` for local testing.

## Design choices

- The composer uses a deterministic output guard for message length, internal field jargon, category taboo phrases, and numeric facts that do not appear in the supplied contexts. If a draft fails, it falls back to a short, fact-light question.
- Placeholder-only merchant triggers can use a refreshed performance movement, scoped category comparison, review theme, active offer, or customer aggregate when one is present. Otherwise, the bot asks for verified details.
- Trigger selection is a separate deterministic scoring step with urgency, category fit, high-stakes kinds, 24-hour fatigue, suppression keys, cross-trigger near-duplicate checks, and a higher bar for interrupting an unanswered conversation. Each action includes candidate scores and the selected engagement lever in its decision audit.
- Customer outreach requires matching customer and merchant IDs, an opt-in timestamp, and consent for the specific message family. Refill messages are restricted to pharmacy merchants. Hindi and Hindi-English customer preferences receive concise Hinglish wording; other preferences use English.
- The response handler stops after repeated automatic replies, avoids sending the same response twice in one conversation, and honors explicit opt-outs.
- `self_test.py` previews every generated trigger and checks the updated context version changes the next tick's message. It also verifies the response schema, consent checks, selection audit, and restraint around open conversations.
- The sample data is synthetic. The bot never sends context to an external service; the optional judge simulator sends its scoring prompts to the configured LLM provider.

## Tradeoffs

SQLite persistence is optional and intended for a single process. It stores supplied contexts—including customer identifiers and consent attributes—plus conversation text and suppression state, so configure it only on a trusted, access-controlled disk and use the supplied synthetic challenge data unless you have an approved data-handling setup. Render disks are encrypted at rest, but SQLite itself is not encrypted. Multi-worker or multi-instance deployments need a shared database/service instead of this local SQLite file. The prose is deterministic templates rather than an LLM, trading stylistic variety for predictable latency and source traceability.

The most useful additional context would be explicit suppression history across prior campaigns, merchant outreach consent and quiet hours, and the judge's exact template-approval requirements for each trigger family.
