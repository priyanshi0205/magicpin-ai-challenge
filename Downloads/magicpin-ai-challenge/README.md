# Vera Deterministic Growth Bot

Vera is a dependency-free Python HTTP server for the magicpin AI challenge. It stores pushed context in memory, ranks active triggers deterministically, and composes short WhatsApp-style messages from category, merchant, trigger, and optional customer context.

## Approach

- **State manager**: `/v1/context` stores the latest version per `(scope, context_id)` and rejects stale versions.
- **Composer**: rule-based trigger routing chooses the strongest available signal and grounds every message in pushed context only.
- **Category fit**: routes use vertical-specific vocabulary and offer formats for dentists, salons, restaurants, gyms, and pharmacies.
- **Merchant fit**: messages use owner name, locality, active offers, performance, customer aggregates, signals, and recent conversation history when available.
- **Engagement**: CTAs are short and low-friction, usually "Reply YES/CONFIRM," with effort-externalization such as drafted posts, checklists, WhatsApp copy, and workflows.
- **Replay handling**: `/v1/reply` detects auto-replies, hard opt-outs, intent transitions, waiting requests, and off-topic/GST turns.

## Run

```bash
python bot.py --host 0.0.0.0 --port 8080
```

For local testing on Windows:

```powershell
python bot.py --host 127.0.0.1 --port 8090
$env:BOT_URL="http://localhost:8090"
python smoke_test.py
```

## Render Deployment

This repo is configured for Render's free Python web service using `render.yaml`.

Render settings:

- **Deployment method**: GitHub
- **Runtime**: Python
- **Plan**: Free
- **Build command**: `python -m py_compile bot.py`
- **Start command**: `python bot.py --host 0.0.0.0 --port $PORT`
- **Health check path**: `/v1/healthz`

After deployment, submit only the public base URL, for example:

```text
https://vera-magicpin-bot.onrender.com
```

Do not submit `/v1/healthz` or any endpoint suffix. The judge will call `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata` on that base URL.

Note: Render free web services can sleep after inactivity. Before final submission, open `/v1/healthz` once and wait until it returns JSON, then submit the base URL.

## Tradeoffs

The bot is deterministic and fast, with no external LLM dependency during judging. This avoids latency, cost, and hallucination risk, but rule-based copy is less flexible than a frontier LLM on completely novel trigger kinds. Unknown triggers fall back to category-specific, grounded messages instead of failing.

## Useful Additional Context

The most valuable extra inputs would be real slot availability, affected-customer counts for compliance alerts, local search counts, and richer merchant conversation state. The composer is designed to use those fields immediately if they arrive through context injection.
