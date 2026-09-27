"""Deterministic, context-grounded Vera challenge bot API."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

START_TIME = time.time()
SCOPES = {"category", "merchant", "trigger", "customer"}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_by_merchant: dict[str, list[tuple[float, str]]] = {}
suppressed_merchants: set[str] = set()
suppressed_customers: set[str] = set()
merchant_auto_reply_counts: dict[str, int] = {}
_lock = RLock()

app = FastAPI(title="Magicpin Vera Bot", version="1.0.0")


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str
    received_at: str
    turn_number: int = 1


def _context(scope: str, context_id: str | None) -> dict[str, Any] | None:
    if not context_id:
        return None
    stored = contexts.get((scope, str(context_id)))
    return stored.get("payload") if stored else None


def _pct(value: Any) -> str:
    try:
        return f"{abs(float(value)) * 100:.0f}%"
    except (TypeError, ValueError):
        return ""


def _digest(category: dict[str, Any], item_id: str | None) -> dict[str, Any] | None:
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def _compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    ident = merchant.get("identity") or {}
    name = ident.get("name") or "there"
    first = ident.get("owner_first_name")
    salutation = f"Dr. {first}" if first and merchant.get("category_slug") == "dentists" else (first or name)
    kind = str(trigger.get("kind", "update"))
    payload = trigger.get("payload") or {}
    perf = merchant.get("performance") or {}
    delta = perf.get("delta_7d") or {}
    signals = " ".join(str(x).lower() for x in merchant.get("signals", []))
    active_offer = next((o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active" and o.get("title")), None)
    cta = "open_ended"
    customer_facing = customer is not None or trigger.get("scope") == "customer"

    if kind == "competitor_opened":
        competitor = payload.get("competitor_name")
        distance = payload.get("distance_km")
        offer = payload.get("their_offer")
        facts = []
        if competitor:
            facts.append(str(competitor))
        if isinstance(distance, (int, float)):
            facts.append(f"{distance:g} km away")
        if offer:
            facts.append(f"listing {offer}")
        detail = ", ".join(facts) or "a nearby competitor"
        body = f"Hi {salutation}, {detail} has opened nearby. What is the one service you would want local customers to notice first?"
        rationale = "Uses the competitor details actually supplied and asks the merchant to choose the response, without asserting unverified impact."
        send_as = "vera"
    elif kind in {"perf_spike", "seasonal_perf_dip"}:
        metric = str(payload.get("metric", "listing activity")).replace("_", " ")
        movement = _pct(payload.get("delta_pct"))
        window = payload.get("window", "7d")
        if movement:
            trend = "up" if kind == "perf_spike" or float(payload.get("delta_pct", 0)) > 0 else "down"
            body = f"Hi {salutation}, {name}'s {metric} are {movement} {trend} over {window}."
            if payload.get("likely_driver"):
                body += f" The update points to {str(payload['likely_driver']).replace('_', ' ')}."
            if payload.get("season_note"):
                body += f" The context notes {str(payload['season_note']).replace('_', ' ')}."
            body += " Would you like to review what to do next?"
        else:
            body = f"Hi {salutation}, there is a new update on your {metric}. Would you like me to share the verified details?"
        rationale = "Reflects the reported performance movement and separates a supplied likely driver or seasonal note from inference."
        send_as = "vera"
    elif kind == "active_planning_intent":
        topic = str(payload.get("intent_topic", "the plan")).replace("_", " ")
        body = f"Hi {salutation}, I can sketch the next step for {topic}. Should I draft a first outline for you to review?"
        rationale = "Treats the merchant’s recorded planning intent as an active handoff and offers an immediate draft rather than restarting qualification."
        send_as = "vera"
    elif kind == "supply_alert":
        molecule = payload.get("molecule")
        batches = payload.get("affected_batches") or []
        detail = f" for {molecule}" if molecule else ""
        if batches:
            detail += f" (batches {', '.join(map(str, batches))})"
        body = f"Hi {salutation}, a supply or product alert is recorded{detail}. Please verify the affected stock against the notice before advising customers. Would a concise stock-check summary help?"
        rationale = "Reports only the molecule and batches provided and directs a pharmacist to verify the notice; it does not give patient-specific medical guidance."
        send_as = "vera"
    elif kind == "category_seasonal":
        season = str(payload.get("season", "seasonal demand update")).replace("_", " ")
        trends = [str(x).replace("_", " ") for x in payload.get("trends", [])[:3]]
        detail = "; ".join(trends)
        body = f"Hi {salutation}, the category update for {season} lists {detail or 'a seasonal demand shift'}. Would a shelf-planning checklist be useful?"
        rationale = "Uses only the supplied seasonal trend list and proposes a practical, non-clinical next step."
        send_as = "vera"
    elif kind == "gbp_unverified":
        path = str(payload.get("verification_path", "the verification steps")).replace("_", " ")
        body = f"Hi {salutation}, your business profile is marked unverified. The listed verification route is {path}. Would you like me to walk through that process?"
        rationale = "States the supplied verification status and route without promising an uplift or a successful verification."
        send_as = "vera"
    elif kind == "cde_opportunity":
        item = _digest(category, payload.get("digest_item_id"))
        if item:
            date = item.get("date")
            date_text = f" on {date}" if date else ""
            body = f"Hi {salutation}, {item.get('title', 'a continuing education session')}{date_text} offers {payload.get('credits', item.get('credits', 'listed'))} credits. The listed fee is {payload.get('fee', 'shown in the category update')}. Would you like the details?"
        else:
            body = f"Hi {salutation}, a continuing education opportunity lists {payload.get('credits', 'some')} credits. Would you like me to check its date and fee?"
        rationale = "Retrieves the referenced category digest item and uses its date and credit details when they are present."
        send_as = "vera"
    elif customer_facing and kind == "wedding_package_followup":
        ci = (customer or {}).get("identity") or {}
        cn = ci.get("name") or "there"
        next_step = str(payload.get("next_step_window_open", "the next planning step")).replace("_", " ")
        wedding = payload.get("wedding_date")
        when = f" for your {wedding} wedding" if wedding else ""
        body = f"Hi {cn}, following your trial, the {next_step} window is open{when}. Would you like the salon to share the plan?"
        rationale = "Connects the follow-up to the recorded trial and wedding timing without inventing package prices or booking availability."
        send_as = "merchant_on_behalf"
    elif customer_facing and kind == "appointment_tomorrow":
        ci = (customer or {}).get("identity") or {}
        cn = ci.get("name") or "there"
        body = f"Hi {cn}, a reminder from {name}: your appointment is listed for tomorrow. Please reply if you need the salon to confirm or change the details."
        rationale = "Uses only the appointment timing stated in the trigger and invites correction without inventing a time or service."
        send_as = "merchant_on_behalf"
    elif customer_facing and kind in {"chronic_refill_due", "chronic_refill_grandfather"}:
        ci = (customer or {}).get("identity") or {}
        cn = ci.get("name") or "there"
        molecules = payload.get("molecule_list") or ([payload["molecule"]] if payload.get("molecule") else [])
        meds = ", ".join(str(x) for x in molecules)
        stock_date = payload.get("stock_runs_out_iso")
        detail = f" for {meds}" if meds else ""
        if stock_date:
            detail += f"; the recorded supply date is {stock_date[:10]}"
        body = f"Hi {cn}, a refill reminder from {name}{detail}. Please confirm with the pharmacy before changing any medicine. Would you like the pharmacy to check availability?"
        rationale = "Uses the pharmacy record for a neutral refill reminder and avoids treatment recommendations or asserting that a medicine should be taken."
        send_as = "merchant_on_behalf"
    elif customer_facing:
        customer = customer or {}
        ci = customer.get("identity") or {}
        cn = ci.get("name") or "there"
        service = str(payload.get("service_due") or ("refill" if "refill" in kind else "visit" )).replace("_", " ")
        due = payload.get("due_date")
        slots = payload.get("available_slots") or []
        slot = (slots[0] or {}).get("label") if slots and isinstance(slots[0], dict) else None
        detail = f" Your last service was {payload['last_service_date']}." if payload.get("last_service_date") else ""
        if due:
            detail += f" The suggested date is {due}."
        if slot:
            detail += f" One available time is {slot}."
        body = f"Hi {cn}, {name} here. A {service} follow-up may be due.{detail} Would you like us to check a suitable appointment?"
        rationale = "Customer-facing reminder uses the provided service and scheduling facts; asks one consent-aligned, low-friction question."
        send_as = "merchant_on_behalf"
    elif kind in {"regulation_change", "research_digest", "category_research_digest_release"}:
        item = _digest(category, payload.get("top_item_id"))
        if item:
            source = item.get("source")
            citation = f" ({source})" if source else ""
            actionable = item.get("actionable") or item.get("summary") or item.get("title", "")
            body = f"Hi {salutation}, {item.get('title', 'A category update')}{citation}. {actionable} Is this relevant to your practice?"
        else:
            topic = payload.get("metric_or_topic") or payload.get("topic") or ""
            fact = f" about {topic}" if topic else ""
            body = f"Hi {salutation}, there is a new {kind.replace('_', ' ')}{fact}. I don't have the source details in the context yet. Would a verified summary be useful?"
        rationale = "Shares a matching, source-attributed category item when available and asks whether it applies; avoids inventing missing research details."
        send_as = "vera"
    elif kind in {"perf_dip", "performance_dip"} or "perf_dip" in signals or "ctr_below_peer_median" in signals:
        metric = payload.get("metric") or ("calls" if "calls_pct" in delta and float(delta.get("calls_pct", 0) or 0) < 0 else "views")
        raw = payload.get("delta_pct", delta.get(f"{metric}_pct"))
        movement = _pct(raw)
        window = payload.get("window") or "7 days"
        value = perf.get(metric)
        specifics = f" Your current {metric} count is {value}." if isinstance(value, (int, float)) else ""
        direction = "down" if raw is not None and float(raw) < 0 else ""
        if movement:
            body = f"Hi {salutation}, {name}'s {metric} are {movement} {direction} over {window}.{specifics} Want to review one practical next step?"
        elif isinstance(value, (int, float)):
            body = f"Hi {salutation}, your listing recorded {value} {metric} in the last {perf.get('window_days', 30)} days.{specifics} Would a quick comparison with your category benchmark help?"
        else:
            body = f"Hi {salutation}, I noticed a change in your listing activity, but don't have a verified figure to share. Would you like me to check the latest numbers?"
        rationale = "Uses the trigger or refreshed merchant performance figures and avoids quantifying an unprovided movement."
        send_as = "vera"
    elif kind in {"renewal_due", "subscription_renewal"}:
        days = payload.get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
        plan = payload.get("plan") or (merchant.get("subscription") or {}).get("plan")
        detail = f" Your {plan} plan" if plan else " Your plan"
        detail += f" has {days} days remaining." if isinstance(days, (int, float)) else " is approaching renewal."
        body = f"Hi {salutation},{detail} Would you like me to share the renewal options?"
        rationale = "Surfaces the verified subscription timing and offers a single next step without assuming renewal intent."
        send_as = "vera"
    elif kind in {"curious_ask_due", "scheduled_recurring"}:
        body = f"Hi {salutation}, which service have customers asked you about most this week? I can help turn it into a useful profile post."
        rationale = "Uses a direct, curiosity-led question suited to a low-urgency check-in, with one clear offer of help."
        send_as = "vera"
    elif kind in {"review_theme_emerged", "customer_feedback"}:
        theme = payload.get("theme") or ""
        count = payload.get("occurrences_30d")
        phrase = f"{count} recent reviews mention {str(theme).replace('_', ' ')}" if count else f"recent feedback mentions {str(theme).replace('_', ' ')}"
        quote = payload.get("common_quote")
        quote_text = f' One customer wrote, “{quote}”.' if quote else ""
        body = f"Hi {salutation}, {phrase}.{quote_text} Would it help to look at a small change together?"
        rationale = "Raises a supplied customer-feedback theme with its actual count or quote and invites one practical discussion."
        send_as = "vera"
    elif kind in {"milestone_reached", "milestone"}:
        value = payload.get("value_now", payload.get("milestone_value"))
        metric = str(payload.get("metric", "milestone")).replace("_", " ")
        fact = f" at {value}" if isinstance(value, (int, float)) else ""
        body = f"Hi {salutation}, a quick milestone: your {metric}{fact}. Would you like a short thank-you post draft?"
        rationale = "Acknowledges the milestone using the supplied metric and value, then offers one directly relevant action."
        send_as = "vera"
    else:
        parts = []
        if payload.get("festival"):
            parts.append(f"{payload['festival']} is listed for {payload.get('date', 'the date in your update')}")
        elif payload.get("match"):
            parts.append(f"{payload['match']} at {payload.get('venue', 'the listed venue')}")
        elif payload.get("days_until") is not None:
            parts.append(f"the event is in {payload['days_until']} days")
        elif payload.get("delta_pct") is not None:
            parts.append(f"the reported change is {_pct(payload['delta_pct'])}")
        elif payload.get("metric_or_topic"):
            parts.append(str(payload["metric_or_topic"]))
        if not parts and isinstance(delta, dict):
            moved = next(((k.removesuffix("_pct"), v) for k, v in delta.items() if isinstance(v, (int, float)) and v != 0), None)
            if moved:
                parts.append(f"your {moved[0]} changed {_pct(moved[1])} in 7 days")
        extra = f" I also see your active {active_offer} offer." if active_offer and kind in {"festival_upcoming", "local_event", "weather_heatwave"} else ""
        fact_text = "; ".join(parts)
        if fact_text:
            body = f"Hi {salutation}, {fact_text}.{extra} Would one practical idea for {category.get('display_name', 'your business').lower()} be useful?"
        elif active_offer:
            body = f"Hi {salutation}, your active offer is {active_offer}.{extra} Would you like one idea to help more nearby customers find it?"
        else:
            body = f"Hi {salutation}, I don't have a verified detail for this update yet. Would you like me to check the latest information before suggesting a next step?"
        rationale = "Connects the message to the trigger when its payload contains usable facts; otherwise uses a verified merchant fact or asks before advising."
        send_as = "vera"

    suppression = trigger.get("suppression_key") or trigger.get("id") or f"{kind}:{merchant.get('merchant_id', '')}"
    return {"body": body.strip(), "cta": cta, "send_as": send_as, "suppression_key": suppression, "rationale": rationale}


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope"})
    key = (body.scope, body.context_id)
    with _lock:
        current = contexts.get(key)
        if current and body.version <= current["version"]:
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current["version"]})
        contexts[key] = {"version": body.version, "payload": body.payload, "delivered_at": body.delivered_at}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/healthz")
async def healthz():
    counts = {scope: 0 for scope in SCOPES}
    with _lock:
        for scope, _ in contexts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {"team_name": "Magicpin Vera Bot", "team_members": [], "model": "deterministic-python", "approach": "context-grounded priority selection, deterministic composition, and validated reply state machine", "contact_email": "", "version": "1.0.0", "submitted_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
async def tick(body: TickBody):
    now = time.time()
    try:
        simulated_now = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
        if simulated_now.tzinfo is None:
            simulated_now = simulated_now.replace(tzinfo=timezone.utc)
    except ValueError:
        simulated_now = datetime.now(timezone.utc)
    actions: list[dict[str, Any]] = []
    considered: list[tuple[int, str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    with _lock:
        for trigger_id in body.available_triggers:
            trigger = _context("trigger", trigger_id)
            if not trigger:
                continue
            merchant_id = trigger.get("merchant_id")
            merchant = _context("merchant", merchant_id)
            customer = _context("customer", trigger.get("customer_id")) if trigger.get("customer_id") else None
            category = _context("category", merchant.get("category_slug")) if merchant else None
            if not (merchant and category):
                continue
            expires = trigger.get("expires_at")
            if expires:
                try:
                    if datetime.fromisoformat(expires.replace("Z", "+00:00")) < simulated_now:
                        continue
                except ValueError:
                    pass
            urgency = max(1, min(5, int(trigger.get("urgency", 1))))
            score = urgency * 10
            kind = trigger.get("kind", "")
            if kind in {"renewal_due", "regulation_change", "recall_due", "appointment_tomorrow"}:
                score += 15
            if kind == "perf_dip" and any("perf_dip" in str(x) for x in merchant.get("signals", [])):
                score += 20
            payload = trigger.get("payload") or {}
            if payload.get("placeholder") is True and len(payload) <= 2:
                score -= 10
            previous = [x for x in sent_by_merchant.get(str(merchant_id), []) if now - x[0] < 86400]
            score -= min(30, len(previous) * 12)
            if any(x[1] == trigger.get("suppression_key") for x in previous):
                continue
            if previous and not kind in {"renewal_due", "regulation_change"}:
                score -= 15
            considered.append((score, str(trigger_id), trigger, merchant, category, customer))
        considered.sort(key=lambda row: (-row[0], row[1]))
        picked_merchants: set[str] = set()
        for score, trigger_id, trigger, merchant, category, customer in considered:
            merchant_id = str(merchant.get("merchant_id", trigger.get("merchant_id", "")))
            customer_id = str((customer or {}).get("customer_id", ""))
            if len(actions) >= 20 or score < 15 or merchant_id in picked_merchants or merchant_id in suppressed_merchants or customer_id in suppressed_customers:
                continue
            composed = _compose(category, merchant, trigger, customer)
            conversation_id = f"conv_{merchant_id}_{trigger_id}"
            composed["rationale"] += f" Selection score {score}; considered {len(considered)} available trigger(s), selected the highest-priority eligible trigger for this merchant."
            action = {"conversation_id": conversation_id, "merchant_id": merchant_id, "customer_id": (customer or {}).get("customer_id"), "send_as": composed["send_as"], "trigger_id": trigger_id, "template_name": "vera_contextual_v1", "template_params": [merchant.get("identity", {}).get("name", ""), trigger.get("kind", ""), str((trigger.get("payload") or {}).get("top_item_id", ""))], **composed}
            actions.append(action)
            picked_merchants.add(merchant_id)
            sent_by_merchant.setdefault(merchant_id, []).append((now, composed["suppression_key"]))
            conversations.setdefault(conversation_id, {"merchant_id": merchant_id, "customer_id": customer_id, "sent": [], "last_trigger": trigger})["sent"].append(composed["body"])
    return {"actions": actions}


def _reply_kind(message: str) -> str:
    text = re.sub(r"\s+", " ", message.strip().lower())
    if not text:
        return "empty"
    if any(x in text for x in ("idiot", "stupid", "shut up", "fool", "damn", "abuse", "fuck")):
        return "hostile"
    if any(x in text for x in ("stop", "unsubscribe", "don't message", "do not message", "not interested", "no thanks", "remove me")):
        return "optout"
    if any(x in text for x in ("automated assistant", "automatic reply", "auto-reply", "thank you for contacting", "thanks for contacting", "we will get back", "our team will get back", "business hours", "currently unavailable")):
        return "auto"
    if any(x in text for x in ("let's do it", "lets do it", "go ahead", "please proceed", "start it", "do this", "yes, do", "yes please")):
        return "intent"
    if any(x in text for x in ("gst", "unrelated", "weather", "cricket", "politics")):
        return "curveball"
    if re.fullmatch(r"(yes|yeah|yep|ok|okay|sure|no|nope|nah|thanks|thank you|fine)[.! ]*", text):
        return "simple"
    return "other"


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    with _lock:
        state = conversations.setdefault(body.conversation_id, {"merchant_id": body.merchant_id, "customer_id": body.customer_id, "sent": [], "replies": [], "auto_count": 0, "ended": False})
        if state.get("ended"):
            return {"action": "end", "rationale": "This conversation has already been closed; do not restart after an opt-out or terminal state."}
        state["replies"].append(body.message)
        kind = _reply_kind(body.message)
        normalized = re.sub(r"\s+", " ", body.message.strip().lower())
        repeats = sum(re.sub(r"\s+", " ", x.strip().lower()) == normalized for x in state["replies"])
        if kind == "auto":
            state["auto_count"] = state.get("auto_count", 0) + 1
            merchant_key = str(body.merchant_id or state.get("merchant_id") or body.conversation_id)
            merchant_auto_reply_counts[merchant_key] = merchant_auto_reply_counts.get(merchant_key, 0) + 1
            if state["auto_count"] > 1 or merchant_auto_reply_counts[merchant_key] > 1 or repeats > 1:
                state["ended"] = True
                return {"action": "end", "rationale": "Repeated canned auto-reply detected; stop to avoid wasting turns."}
            return {"action": "wait", "rationale": "Likely business auto-reply detected; wait once for a human response instead of continuing the sales thread."}
        merchant_key = str(body.merchant_id or state.get("merchant_id") or "")
        if merchant_key:
            merchant_auto_reply_counts[merchant_key] = 0
        if kind == "hostile":
            state["ended"] = True
            return {"action": "send", "body": "I’m sorry this was unwelcome. I’ll stop here.", "cta": "none", "rationale": "Acknowledges hostility briefly and closes without arguing."}
        if kind == "optout":
            state["ended"] = True
            if state.get("customer_id"):
                suppressed_customers.add(str(state["customer_id"]))
            elif state.get("merchant_id"):
                suppressed_merchants.add(str(state["merchant_id"]))
            return {"action": "end", "rationale": "Explicit opt-out detected; close the conversation and suppress further outreach."}
        if kind == "intent":
            trigger = state.get("last_trigger") or {}
            task = str(trigger.get("kind", "next step")).replace("_", " ")
            return {"action": "send", "body": f"Understood. I’ll move ahead with the {task} we discussed. Is that okay?", "cta": "binary_confirm", "rationale": "Recognizes explicit intent and advances directly to confirmation of the already-stated action, with no additional qualification."}
        if kind == "curveball":
            return {"action": "send", "body": "I can’t help with that here, but I can continue with the business update we were discussing. Would you like to do that?", "cta": "open_ended", "rationale": "Politely declines an unrelated request and returns to the single open business question."}
        if kind == "simple":
            if normalized.startswith(("no", "nah")):
                state["ended"] = True
                return {"action": "end", "rationale": "Merchant declined; close politely without another pitch."}
            state["ended"] = True
            return {"action": "end", "rationale": "Acknowledgement received; no additional action is needed."}
        last = state.get("sent", [])[-1] if state.get("sent") else "the update"
        if len(state.get("sent", [])) and last in state.get("replies", []):
            return {"action": "wait", "rationale": "Reply repeats the bot’s previous message; wait rather than repeat it."}
        return {"action": "send", "body": "Thanks for sharing that. What would be most useful for you to address first?", "cta": "open_ended", "rationale": "Acknowledges the response and asks one concise question to clarify the merchant’s priority."}


@app.post("/v1/teardown")
async def teardown():
    with _lock:
        contexts.clear()
        conversations.clear()
        sent_by_merchant.clear()
        suppressed_merchants.clear()
        suppressed_customers.clear()
        merchant_auto_reply_counts.clear()
    return {"cleared": True}
