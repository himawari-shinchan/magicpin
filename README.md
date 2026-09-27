# Magicpin Vera challenge bot

This project implements the challenge API in `bot.py` with deterministic, context-grounded composition. It prioritizes urgent triggers, uses refreshed merchant and category context, suppresses repeated merchant outreach, handles a single action per merchant per tick, and includes a compact reply state machine for canned replies, opt-outs, intent transitions, hostility, and off-topic messages. No LLM key or external API is required.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m uvicorn bot:app --host 127.0.0.1 --port 8080
```

The application uses in-memory state for the challenge run. `POST /v1/teardown` clears it. The Render and Docker start commands use the platform-provided port.

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

The key stays out of the source file. Simulator scoring makes API requests that are subject to your Google AI Studio project's model access, quota, and billing tier; check its Usage/Billing page first. The endpoint self-check does not require a key: with the bot running, use `python self_test.py` or the bundled Python executable shown above.

For a deployed service, set `$env:BOT_URL = "https://YOUR-SERVICE.onrender.com"` before running the judge simulator. The default remains `http://localhost:8080` for local testing.

## Design choices

- The composer uses only facts present in the trigger, merchant, customer, and category contexts; placeholder-only triggers fall back to available merchant facts or ask a question.
- Trigger selection is a separate deterministic scoring step with urgency, high-stakes kinds, suppression, and rolling merchant fatigue.
- Customer-facing messages use the supplied relationship and appointment details. The response handler stops after repeated automatic replies and honors explicit opt-outs.
- The sample data is synthetic. The bot never sends context to an external service; the optional judge simulator sends its scoring prompts to the configured LLM provider.

## Tradeoffs

State is in memory, so it survives requests to one running process but not process restarts or multi-instance deployment. For the supplied single-process challenge deployment this avoids an undeclared Redis service; use shared storage if deploying multiple workers or requiring restart persistence. The prose is deterministic templates rather than an LLM, trading stylistic variety for predictable latency and source traceability.

The most useful additional context would be explicit suppression history across prior campaigns, merchant outreach consent and quiet hours, and the judge's exact template-approval requirements for each trigger family.
