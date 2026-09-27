"""Live HTTP smoke/integration checks. Start the API first with `python -m uvicorn bot:app --port 8080`."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BOT_URL", "http://127.0.0.1:8080").rstrip("/")
DATA = Path(__file__).parent / "challenge_bundle" / "expanded"


def request(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def request_text(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    request("/v1/teardown", {})
    code, html = request_text("/")
    check(code == 200 and "Message preview lab" in html, "interactive dashboard")
    code, body = request("/v1/healthz")
    check(code == 200 and body["status"] == "ok", "healthz")
    code, body = request("/v1/metadata")
    check(code == 200 and body["version"] == "1.0.0", "metadata")

    pushed = []
    for scope, folder, id_field in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"), ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
        for file in (DATA / folder).glob("*.json"):
            payload = read(file)
            ctx = {"scope": scope, "context_id": payload[id_field], "version": 1, "payload": payload, "delivered_at": "2026-04-26T08:00:00Z"}
            code, result = request("/v1/context", ctx)
            check(code == 200 and result.get("accepted"), f"push {ctx['context_id']}")
            pushed.append(ctx)
    counts = request("/v1/healthz")[1]["contexts_loaded"]
    check(counts == {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}, "all context counts")

    merchant_ctx = next(x for x in pushed if x["scope"] == "merchant")
    category_ctx = next(x for x in pushed if x["scope"] == "category" and x["context_id"] == merchant_ctx["payload"].get("category_slug"))
    trigger_ctx = next(x for x in pushed if x["scope"] == "trigger" and x["payload"].get("merchant_id") == merchant_ctx["context_id"])
    preview_input = {"category": category_ctx["payload"], "merchant": merchant_ctx["payload"], "trigger": trigger_ctx["payload"]}
    code, preview = request("/v1/preview", preview_input)
    check(code == 200 and preview.get("preview_only") and preview.get("body") and preview.get("rationale"), "stateless composer preview")
    check(request("/v1/healthz")[1]["contexts_loaded"] == counts, "preview does not mutate context")

    code, result = request("/v1/context", merchant_ctx)
    check(code == 409 and result["current_version"] == 1, "same-version push rejected")
    code, _ = request("/v1/context", {**merchant_ctx, "version": 0})
    check(code == 422, "invalid version rejected")
    code, result = request("/v1/context", {**merchant_ctx, "version": 2, "payload": {**merchant_ctx["payload"], "adaptive_update": True}})
    check(code == 200 and result["accepted"], "higher-version update accepted")
    code, _ = request("/v1/context", {**merchant_ctx, "scope": "invalid"})
    check(code == 400, "invalid scope rejected")

    triggers = [read(path) for path in (DATA / "triggers").glob("*.json")]
    code, result = request("/v1/tick", {"now": "2026-04-26T08:00:00Z", "available_triggers": [t["id"] for t in triggers]})
    actions = result.get("actions") or []
    check(code == 200 and len(actions) <= 20, "tick schema and 20-action cap")
    for action in actions:
        required = ("conversation_id", "merchant_id", "send_as", "trigger_id", "template_name", "template_params", "body", "cta", "suppression_key", "rationale")
        check(all(key in action for key in required) and action["body"].strip(), "action schema/body")

    base_reply = {"from_role": "merchant", "received_at": "2026-04-26T08:01:00Z", "turn_number": 1}
    _, first = request("/v1/reply", {**base_reply, "conversation_id": "self-auto", "message": "Thank you for contacting us. Our team will get back to you."})
    _, second = request("/v1/reply", {**base_reply, "conversation_id": "self-auto", "message": "Thank you for contacting us. Our team will get back to you."})
    check(first["action"] == "wait" and second["action"] == "end", "auto-reply detection and exit")
    _, intent = request("/v1/reply", {**base_reply, "conversation_id": "self-intent", "message": "Okay, let's do it"})
    check(intent["action"] == "send" and intent["cta"] == "binary_confirm", "intent handoff")
    _, hostile = request("/v1/reply", {**base_reply, "conversation_id": "self-hostile", "message": "You are an idiot, stop messaging me"})
    check(hostile["action"] == "send" and hostile["cta"] == "none", "hostility response")
    _, opted_out = request("/v1/reply", {**base_reply, "conversation_id": "self-optout", "merchant_id": merchant_ctx["context_id"], "message": "STOP, unsubscribe"})
    check(opted_out["action"] == "end", "opt-out")
    _, curveball = request("/v1/reply", {**base_reply, "conversation_id": "self-curveball", "message": "Can you file my GST?"})
    check(curveball["action"] == "send", "curveball redirect")

    code, result = request("/v1/teardown", {})
    check(code == 200 and result["cleared"], "teardown")
    check(request("/v1/healthz")[1]["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}, "teardown clears context")
    print(f"PASS: dashboard, health/metadata, {len(pushed)} context pushes, stateless preview, version and scope validation, {len(triggers)} trigger candidates ({len(actions)} actions returned), reply flows, teardown.")


if __name__ == "__main__":
    main()
