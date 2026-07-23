#!/usr/bin/env python3
"""
No-LLM local smoke test for Vera.

Usage:
    python smoke_test.py

Optional:
    set BOT_URL=http://localhost:8090
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib import error, request


BOT_URL = os.environ.get("BOT_URL", "http://localhost:8090").rstrip("/")
ROOT = Path(__file__).parent
DATASET = ROOT / "expanded"


def get(path: str) -> dict:
    return json.loads(request.urlopen(BOT_URL + path, timeout=5).read().decode("utf-8"))


def post(path: str, body: dict, timeout: int = 10) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        BOT_URL + path,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.loads(request.urlopen(req, timeout=timeout).read().decode("utf-8"))
    except error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def push_contexts() -> None:
    for path in (DATASET / "categories").glob("*.json"):
        payload = load_json(path)
        push("category", payload["slug"], payload)

    for folder, scope, key in [
        ("merchants", "merchant", "merchant_id"),
        ("customers", "customer", "customer_id"),
        ("triggers", "trigger", "id"),
    ]:
        for path in (DATASET / folder).glob("*.json"):
            payload = load_json(path)
            push(scope, payload[key], payload)


def push(scope: str, context_id: str, payload: dict) -> None:
    response = post(
        "/v1/context",
        {
            "scope": scope,
            "context_id": context_id,
            "version": 1,
            "payload": payload,
            "delivered_at": "2026-04-26T10:00:00Z",
        },
    )
    if not response.get("accepted") and response.get("reason") != "stale_version":
        raise RuntimeError(f"Failed to push {scope}/{context_id}: {response}")


def main() -> None:
    if not DATASET.exists():
        raise SystemExit("Run first: python dataset\\generate_dataset.py --seed-dir dataset --out expanded")

    print(f"Testing Vera at {BOT_URL}")
    print("health before:", get("/v1/healthz")["contexts_loaded"])

    start = time.time()
    push_contexts()
    print(f"context push: {(time.time() - start):.2f}s")
    print("health after:", get("/v1/healthz")["contexts_loaded"])

    pairs = load_json(DATASET / "test_pairs.json")["pairs"]
    triggers = [pair["trigger_id"] for pair in pairs[:10]]
    response = post("/v1/tick", {"now": "2026-04-26T10:05:00Z", "available_triggers": triggers})
    actions = response.get("actions", [])
    print(f"tick actions: {len(actions)}")
    if actions:
        print("sample body:", actions[0]["body"])

    reply = post(
        "/v1/reply",
        {
            "conversation_id": "smoke_intent",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": None,
            "from_role": "merchant",
            "message": "Ok lets do it. Whats next?",
            "received_at": "2026-04-26T10:10:00Z",
            "turn_number": 2,
        },
    )
    print("intent reply:", reply)


if __name__ == "__main__":
    main()
