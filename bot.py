"""Deterministic, context-grounded Vera challenge bot API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

START_TIME = time.time()
SCOPES = {"category", "merchant", "trigger", "customer"}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_by_merchant: dict[str, list[tuple[float, str, str, str]]] = {}
suppressed_merchants: set[str] = set()
suppressed_customers: set[str] = set()
merchant_auto_reply_counts: dict[str, int] = {}
_lock = RLock()
_state_db_setting = os.getenv("VERA_STATE_DB", "").strip()
STATE_DB_PATH = Path(_state_db_setting).expanduser().resolve() if _state_db_setting else None


def _state_snapshot() -> dict[str, Any]:
    return {
        "contexts": [
            {"scope": scope, "context_id": context_id, **record}
            for (scope, context_id), record in contexts.items()
        ],
        "conversations": conversations,
        "sent_by_merchant": {
            merchant_id: [list(entry) for entry in entries]
            for merchant_id, entries in sent_by_merchant.items()
        },
        "suppressed_merchants": sorted(suppressed_merchants),
        "suppressed_customers": sorted(suppressed_customers),
        "merchant_auto_reply_counts": merchant_auto_reply_counts,
    }


def _connect_state_db() -> sqlite3.Connection:
    if STATE_DB_PATH is None:
        raise RuntimeError("VERA_STATE_DB is not configured")
    STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE_DB_PATH, timeout=10)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS vera_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return connection


def _load_persisted_state() -> None:
    if STATE_DB_PATH is None or not STATE_DB_PATH.exists():
        return
    connection = _connect_state_db()
    try:
        row = connection.execute("SELECT payload FROM vera_state WHERE id = 1").fetchone()
        if row is None:
            return
        snapshot = json.loads(row[0])
        if not isinstance(snapshot, dict):
            raise ValueError("saved state must be a JSON object")
        for entry in snapshot.get("contexts", []):
            scope = entry.get("scope")
            context_id = entry.get("context_id")
            if scope not in SCOPES or not isinstance(context_id, str):
                raise ValueError("saved context has an invalid key")
            contexts[(scope, context_id)] = {
                "version": entry["version"],
                "payload": entry["payload"],
                "delivered_at": entry["delivered_at"],
            }
        conversations.update(snapshot.get("conversations", {}))
        sent_by_merchant.update({
            str(merchant_id): [
                (float(entry[0]), str(entry[1]), str(entry[2]), str(entry[3]))
                for entry in entries
            ]
            for merchant_id, entries in snapshot.get("sent_by_merchant", {}).items()
        })
        suppressed_merchants.update(snapshot.get("suppressed_merchants", []))
        suppressed_customers.update(snapshot.get("suppressed_customers", []))
        merchant_auto_reply_counts.update({
            str(key): int(value)
            for key, value in snapshot.get("merchant_auto_reply_counts", {}).items()
        })
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load persisted Vera state from {STATE_DB_PATH}") from error
    finally:
        connection.close()


def _save_persisted_state_locked() -> None:
    if STATE_DB_PATH is None:
        return
    connection = _connect_state_db()
    try:
        connection.execute(
            "INSERT INTO vera_state (id, payload, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (json.dumps(_state_snapshot(), ensure_ascii=False, allow_nan=False), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


def _clear_persisted_state_locked() -> None:
    if STATE_DB_PATH is None or not STATE_DB_PATH.exists():
        return
    connection = _connect_state_db()
    try:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("DELETE FROM vera_state WHERE id = 1")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


_load_persisted_state()

app = FastAPI(title="Magicpin Vera Bot", version="1.1.0")
UI_FILE = Path(__file__).parent / "static" / "index.html"


@app.middleware("http")
async def persist_mutating_state(request, call_next):
    response = await call_next(request)
    if STATE_DB_PATH is not None and request.method == "POST" and request.url.path in {
        "/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown",
    }:
        try:
            with _lock:
                if request.url.path == "/v1/teardown":
                    _clear_persisted_state_locked()
                else:
                    _save_persisted_state_locked()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return JSONResponse(status_code=503, content={"detail": "Persistent state could not be saved."})
    return response


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


class PreviewBody(BaseModel):
    category: dict[str, Any]
    merchant: dict[str, Any]
    trigger: dict[str, Any]
    customer: dict[str, Any] | None = None


def _context(scope: str, context_id: str | None) -> dict[str, Any] | None:
    if not context_id:
        return None
    stored = contexts.get((scope, str(context_id)))
    return stored.get("payload") if stored else None


def _pct(value: Any) -> str:
    try:
        number = float(value)
        return f"{abs(number) * 100:.0f}%" if isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def _is_negative(value: Any) -> bool:
    try:
        return float(value) < 0
    except (TypeError, ValueError):
        return False


def _digest(category: dict[str, Any], item_id: str | None) -> dict[str, Any] | None:
    for item in category.get("digest") or []:
        if item.get("id") == item_id:
            return item
    return None


def _customer_eligibility(trigger: dict[str, Any], merchant: dict[str, Any], customer: dict[str, Any] | None) -> tuple[bool, str]:
    """Fail closed for customer outreach when identity, business, category, or consent do not match."""
    scope = trigger.get("scope")
    if scope == "merchant":
        if trigger.get("customer_id"):
            return False, "merchant-scoped trigger unexpectedly includes a customer"
        return True, ""
    if scope != "customer":
        return False, "trigger scope is invalid"
    if not customer:
        return False, "customer context is missing"
    customer_id = str(trigger.get("customer_id") or "")
    if not customer_id or str(customer.get("customer_id") or "") != customer_id:
        return False, "customer identity does not match the trigger"
    merchant_id = str(merchant.get("merchant_id") or "")
    if str(customer.get("merchant_id") or "") != merchant_id or str(trigger.get("merchant_id") or "") != merchant_id:
        return False, "customer and trigger do not belong to this merchant"

    category_slug = str(merchant.get("category_slug") or "")
    kind = str(trigger.get("kind") or "")
    if kind in {"chronic_refill_due", "chronic_refill_grandfather"} and category_slug != "pharmacies":
        return False, "refill outreach is restricted to pharmacy merchants"
    if kind == "wedding_package_followup" and category_slug != "salons":
        return False, "wedding package follow-up is restricted to salon merchants"
    if kind == "trial_followup" and category_slug not in {"gyms", "salons"}:
        return False, "trial follow-up is unsupported for this category"

    required_scopes = {
        "recall_due": {"recall_reminders", "recall_alerts"},
        "appointment_tomorrow": {"appointment_reminders"},
        "chronic_refill_due": {"refill_reminders"},
        "chronic_refill_grandfather": {"refill_reminders"},
        "wedding_package_followup": {"bridal_package_followup"},
        "trial_followup": {"program_updates", "kids_program_updates"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
        "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
        "winback_eligible": {"winback_offers", "promotional_offers"},
    }.get(kind, {"promotional_offers"})
    consent = customer.get("consent") or {}
    if not isinstance(consent, dict):
        return False, "customer consent record is invalid"
    scopes = set(consent.get("scope") or [])
    if not consent.get("opted_in_at"):
        return False, "customer opt-in timestamp is missing"
    if not scopes.intersection(required_scopes):
        return False, "customer has no consent for this message type"
    return True, ""


def _language_mode(customer: dict[str, Any] | None) -> str:
    identity = (customer or {}).get("identity") or {}
    preference = re.sub(r"[_\s]+", " ", str(identity.get("language_pref") or "").strip().lower())
    return "hi-en" if preference in {"hi", "hindi", "hi en mix", "hinglish"} else "en"


def _human_metric(value: Any) -> str:
    metric = str(value or "listing activity").replace("_", " ").strip()
    aliases = {
        "ctr": "click-through rate",
        "views": "listing views",
        "directions": "direction requests",
        "leads": "customer leads",
    }
    return aliases.get(metric.lower(), metric)


def _placeholder_insight(merchant: dict[str, Any], category: dict[str, Any]) -> str | None:
    """Choose a relevant, literal fact from refreshed context when a trigger is only a placeholder."""
    performance = merchant.get("performance") or {}
    deltas = performance.get("delta_7d") or {}
    if isinstance(deltas, dict):
        for key in ("calls_pct", "views_pct", "directions_pct", "ctr_pct"):
            value = deltas.get(key)
            if isinstance(value, (int, float)) and isfinite(float(value)) and value != 0:
                metric_key = key.removesuffix("_pct")
                metric = _human_metric(metric_key)
                direction = "up" if value > 0 else "down"
                detail = f"The latest 7-day listing update shows {metric} {_pct(value)} {direction}."
                current_value = performance.get(metric_key)
                peer_stats = category.get("peer_stats") or {}
                peer_value = peer_stats.get(f"avg_{metric_key}_30d") if isinstance(peer_stats, dict) and performance.get("window_days") == 30 else None
                if isinstance(current_value, (int, float)) and isinstance(peer_value, (int, float)):
                    scope = peer_stats.get("scope") or "category"
                    detail += f" The current 30-day count is {current_value}; the {scope} reference is {peer_value}."
                return detail

    reviews = merchant.get("review_themes") or []
    if reviews and isinstance(reviews[0], dict):
        review = reviews[0]
        theme = str(review.get("theme") or "customer feedback").replace("_", " ")
        count = review.get("occurrences_30d")
        if isinstance(count, int) and count > 0:
            return f"Your latest review summary has {count} mentions of {theme}."

    active_offer = next((offer.get("title") for offer in merchant.get("offers") or []
                         if isinstance(offer, dict) and offer.get("status") == "active" and offer.get("title")), None)
    if active_offer:
        return f"Your active listing includes {active_offer}."

    aggregates = merchant.get("customer_aggregate") or {}
    total = aggregates.get("total_unique_ytd") if isinstance(aggregates, dict) else None
    if isinstance(total, int) and total > 0:
        return f"Your customer summary records {total} unique customers year to date."
    return None


def _fact_numbers(value: Any, allowed: set[str]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if isfinite(number):
            raw = f"{number:g}"
            allowed.add(raw)
            allowed.add(f"{abs(number):g}")
            if 0 <= abs(number) <= 1:
                allowed.add(f"{abs(number) * 100:g}%")
                allowed.add(f"{abs(number) * 100:g}")
        return
    if isinstance(value, str):
        allowed.update(match.group(0).replace(",", "").rstrip("%") for match in re.finditer(r"(?<![\w])\d[\d,]*(?:\.\d+)?%?", value))
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            window = re.search(r"_(\d+)d$", str(key))
            if window:
                allowed.add(window.group(1))
            _fact_numbers(nested, allowed)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _fact_numbers(nested, allowed)


def _validate_message(body: str, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None) -> list[str]:
    warnings: list[str] = []
    if not body.strip():
        warnings.append("empty body")
    if len(body) > 500:
        warnings.append("body exceeds 500 characters")
    jargon = ("payload", "merchant_id", "customer_id", "suppression_key", "delta_7d", "ctr")
    if any(re.search(rf"\b{re.escape(term)}\b", body, re.IGNORECASE) for term in jargon):
        warnings.append("internal field name or jargon")
    voice = category.get("voice") or {}
    taboo = voice.get("vocab_taboo") or [] if isinstance(voice, dict) else []
    for phrase in taboo:
        # Some datasets annotate a conditional taboo in parentheses; apply the taboo conservatively.
        blocked = str(phrase).split(" (", 1)[0].strip()
        if blocked and re.search(re.escape(blocked), body, re.IGNORECASE):
            warnings.append("category taboo vocabulary")
            break
    allowed_numbers: set[str] = set()
    for source in (category, merchant, trigger, customer):
        _fact_numbers(source, allowed_numbers)
    for match in re.finditer(r"(?<![\w])\d[\d,]*(?:\.\d+)?%?", body):
        number = match.group(0).replace(",", "").rstrip("%")
        if number not in allowed_numbers:
            warnings.append(f"unsupported number: {match.group(0)}")
            break
    return warnings


def _near_duplicate(body: str, previous_messages: list[str]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9]+", body.lower()))
    if not normalized:
        return False
    return any(SequenceMatcher(None, normalized, " ".join(re.findall(r"[a-z0-9]+", old.lower()))).ratio() >= 0.86
               for old in previous_messages if old)


def _epoch(value: str | None, fallback: float | None = None) -> float:
    if value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp()
        except (TypeError, ValueError):
            pass
    return time.time() if fallback is None else fallback


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
    active_offer = next((o.get("title") for o in merchant.get("offers") or [] if isinstance(o, dict) and o.get("status") == "active" and o.get("title")), None)
    cta = "open_ended"
    customer_facing = customer is not None or trigger.get("scope") == "customer"
    language_mode = _language_mode(customer)
    placeholder_insight = _placeholder_insight(merchant, category) if payload.get("placeholder") is True and not customer_facing and kind != "curious_ask_due" else None
    engagement_lever = "scoped_category_benchmark" if placeholder_insight and "reference is" in placeholder_insight else "direct_question"

    if placeholder_insight:
        body = f"Hi {salutation}, {placeholder_insight} Would you like to review one practical next step?"
        rationale = "The trigger has placeholder-only details, so this message uses a current, merchant-specific fact from refreshed context and asks one clear question."
        send_as = "vera"
    elif kind == "competitor_opened":
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
        metric = _human_metric(payload.get("metric", "listing activity"))
        movement = _pct(payload.get("delta_pct"))
        window = payload.get("window", "7d")
        if movement:
            if payload.get("delta_pct") is not None:
                trend = "down" if _is_negative(payload.get("delta_pct")) else "up"
            else:
                trend = "up" if kind == "perf_spike" else "down"
            verb = "is" if metric == "click-through rate" else "are"
            body = f"Hi {salutation}, {name}'s {metric} {verb} {movement} {trend} over {window}."
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
        if language_mode == "hi-en":
            body = f"Hi {cn}, {name} se reminder: aapki appointment kal ke liye listed hai. Time ya details confirm ya change karni ho, please reply karein."
        else:
            body = f"Hi {cn}, a reminder from {name}: your appointment is listed for tomorrow. Please reply if you need us to confirm or change the details."
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
        if language_mode == "hi-en":
            body = f"Hi {cn}, {name} se refill reminder{detail}. Medicine mein koi change karne se pehle pharmacy se confirm karein. Kya pharmacy availability check kare?"
        else:
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
        if language_mode == "hi-en":
            body = f"Hi {cn}, {name} se ek {service} follow-up reminder hai.{detail} Kya hum aapke liye suitable appointment check karein?"
        else:
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
        metric = payload.get("metric") or ("calls" if "calls_pct" in delta and _is_negative(delta.get("calls_pct")) else "views")
        raw = payload.get("delta_pct", delta.get(f"{metric}_pct"))
        movement = _pct(raw)
        window = payload.get("window") or "7 days"
        value = perf.get(metric)
        metric_label = _human_metric(metric)
        if isinstance(value, (int, float)) and str(metric).lower() == "ctr":
            specifics = f" The current click-through rate is {_pct(value)}."
        else:
            specifics = f" Your current {metric_label} count is {value}." if isinstance(value, (int, float)) else ""
        direction = "down" if _is_negative(raw) else "up"
        rationale = "Uses the trigger or refreshed merchant performance figures and avoids quantifying an unprovided movement."
        if movement:
            body = f"Hi {salutation}, {name}'s {_human_metric(metric)} are {movement} {direction} over {window}.{specifics} Want to review one practical next step?"
            peer_stats = category.get("peer_stats") or {}
            peer_key = f"avg_{metric}_30d"
            peer_value = peer_stats.get(peer_key) if isinstance(peer_stats, dict) and perf.get("window_days") == 30 else None
            if isinstance(peer_value, (int, float)) and isfinite(float(peer_value)) and isinstance(value, (int, float)):
                body = f"Hi {salutation}, {name}'s {_human_metric(metric)} are {movement} {direction} over {window}.{specifics} For context, the {peer_stats.get('scope', 'category')} reference is {peer_value} in 30 days. Want to compare one practical next step?"
                rationale = "Uses the refreshed merchant movement and an explicitly scoped category reference, then asks one practical follow-up."
                engagement_lever = "scoped_category_benchmark"
        elif isinstance(value, (int, float)):
            metric_value = _pct(value) if str(metric).lower() == "ctr" else str(value)
            body = f"Hi {salutation}, your listing recorded {metric_value} {metric_label} in the last {perf.get('window_days', 30)} days.{specifics} Would a quick comparison with your category benchmark help?"
        else:
            body = f"Hi {salutation}, I noticed a change in your listing activity, but don't have a verified figure to share. Would you like me to check the latest numbers?"
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
        category_questions = {
            "dentists": "Which treatment question have patients asked you most often this week?",
            "gyms": "Which class or training goal have members asked about most this week?",
            "pharmacies": "Which pharmacy service question have customers asked most this week?",
            "restaurants": "Which dish have guests asked about most this week?",
            "salons": "Which salon service have clients asked about most this week?",
        }
        question = category_questions.get(str(merchant.get("category_slug") or ""), "What have customers asked you about most this week?")
        body = f"Hi {salutation}, {question} I can help turn it into a useful profile post."
        rationale = "Uses a direct, curiosity-led question suited to a low-urgency check-in, with one clear offer of help."
        engagement_lever = "category_specific_curiosity"
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
    body = body.strip()
    warnings = _validate_message(body, category, merchant, trigger, customer)
    fallback_used = bool(warnings)
    if fallback_used:
        if customer_facing:
            customer_name = ((customer or {}).get("identity") or {}).get("name") or "there"
            body = f"Hi {customer_name}, {name} here. Would you like us to confirm a suitable next step for you?"
        else:
            body = f"Hi {salutation}, I have an update for your business. Would a verified summary be useful?"
        rationale += " The output guard selected a short fallback because the draft contained an unverified fact, restricted phrase, or exceeded the length limit."
        engagement_lever = "verified_follow_up"
    final_warnings = _validate_message(body, category, merchant, trigger, customer)
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression,
        "rationale": rationale,
        "engagement_lever": engagement_lever,
        "validation": {"passed": not final_warnings, "fallback_used": fallback_used, "warnings": final_warnings},
    }


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(UI_FILE, media_type="text/html")


@app.post("/v1/preview")
async def preview(body: PreviewBody):
    """Return a composer preview without writing context or conversation state."""
    return {**_compose(body.category, body.merchant, body.trigger, body.customer), "preview_only": True}


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
    return {"team_name": "Magicpin Vera Bot", "team_members": ["Harsh Jha"], "model": "deterministic-python", "approach": "consent-aware priority selection, cross-trigger fatigue and dedupe, fact-checked deterministic composition, and audited reply state machine", "contact_email": os.getenv("TEAM_CONTACT_EMAIL", ""), "version": "1.1.0", "submitted_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
async def tick(body: TickBody):
    try:
        simulated_now = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
        if simulated_now.tzinfo is None:
            simulated_now = simulated_now.replace(tzinfo=timezone.utc)
    except ValueError:
        simulated_now = datetime.now(timezone.utc)
    now = simulated_now.timestamp()
    actions: list[dict[str, Any]] = []
    considered: list[tuple[int, str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any], bool, dict[str, int]]] = []
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
            eligible, _eligibility_reason = _customer_eligibility(trigger, merchant, customer)
            if not eligible:
                continue
            expires = trigger.get("expires_at")
            if expires:
                try:
                    expiry_time = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if expiry_time.tzinfo is None:
                        expiry_time = expiry_time.replace(tzinfo=timezone.utc)
                    if expiry_time < simulated_now:
                        continue
                except (AttributeError, TypeError, ValueError):
                    pass
            try:
                urgency = max(1, min(5, int(trigger.get("urgency", 1))))
            except (TypeError, ValueError, OverflowError):
                urgency = 1
            kind = trigger.get("kind", "")
            category_slug = str(merchant.get("category_slug") or "")
            high_stakes_bonus = 15 if kind in {"renewal_due", "regulation_change", "recall_due", "appointment_tomorrow"} else 0
            if kind in {"chronic_refill_due", "chronic_refill_grandfather"} and category_slug == "pharmacies":
                high_stakes_bonus += 5
            category_fit_bonus = 5 if ((kind in {"regulation_change", "research_digest"} and category_slug in {"dentists", "pharmacies"}) or (kind == "wedding_package_followup" and category_slug == "salons")) else 0
            matching_signals = [str(signal) for signal in merchant.get("signals", []) if "perf_dip" in str(signal)] if kind == "perf_dip" else []
            signal_bonus = 20 if matching_signals else 0
            payload = trigger.get("payload") or {}
            placeholder_penalty = -10 if payload.get("placeholder") is True and len(payload) <= 2 else 0
            previous = [x for x in sent_by_merchant.get(str(merchant_id), []) if 0 <= now - x[0] < 86400]
            if any(x[1] == trigger.get("suppression_key") for x in previous):
                continue
            fatigue_penalty = -min(30, len(previous) * 12)
            recent_send_penalty = -15 if previous and kind not in {"renewal_due", "regulation_change"} else 0
            merchant_key = str(merchant.get("merchant_id", merchant_id))
            customer_key = str((customer or {}).get("customer_id") or "")
            recent_same_recipient = [x[3] for x in previous if x[2] == customer_key]
            composed = _compose(category, merchant, trigger, customer)
            if _near_duplicate(composed["body"], recent_same_recipient):
                continue
            open_conversation = any(
                str(state.get("merchant_id") or "") == merchant_key
                and state.get("awaiting_reply")
                and not state.get("ended")
                and now - float(state.get("last_activity_at", now)) < 86400
                for state in conversations.values()
            )
            open_conversation_penalty = -20 if open_conversation else 0
            score_components = {
                "urgency": urgency * 10,
                "high_stakes": high_stakes_bonus,
                "category_fit": category_fit_bonus,
                "merchant_signal_match": signal_bonus,
                "placeholder_only": placeholder_penalty,
                "rolling_fatigue": fatigue_penalty,
                "recent_send": recent_send_penalty,
                "open_conversation": open_conversation_penalty,
            }
            score = sum(score_components.values())
            considered.append((score, str(trigger_id), trigger, merchant, category, customer, composed, open_conversation, score_components))
        considered.sort(key=lambda row: (-row[0], row[1]))
        picked_merchants: set[str] = set()
        for score, trigger_id, trigger, merchant, category, customer, composed, open_conversation, score_components in considered:
            merchant_id = str(merchant.get("merchant_id", trigger.get("merchant_id", "")))
            customer_id = str((customer or {}).get("customer_id", ""))
            selected_signals = [str(signal) for signal in merchant.get("signals", []) if "perf_dip" in str(signal)] if trigger.get("kind") == "perf_dip" else []
            if len(actions) >= 20 or score < 15 or merchant_id in picked_merchants or merchant_id in suppressed_merchants or customer_id in suppressed_customers:
                continue
            high_stakes = trigger.get("kind") in {"renewal_due", "regulation_change", "recall_due", "appointment_tomorrow", "supply_alert", "chronic_refill_due"}
            if open_conversation and not (high_stakes and score >= 45):
                continue
            conversation_id = f"conv_{merchant_id}_{trigger_id}"
            merchant_candidates = [row for row in considered if str(row[3].get("merchant_id") or row[2].get("merchant_id") or "") == merchant_id]
            candidate_scores = [{"trigger_id": row[1], "kind": row[2].get("kind"), "score": row[0], "score_components": row[8]} for row in merchant_candidates[:3]]
            score_summary = ", ".join(f"{item['trigger_id']}={item['score']}" for item in candidate_scores)
            breakdown = ", ".join(f"{key} {value:+d}" for key, value in score_components.items() if value)
            composed["rationale"] += f" Priority score {score} ({breakdown}); merchant candidates: {score_summary}; selected the highest-scoring eligible trigger. Engagement lever: {composed['engagement_lever']}."
            action = {"conversation_id": conversation_id, "merchant_id": merchant_id, "customer_id": (customer or {}).get("customer_id"), "send_as": composed["send_as"], "trigger_id": trigger_id, "template_name": "vera_contextual_v1", "template_params": [merchant.get("identity", {}).get("name", ""), trigger.get("kind", ""), str((trigger.get("payload") or {}).get("top_item_id", ""))], **composed}
            action["decision"] = {
                "candidate_scores": candidate_scores,
                "selection_reason": "highest-scoring eligible trigger after expiry, consent, suppression, fatigue, duplicate, and open-conversation checks",
                "engagement_lever": composed["engagement_lever"],
                "selected_score_components": score_components,
                "matched_merchant_signals": selected_signals,
                "interrupted_open_conversation": open_conversation,
            }
            actions.append(action)
            picked_merchants.add(merchant_id)
            sent_by_merchant.setdefault(merchant_id, []).append((now, composed["suppression_key"], customer_id, composed["body"]))
            conversation = conversations.setdefault(conversation_id, {"merchant_id": merchant_id, "customer_id": customer_id, "sent": [], "replies": [], "auto_count": 0, "ended": False, "awaiting_reply": True, "last_trigger": trigger})
            conversation["sent"].append(composed["body"])
            conversation["last_activity_at"] = now
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
        state = conversations.setdefault(body.conversation_id, {"merchant_id": body.merchant_id, "customer_id": body.customer_id, "sent": [], "replies": [], "auto_count": 0, "ended": False, "awaiting_reply": False})
        if state.get("ended"):
            return {"action": "end", "rationale": "This conversation has already been closed; do not restart after an opt-out or terminal state."}
        state["replies"].append(body.message)
        state["awaiting_reply"] = False
        response_at = _epoch(body.received_at)
        state["last_activity_at"] = response_at
        kind = _reply_kind(body.message)
        normalized = re.sub(r"\s+", " ", body.message.strip().lower())
        repeats = sum(re.sub(r"\s+", " ", x.strip().lower()) == normalized for x in state["replies"])

        def send_once(response: dict[str, Any]) -> dict[str, Any]:
            message = str(response.get("body") or "")
            if message and message in state.get("sent", []):
                state["awaiting_reply"] = False
                return {"action": "wait", "rationale": "The next message would repeat an earlier reply; wait for a new signal instead."}
            if message:
                state.setdefault("sent", []).append(message)
                state["awaiting_reply"] = True
                state["last_activity_at"] = response_at
            return response

        if kind == "auto":
            state["auto_count"] = state.get("auto_count", 0) + 1
            merchant_key = str(body.merchant_id or state.get("merchant_id") or body.conversation_id)
            merchant_auto_reply_counts[merchant_key] = merchant_auto_reply_counts.get(merchant_key, 0) + 1
            if state["auto_count"] > 1 or merchant_auto_reply_counts[merchant_key] > 1 or repeats > 1:
                state["ended"] = True
                return {"action": "end", "rationale": "Repeated canned auto-reply detected; stop to avoid wasting turns."}
            state["awaiting_reply"] = True
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
            return send_once({"action": "send", "body": f"Understood. I’ll move ahead with the {task} we discussed. Is that okay?", "cta": "binary_confirm", "rationale": "Recognizes explicit intent and advances directly to confirmation of the already-stated action, with no additional qualification."})
        if kind == "curveball":
            return send_once({"action": "send", "body": "I can’t help with that here, but I can continue with the business update we were discussing. Would you like to do that?", "cta": "open_ended", "rationale": "Politely declines an unrelated request and returns to the single open business question."})
        if kind == "simple":
            if normalized.startswith(("no", "nah")):
                state["ended"] = True
                return {"action": "end", "rationale": "Merchant declined; close politely without another pitch."}
            state["ended"] = True
            return {"action": "end", "rationale": "Acknowledgement received; no additional action is needed."}
        return send_once({"action": "send", "body": "Thanks for sharing that. What would be most useful for you to address first?", "cta": "open_ended", "rationale": "Acknowledges the response and asks one concise question to clarify the merchant’s priority."})


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
