# Token Leash

A budget kill-switch for AI agents, controlled from your phone.

Token Leash is a small local proxy that sits between any OpenAI-compatible agent and its API. It meters tokens as they are used, pauses the agent when it reaches a budget or starts repeating the same request, and asks you on your phone whether to continue. Built for the belweave Weekend01 hackathon on top of Notify.

## What it does

- **Live meter.** A Notify Live Activity on your lock screen shows tokens used vs. budget, requests per minute, and tokens per minute.
- **Budget pause.** When the budget is reached, new requests are blocked and the agent gets a 429 `budget_exceeded`.
- **Approval to extend.** Your phone gets a yes/no push: "Budget hit. Extend by 20k?" Tap Yes and the agent resumes. No answer, or No, and it stays paused.
- **Loop detection.** If the agent sends the same request several times in a row, you get a push asking whether to keep going.
- **Rate and in-flight limits.** Requests are queued to respect the upstream limits (60 requests per minute, 2 in flight).
- **Fallback.** If a Live Activity cannot start, it falls back to milestone push notifications.

## How it works

```
agent / script  ->  Token Leash proxy (127.0.0.1:8787)  ->  OpenAI-compatible API
                          |
                          +--> Notify: Live Activity (meter) + yes/no approval push
```

Point any OpenAI-compatible client at `http://127.0.0.1:8787/v1`. No agent code changes beyond the base URL. Python standard library only, no dependencies for the proxy.

## Run it

```powershell
$env:BUDGET_TOKENS="3000"
$env:NOTIFY_WEBHOOK="https://notify.belweave.ai/hooks/whk_..."   # your Notify webhook
$env:BW_KEY="..."                                                  # your API key
python leash.py
```

In a second window, run the demo agent:

```powershell
$env:BW_KEY="..."
python leash.py burn
```

The `burn` client needs the `openai` Python package (`pip install openai`).

### Simulated backend

Set `MOCK=1` to replace the model backend with a simulated one that returns fixed 80-token replies. The meter, budget pause, approval push and resume all run for real; only the model output is fake. This was used for the demo because of upstream in-flight limits during the hackathon.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BUDGET_TOKENS` | 20000 | Token budget before pausing |
| `EXTEND_TOKENS` | 20000 | Tokens added when you approve |
| `MAX_RPM` | 55 | Requests per minute cap |
| `LOOP_REPEATS` | 4 | Identical requests before a loop check |
| `ASK_SECONDS` | 300 | How long an approval stays open |
| `MOCK` | off | `1` = simulated backend |

## Built with

Notify (webhooks, Live Activities, approvals) and the Belweave inference API (`Belweave/Kai-4`).
