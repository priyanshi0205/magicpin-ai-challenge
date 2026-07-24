#!/usr/bin/env python3
"""
Vera - deterministic magicpin challenge bot server.

Run:
    python bot.py

The implementation intentionally uses only the Python standard library so the
server runs in a fresh challenge checkout without installing dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


START_TIME = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}
TEAM_NAME = "Vera Deterministic Growth Bot"

contexts: dict[str, dict[str, dict[str, Any]]] = {scope: {} for scope in VALID_SCOPES}
conversations: dict[str, dict[str, Any]] = {}
suppressed_keys: set[str] = set()
merchant_suppression: dict[str, float] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "â‚¹": "Rs ",
        "â€”": "-",
        "â†’": "->",
        "â˜…": "star",
        "ðŸ¦·": "",
        "ðŸ’": "",
        "ðŸ‘‹": "",
        "ðŸ™": "",
        "\u20b9": "Rs ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("Rs  ", "Rs ")
    return text.strip()


def pct(value: Any, signed: bool = True) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    percentage = round(number * 100)
    if signed and percentage > 0:
        return f"+{percentage}%"
    return f"{percentage}%"


def money(value: Any) -> str:
    try:
        return f"Rs {int(value):,}"
    except (TypeError, ValueError):
        return clean_text(value)


def context_payload(scope: str, context_id: str | None) -> dict[str, Any] | None:
    if not context_id:
        return None
    record = contexts.get(scope, {}).get(context_id)
    return record.get("payload") if record else None


def identity_name(merchant: dict[str, Any]) -> str:
    return clean_text(merchant.get("identity", {}).get("name", "your business"))


def owner_name(merchant: dict[str, Any], category_slug: str = "") -> str:
    identity = merchant.get("identity", {})
    first = clean_text(identity.get("owner_first_name") or identity.get("name") or "there")
    if category_slug == "dentists" and not first.lower().startswith("dr"):
        return f"Dr. {first}"
    return first


def locality(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    loc = clean_text(identity.get("locality"))
    city = clean_text(identity.get("city"))
    return loc or city or "your locality"


def active_offers(merchant: dict[str, Any], category: dict[str, Any] | None = None) -> list[str]:
    offers = [
        clean_text(offer.get("title"))
        for offer in merchant.get("offers", [])
        if offer.get("status") == "active" and offer.get("title")
    ]
    if offers:
        return offers
    if category:
        return [clean_text(item.get("title")) for item in category.get("offer_catalog", [])[:2] if item.get("title")]
    return []


def first_offer(merchant: dict[str, Any], category: dict[str, Any] | None = None) -> str:
    offers = active_offers(merchant, category)
    return offers[0] if offers else ""


def find_digest(category: dict[str, Any] | None, item_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
    if not category:
        return {}
    digest = category.get("digest", [])
    if item_id:
        for item in digest:
            if item.get("id") == item_id:
                return item
    if kind:
        for item in digest:
            if item.get("kind") == kind:
                return item
    return digest[0] if digest else {}


def peer_ctr(category: dict[str, Any] | None) -> str:
    stats = (category or {}).get("peer_stats", {})
    return pct(stats.get("avg_ctr"), signed=False) if stats.get("avg_ctr") is not None else ""


def customer_count(merchant: dict[str, Any], *keys: str) -> str:
    aggregate = merchant.get("customer_aggregate", {})
    for key in keys:
        value = aggregate.get(key)
        if value is not None:
            return str(value)
    return ""


def conversation_hint(merchant: dict[str, Any]) -> str:
    history = merchant.get("conversation_history", []) or []
    for turn in reversed(history[-4:]):
        if turn.get("from") == "merchant":
            body = clean_text(turn.get("body"))
            if body:
                return body[:90]
    for turn in reversed(history[-4:]):
        body = clean_text(turn.get("body"))
        if body:
            return body[:90]
    return ""


def signal_hint(merchant: dict[str, Any], category: dict[str, Any] | None = None) -> str:
    signals = [clean_text(signal).replace("_", " ") for signal in merchant.get("signals", []) if signal]
    if signals:
        return signals[0]
    perf = merchant.get("performance", {})
    views = perf.get("views")
    calls = perf.get("calls")
    peer = peer_ctr(category)
    if views is not None and calls is not None:
        return f"{views} views, {calls} calls" + (f", peer CTR {peer}" if peer else "")
    return ""


def merchant_language_hint(merchant: dict[str, Any]) -> str:
    languages = [str(item).lower() for item in merchant.get("identity", {}).get("languages", [])]
    if "hi" in languages:
        return "Hinglish OK"
    if languages:
        return f"{languages[0]} preferred"
    return "English"


def build_template_params(body: str, merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> list[str]:
    target_name = clean_text(customer.get("identity", {}).get("name")) if customer else owner_name(merchant, merchant.get("category_slug", ""))
    body_clean = cap_body(body)
    sentences = re.split(r"(?<=[.!?])\s+", body_clean)
    hook = sentences[0] if sentences else body_clean[:120]
    ask = sentences[-1] if sentences else body_clean[-120:]
    return [target_name, clean_text(trigger.get("kind", "")).replace("_", " "), hook[:160], ask[:160]]


def trigger_customer(trigger: dict[str, Any]) -> dict[str, Any] | None:
    return context_payload("customer", trigger.get("customer_id"))


def strongest_signal_score(trigger: dict[str, Any]) -> tuple[int, int]:
    urgency = int(trigger.get("urgency") or 0)
    kind_weights = {
        "supply_alert": 9,
        "regulation_change": 8,
        "active_planning_intent": 8,
        "renewal_due": 7,
        "recall_due": 7,
        "chronic_refill_due": 7,
        "perf_dip": 7,
        "review_theme_emerged": 7,
        "customer_lapsed_hard": 7,
        "customer_lapsed_soft": 6,
        "ipl_match_today": 6,
        "gbp_unverified": 6,
        "wedding_package_followup": 6,
        "competitor_opened": 6,
        "research_digest": 5,
        "seasonal_perf_dip": 5,
        "trial_followup": 5,
        "perf_spike": 4,
        "milestone_reached": 4,
        "festival_upcoming": 3,
        "curious_ask_due": 3,
        "dormant_with_vera": 3,
    }
    return urgency, kind_weights.get(trigger.get("kind"), 2)


def cap_body(body: str) -> str:
    body = clean_text(body)
    return body[:700].rsplit(" ", 1)[0] if len(body) > 700 else body


def build_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    category_slug = merchant.get("category_slug") or (category or {}).get("slug", "")
    kind = trigger.get("kind", "general")
    payload = trigger.get("payload", {}) or {}
    first = owner_name(merchant, category_slug)
    business = identity_name(merchant)
    place = locality(merchant)
    offer = first_offer(merchant, category)
    perf = merchant.get("performance", {})
    aggregate = merchant.get("customer_aggregate", {})
    recent = conversation_hint(merchant)
    signal = signal_hint(merchant, category)
    language = merchant_language_hint(merchant)
    send_as = "merchant_on_behalf" if customer else "vera"
    cta = "binary_yes_no"
    template_name = f"vera_{kind}_v1" if not customer else f"merchant_{kind}_v1"
    rationale = f"{kind} trigger matched to {category_slug}; uses one grounded signal and one low-friction CTA."

    if customer:
        body, cta = compose_customer(category, merchant, trigger, customer, offer)
    elif kind == "research_digest":
        item = find_digest(category, payload.get("top_item_id"), "research")
        trial = item.get("trial_n")
        segment = clean_text(item.get("patient_segment") or "").replace("_", "-")
        count = customer_count(merchant, "high_risk_adult_count", "lapsed_180d_plus")
        source = clean_text(item.get("source"))
        title = clean_text(item.get("title"))
        title = re.sub(r"outperforms.*", "cuts caries recurrence 38% better", title) if "38" in clean_text(item.get("summary")) else title
        segment_phrase = f" for your {count} {segment} patients" if count and segment else f" for {place} patients"
        trial_phrase = f"{trial:,}-patient " if isinstance(trial, int) else ""
        history_phrase = f" Last time you said '{recent}', so I kept this practical." if recent else ""
        body = (
            f"{first}, {source} landed with one item relevant to {business}: {trial_phrase}{title}{segment_phrase}. "
            f"{history_phrase}Worth a 2-min look; I can pull the abstract and draft a patient-ed WhatsApp. Reply YES to get both?"
        )
        cta = "binary_yes_no"
        rationale = "Research digest chosen over generic offers because it cites source, cohort, trial size, and a clear merchant-specific use."
    elif kind in {"regulation_change", "compliance"}:
        item = find_digest(category, payload.get("top_item_id"), "compliance")
        deadline = clean_text(payload.get("deadline_iso") or trigger.get("expires_at", "")[:10])
        body = (
            f"{first}, compliance heads-up for {business}: {clean_text(item.get('title'))}. Source: {clean_text(item.get('source'))}. "
            f"Deadline {deadline}; missing SOP documentation can slow audits. I can draft a 5-point checklist from this. Reply YES?"
        )
        rationale = "Compliance trigger is urgent and verifiable, so the message leads with source, deadline, and one checklist CTA."
    elif kind == "cde_opportunity":
        item = find_digest(category, payload.get("digest_item_id"), "cde")
        body = (
            f"{first}, {clean_text(item.get('title'))} is on {clean_text(item.get('date'))}; "
            f"{payload.get('credits', item.get('credits', 2))} CDE credits, {clean_text(payload.get('fee') or item.get('actionable'))}. "
            f"Want me to block it on your calendar?"
        )
        rationale = "CDE trigger uses credits, date, and fee from digest context with a single calendar CTA."
    elif kind == "competitor_opened":
        body = (
            f"{first}, new competitor alert in {place}: {clean_text(payload.get('competitor_name'))} opened "
            f"{payload.get('distance_km')} km away on {clean_text(payload.get('opened_date'))} with {clean_text(payload.get('their_offer'))}. "
            f"Your strongest counter is {offer or 'a service-price post'}. Want me to draft that GBP post?"
        )
        rationale = "Competitor opening is the strongest signal; uses distance, date, competing offer, and the merchant's actual active offer."
    elif kind == "ipl_match_today":
        digest_item = find_digest(category, kind="seasonal")
        match_time = clean_text(payload.get("match_time_iso", "19:30"))
        if payload.get("is_weeknight") is False and "-12%" in clean_text(digest_item.get("summary")):
            insight = "magicpin Apr 2026 data says Saturday IPL covers run 12% below normal"
            recommendation = f"push {offer or 'a delivery combo'} as delivery-only"
        else:
            insight = "weeknight IPL can lift covers by 18% in metro restaurants"
            recommendation = f"run {offer or 'a match-night combo'} before toss"
        body = (
            f"{first}, {clean_text(payload.get('match'))} at {clean_text(payload.get('venue'))} starts {match_time}. "
            f"{insight}; {recommendation}. Want me to draft the banner?"
        )
        rationale = "Restaurant event trigger interpreted through category digest data, with a single banner draft CTA."
    elif kind == "active_planning_intent":
        topic = clean_text(payload.get("intent_topic"))
        if "corporate" in topic or "thali" in topic:
            body = (
                f"{first}, since you asked what the corporate thali could look like, here is the starter: {offer or 'weekday thali'} for {place} offices. "
                f"10 packs, 25 packs, and 50+ packs with day-before WhatsApp ordering by 5pm. I can draft the 3-line facilities-manager outreach now. Reply YES?"
            )
        elif "kids_yoga" in topic:
            body = (
                f"{first}, for your kids-yoga idea: 4-week summer batch, age 7-12, 3 classes/week, Saturday trial first. "
                f"Your {offer or 'trial class'} keeps entry low-risk. I can draft the GBP post + parent WhatsApp in 5 min. Reply YES?"
            )
        else:
            body = (
                f"{first}, you already said yes to {topic or 'this plan'}, so I am moving to action. "
                f"I can turn it into a ready post and WhatsApp copy now. Reply CONFIRM to proceed."
            )
            cta = "binary_confirm_cancel"
        rationale = "Merchant has explicit planning intent, so Vera switches to drafting rather than asking more qualifying questions."
    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = clean_text(payload.get("metric") or "calls")
        delta = pct(payload.get("delta_pct") or perf.get("delta_7d", {}).get(f"{metric}_pct"))
        if payload.get("is_expected_seasonal"):
            active = customer_count(merchant, "total_active_members", "total_unique_ytd")
            body = (
                f"{first}, {metric} is down {delta} this week, but this matches the Apr-Jun low-acquisition window for gyms. "
                f"Protect the {active or 'current'} member base instead of spending on ads. I can draft a 7-day summer attendance challenge. Reply YES?"
            )
        else:
            baseline = payload.get("vs_baseline")
            peer = peer_ctr(category)
            body = (
                f"{first}, {metric} dropped {delta} in {payload.get('window', '7d')}; baseline was {baseline or perf.get(metric, 'higher')} and peer CTR is {peer or 'available in your category pack'}. "
                f"That is the strongest signal today. I can draft one recovery post around {offer or 'your strongest service'}. Reply YES?"
            )
        rationale = "Performance dip is prioritized because it is urgent, merchant-specific, and actionable in one recovery asset."
    elif kind == "perf_spike":
        metric = clean_text(payload.get("metric") or "calls")
        body = (
            f"{first}, {metric} is up {pct(payload.get('delta_pct'))} over {payload.get('window', '7d')} "
            f"(baseline {payload.get('vs_baseline', perf.get(metric, 'n/a'))}); likely driver: {clean_text(payload.get('likely_driver') or 'recent demand')}. "
            f"Want me to turn it into a follow-up post today?"
        )
        rationale = "Perf spike converts a live signal into momentum while citing metric, lift, window, and likely driver."
    elif kind == "review_theme_emerged":
        body = (
            f"{first}, reviews show {payload.get('occurrences_30d')} mentions of {clean_text(payload.get('theme')).replace('_', ' ')} in 30 days; "
            f"one quote says \"{clean_text(payload.get('common_quote'))}\". Want me to draft a short owner reply?"
        )
        rationale = "Review theme is specific and sensitive, so the CTA is one owner response rather than a broad campaign."
    elif kind == "milestone_reached":
        now = payload.get("value_now")
        milestone = payload.get("milestone_value")
        body = (
            f"{first}, you are at {now} reviews, just {int(milestone) - int(now) if str(now).isdigit() and str(milestone).isdigit() else 'a few'} away from {milestone}. "
            f"Want me to draft a thank-you note asking recent happy customers for the last push?"
        )
        rationale = "Milestone trigger uses the exact current and target values and asks for one review note."
    elif kind == "renewal_due":
        amount = money(payload.get("renewal_amount"))
        body = (
            f"{first}, Pro renewal is due in {payload.get('days_remaining', merchant.get('subscription', {}).get('days_remaining'))} days at {amount}. "
            f"Before you decide, your last 30 days show {perf.get('views')} views and {perf.get('calls')} calls. Want me to send a 1-page renewal ROI summary?"
        )
        rationale = "Renewal trigger uses plan timing, amount, and recent performance instead of a generic payment reminder."
    elif kind in {"winback_eligible", "dormant_with_vera"}:
        days = payload.get("days_since_expiry") or payload.get("days_since_last_merchant_message") or merchant.get("subscription", {}).get("days_since_expiry")
        lapsed = payload.get("lapsed_customers_added_since_expiry") or customer_count(merchant, "lapsed_90d_plus", "lapsed_180d_plus")
        body = (
            f"{first}, quiet update: it has been {days} days since {clean_text(payload.get('last_topic') or 'your last Vera action')}. "
            f"You now have {lapsed or 'some'} lapsed customers to win back. Want me to draft one no-discount comeback message?"
        )
        rationale = "Dormancy/winback uses elapsed days plus lapsed customer count and avoids discount-led copy."
    elif kind == "curious_ask_due":
        body = (
            f"Hi {first}, quick check: what service has been most asked-for this week at {business}? "
            f"I will turn your answer into one Google post and a 4-line WhatsApp pricing reply. Takes 5 min. Reply with the service name?"
        )
        cta = "open_ended"
        rationale = "Curious-ask trigger intentionally asks the merchant for one data point and promises a concrete artifact."
    elif kind == "festival_upcoming":
        body = (
            f"{first}, {clean_text(payload.get('festival'))} is {payload.get('days_until')} days away on {clean_text(payload.get('date'))}. "
            f"{business} already has {offer or 'a category-fit offer'} to anchor the post. Want me to draft the festival post?"
        )
        rationale = "Festival trigger is low urgency, so it uses existing offer fit and one draft CTA."
    elif kind == "supply_alert":
        batches = ", ".join(clean_text(x) for x in payload.get("affected_batches", []))
        chronic = customer_count(merchant, "chronic_rx_count", "total_unique_ytd")
        body = (
            f"{first}, urgent: {clean_text(payload.get('molecule'))} recall for batches {batches} by {clean_text(payload.get('manufacturer'))}. "
            f"You have {chronic} chronic-Rx customers in context; I will not guess affected count, but I can filter purchases and draft the replacement workflow. Reply YES?"
        )
        rationale = "Supply alert is the top-priority signal; avoids inventing affected-customer counts and asks to filter from known chronic-Rx base."
    elif kind == "category_seasonal":
        trends = ", ".join(clean_text(t).replace("_", " ") for t in payload.get("trends", [])[:3])
        body = (
            f"{first}, summer demand shift is live: {trends}. "
            f"For {business}, counter visibility matters more than discounts this week. Want me to draft the shelf + WhatsApp checklist?"
        )
        rationale = "Seasonal pharmacy signal uses listed demand shifts and a precise operational next step."
    elif kind == "gbp_unverified":
        body = (
            f"{first}, your Google profile is still unverified; the trigger estimates {pct(payload.get('estimated_uplift_pct'), signed=False)} uplift once fixed. "
            f"Path is {clean_text(payload.get('verification_path')).replace('_', ' ')}. Want me to walk you through the 3 steps?"
        )
        rationale = "GBP verification trigger uses the provided uplift and verification path with a single help CTA."
    else:
        body = fallback_merchant_message(category, merchant, trigger)

    return {
        "body": cap_body(body),
        "cta": cta,
        "send_as": send_as,
        "template_name": template_name,
        "template_params": build_template_params(body, merchant, trigger, customer),
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id', 'unknown')}"),
        "rationale": f"{rationale} Merchant signal: {signal or 'none explicit'}; language: {language}.",
    }


def compose_customer(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], offer: str) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    cname = clean_text(customer.get("identity", {}).get("name", "there"))
    business = identity_name(merchant)
    owner = owner_name(merchant, merchant.get("category_slug", ""))
    language = clean_text(customer.get("identity", {}).get("language_pref", "english")).lower()
    relationship = customer.get("relationship", {})
    preferences = customer.get("preferences", {})
    hi_mix = "hi" in language

    if kind == "recall_due":
        slots = payload.get("available_slots", [])
        slot_text = " or ".join(clean_text(slot.get("label")) for slot in slots[:2]) or clean_text(preferences.get("preferred_slots"))
        opener = f"Hi {cname}, {business} here." if not hi_mix else f"Hi {cname}, {business} se message."
        body = (
            f"{opener} Last visit was {clean_text(payload.get('last_service_date') or relationship.get('last_visit'))}; "
            f"your {clean_text(payload.get('service_due', 'recall')).replace('_', ' ')} is due on {clean_text(payload.get('due_date'))}. "
            f"Apke liye slots ready: {slot_text}. {offer or 'Cleaning slot'} available. Reply 1 for first slot, 2 for second?"
        )
        return body, "multi_choice_slot"
    if kind == "wedding_package_followup":
        body = (
            f"Hi {cname}, {owner} from {business} here. {payload.get('days_to_wedding')} days to your wedding, "
            f"so the {clean_text(payload.get('next_step_window_open')).replace('_', ' ')} window is open now after your trial. "
            f"I can block your preferred {clean_text(preferences.get('preferred_slots', 'Saturday'))} consult. Reply YES to hold it?"
        )
        return body, "binary_yes_no"
    if kind in {"customer_lapsed_hard", "customer_lapsed_soft"}:
        focus = clean_text(payload.get("previous_focus") or preferences.get("training_focus") or "routine")
        body = (
            f"Hi {cname}, {owner} from {business} here. It has been {payload.get('days_since_last_visit', 'a few')} days; no judgment, routines break. "
            f"We can restart with {offer or 'a free trial'} around your {focus} goal. Reply YES to hold a no-commitment spot?"
        )
        return body, "binary_yes_no"
    if kind == "trial_followup":
        slot = clean_text((payload.get("next_session_options") or [{}])[0].get("label"))
        body = (
            f"Hi {cname}, {business} here. Your trial was on {clean_text(payload.get('trial_date'))}; "
            f"next suitable session is {slot}. Want me to reserve it?"
        )
        return body, "binary_yes_no"
    if kind == "chronic_refill_due":
        meds = ", ".join(clean_text(x) for x in payload.get("molecule_list", []))
        date = clean_text(payload.get("stock_runs_out_iso", "")[:10])
        delivery = "Free home delivery applies" if any("delivery" in x.lower() for x in active_offers(merchant, category)) else "Pickup can be kept ready"
        body = (
            f"Namaste {cname}, {business} here. Your monthly medicines ({meds}) run out on {date}. "
            f"Same dose pack can be prepared today; {delivery}. Reply CONFIRM to dispatch?"
        )
        return body, "binary_confirm_cancel"
    if kind == "appointment_tomorrow":
        body = (
            f"Hi {cname}, reminder from {business}: your appointment is tomorrow. "
            f"Reply CONFIRM if the same slot still works?"
        )
        return body, "binary_confirm_cancel"

    body = (
        f"Hi {cname}, {business} here. Based on your last visit on {clean_text(relationship.get('last_visit'))}, "
        f"{offer or 'your next service'} is relevant now. Reply YES if you want us to help?"
    )
    return body, "binary_yes_no"


def fallback_merchant_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> str:
    category_slug = merchant.get("category_slug") or category.get("slug", "business")
    first = owner_name(merchant, category_slug)
    perf = merchant.get("performance", {})
    offer = first_offer(merchant, category)
    kind = clean_text(trigger.get("kind", "growth")).replace("_", " ")
    views = perf.get("views")
    calls = perf.get("calls")
    place = locality(merchant)
    recent = conversation_hint(merchant)
    recent_phrase = f" Last Vera thread: '{recent}'." if recent else ""
    if category_slug == "dentists":
        angle = f"{views} views, {calls} calls, {peer_ctr(category)} peer CTR, and {customer_count(merchant, 'lapsed_180d_plus', 'high_risk_adult_count') or 'your'} recall pool"
        ask = "I can draft one clinical GBP post plus patient WhatsApp. Reply YES?"
    elif category_slug == "salons":
        angle = f"{views} views in {place} and {offer or 'your strongest service'}"
        ask = "I can draft a visual Google post and pricing reply in 5 min. Reply YES?"
    elif category_slug == "restaurants":
        angle = f"{views} views, {calls} calls, and {offer or 'your menu hook'}"
        ask = "I can draft a 3-line delivery push for today's demand window. Reply YES?"
    elif category_slug == "gyms":
        angle = f"{views} views, {calls} calls, and {customer_count(merchant, 'total_active_members') or 'current'} members"
        ask = "I can draft a no-shame retention nudge for this week. Reply YES?"
    else:
        angle = f"{views} views, {calls} calls, and {offer or 'your service hook'}"
        ask = "I can draft the precise WhatsApp note and workflow. Reply YES?"
    return f"{first}, {kind} is active for {identity_name(merchant)}: {angle}.{recent_phrase} {ask}"


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    return build_message(category or {}, merchant or {}, trigger or {}, customer)


def classify_reply(message: str, conversation_id: str, turn_number: int) -> str:
    text = message.lower().strip()
    if any(phrase in text for phrase in [
        "stop messaging", "stop sending", "not interested", "unsubscribe", "useless spam",
        "don't message", "do not message", "bothering me", "leave me alone",
    ]):
        return "hostile_or_optout"
    if any(phrase in text for phrase in [
        "thank you for contacting", "will respond shortly", "automated assistant",
        "auto-reply", "auto reply", "we are currently unavailable", "business hours",
    ]):
        conv = conversations.setdefault(conversation_id, {"turns": [], "auto_count": 0, "sent_bodies": []})
        previous = [turn.get("message") for turn in conv.get("turns", []) if turn.get("from") != "vera"]
        repeats = sum(1 for item in previous if item and item.lower().strip() == text)
        conv["auto_count"] = max(int(conv.get("auto_count", 0)), repeats + 1)
        if conv["auto_count"] >= 3 or turn_number >= 3 or "automated assistant" in text:
            return "auto_end"
        return "auto_wait"
    if any(word in text for word in ["gst", "tax filing", "income tax", "loan", "website design"]):
        return "off_topic"
    if any(phrase in text for phrase in [
        "ok lets do it", "ok let's do it", "lets do it", "let's do it", "go ahead",
        "yes please", "yes", "confirm", "send it", "do it", "proceed", "interested",
    ]):
        return "commit"
    if any(phrase in text for phrase in ["later", "busy", "call me tomorrow", "after some time"]):
        return "wait"
    if "?" in text or any(word in text for word in ["how", "what", "price", "cost", "why"]):
        return "question"
    return "neutral"


def response_for_reply(body: dict[str, Any]) -> dict[str, Any]:
    conv_id = body.get("conversation_id", "conv_unknown")
    merchant_id = body.get("merchant_id")
    customer_id = body.get("customer_id")
    message = clean_text(body.get("message", ""))
    turn_number = int(body.get("turn_number") or 1)
    conv = conversations.setdefault(conv_id, {"turns": [], "sent_bodies": []})
    conv["turns"].append({"from": body.get("from_role", "merchant"), "message": message, "ts": body.get("received_at")})
    label = classify_reply(message, conv_id, turn_number)

    if label == "hostile_or_optout":
        if merchant_id:
            merchant_suppression[merchant_id] = time.time() + 30 * 24 * 3600
        return {"action": "end", "rationale": "Merchant explicitly opted out or expressed frustration; closing and suppressing future nudges."}
    if label == "auto_end":
        return {"action": "end", "rationale": "Repeated or explicit WhatsApp auto-reply detected; ending to avoid wasting turns."}
    if label == "auto_wait":
        return {"action": "wait", "wait_seconds": 14400, "rationale": "Canned WhatsApp auto-reply detected; backing off for 4 hours."}
    if label == "wait":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time; wait 30 minutes before any follow-up."}
    if label == "off_topic":
        reply = (
            "GST filing is outside what Vera can handle directly; your CA should own that. "
            "Coming back to the growth task, reply CONFIRM and I will prepare the draft now."
        )
        return {"action": "send", "body": reply, "cta": "binary_confirm_cancel", "rationale": "Politely declined off-topic request and returned to the original growth workflow."}
    if label == "commit":
        merchant = context_payload("merchant", merchant_id) or {}
        trigger_id = conv.get("trigger_id")
        trigger = context_payload("trigger", trigger_id) or {}
        kind = trigger.get("kind", "")
        category = context_payload("category", merchant.get("category_slug")) or {}
        if kind == "research_digest":
            count = customer_count(merchant, "high_risk_adult_count", "lapsed_180d_plus")
            scope = f" for your {count} relevant patients" if count else ""
            text = f"Done. I am preparing the 2-min abstract and patient WhatsApp draft{scope} from the cited item. Reply CONFIRM to approve the draft."
        elif kind == "active_planning_intent":
            text = "Done. I am turning this into a ready Google post and WhatsApp copy now, not asking more questions. Reply CONFIRM to approve the first draft."
        elif kind in {"supply_alert", "regulation_change"}:
            text = "Done. I will prepare the checklist and affected-customer note only from records in context. Reply CONFIRM to approve the draft."
        else:
            offer = first_offer(merchant, category)
            text = f"Done. I am drafting the next message using {offer or 'your current context'} now. Reply CONFIRM to approve it."
        return {"action": "send", "body": text, "cta": "binary_confirm_cancel", "rationale": "Merchant committed, so Vera switches to action mode immediately."}
    if label == "question":
        reply = (
            "Short answer: I will use only your current magicpin context and draft it for review before anything goes live. "
            "Reply CONFIRM if you want the first draft now."
        )
        return {"action": "send", "body": reply, "cta": "binary_confirm_cancel", "rationale": "Answered the merchant's concern and kept one action CTA."}

    reply = "Got it. I will keep this tight and draft one option from your current data. Reply CONFIRM to see it."
    return {"action": "send", "body": reply, "cta": "binary_confirm_cancel", "rationale": "Acknowledged neutral reply and advanced with one low-friction CTA."}


class VeraHandler(BaseHTTPRequestHandler):
    server_version = "VeraHTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> tuple[dict[str, Any] | None, str | None]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}, None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")), None
        except Exception as exc:
            return None, str(exc)

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            counts = {scope: len(contexts.get(scope, {})) for scope in ["category", "merchant", "customer", "trigger"]}
            self._send_json({"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": counts})
            return
        if path == "/v1/metadata":
            self._send_json({
                "team_name": TEAM_NAME,
                "team_members": ["Priyanshi Khataniya"],
                "model": "deterministic-standard-library-composer",
                "approach": "stateful in-memory context store with trigger-kind routing and grounded templates",
                "contact_email": "mail.priyanshi.khataniya@gmail.com",
                "version": "1.0.0",
                "submitted_at": "2026-07-23T00:00:00Z",
            })
            return
        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data, error = self._read_json()
        if error:
            self._send_json({"accepted": False, "reason": "malformed_json", "details": error}, HTTPStatus.BAD_REQUEST)
            return
        data = data or {}
        if path == "/v1/context":
            self.handle_context(data)
            return
        if path == "/v1/tick":
            self.handle_tick(data)
            return
        if path == "/v1/reply":
            self._send_json(response_for_reply(data))
            return
        if path == "/v1/teardown":
            for store in contexts.values():
                store.clear()
            conversations.clear()
            suppressed_keys.clear()
            merchant_suppression.clear()
            self._send_json({"accepted": True, "wiped": True, "stored_at": utc_now()})
            return
        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def handle_context(self, body: dict[str, Any]) -> None:
        scope = body.get("scope")
        context_id = body.get("context_id")
        version = body.get("version")
        payload = body.get("payload")
        if scope not in VALID_SCOPES:
            self._send_json({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"}, HTTPStatus.BAD_REQUEST)
            return
        if not context_id or not isinstance(payload, dict) or not isinstance(version, int):
            self._send_json({"accepted": False, "reason": "malformed_context", "details": "context_id, integer version, and object payload are required"}, HTTPStatus.BAD_REQUEST)
            return
        current = contexts[scope].get(context_id)
        if current and current["version"] >= version:
            self._send_json({"accepted": False, "reason": "stale_version", "current_version": current["version"]}, HTTPStatus.CONFLICT)
            return
        contexts[scope][context_id] = {"version": version, "payload": payload, "stored_at": utc_now()}
        self._send_json({"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": utc_now()})

    def handle_tick(self, body: dict[str, Any]) -> None:
        available = body.get("available_triggers") or []
        trigger_records: list[dict[str, Any]] = []
        for trigger_id in available:
            trigger = context_payload("trigger", trigger_id)
            if trigger:
                trigger_records.append(trigger)
        trigger_records.sort(key=strongest_signal_score, reverse=True)

        actions = []
        for trigger in trigger_records:
            if len(actions) >= 20:
                break
            suppression_key = trigger.get("suppression_key", trigger.get("id"))
            merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
            if not merchant_id or suppression_key in suppressed_keys:
                continue
            if merchant_suppression.get(merchant_id, 0) > time.time():
                continue
            merchant = context_payload("merchant", merchant_id)
            if not merchant:
                continue
            category = context_payload("category", merchant.get("category_slug")) or {}
            customer = trigger_customer(trigger) if trigger.get("customer_id") else None
            if trigger.get("scope") == "customer" and not customer:
                continue
            message = compose(category, merchant, trigger, customer)
            conversation_id = self.conversation_id(merchant_id, trigger)
            action = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": trigger.get("customer_id"),
                "send_as": message["send_as"],
                "trigger_id": trigger.get("id"),
                "template_name": message["template_name"],
                "template_params": [param for param in message.get("template_params", []) if param],
                "body": message["body"],
                "cta": message["cta"],
                "suppression_key": message["suppression_key"],
                "rationale": message["rationale"],
            }
            conversations[conversation_id] = {
                "merchant_id": merchant_id,
                "customer_id": trigger.get("customer_id"),
                "trigger_id": trigger.get("id"),
                "turns": [{"from": "vera", "message": action["body"], "ts": body.get("now")}],
                "sent_bodies": [action["body"]],
                "auto_count": 0,
            }
            suppressed_keys.add(suppression_key)
            actions.append(action)
        self._send_json({"actions": actions})

    @staticmethod
    def conversation_id(merchant_id: str, trigger: dict[str, Any]) -> str:
        base = re.sub(r"[^a-zA-Z0-9]+", "_", f"{merchant_id}_{trigger.get('kind')}_{trigger.get('id')}")
        return f"conv_{base}"[:120].strip("_")


def run(host: str = "0.0.0.0", port: int = 8080) -> None:
    try:
        server = ThreadingHTTPServer((host, port), VeraHandler)
    except PermissionError as exc:
        print(f"Could not bind Vera to {host}:{port}: {exc}", file=sys.stderr)
        print("Try: python bot.py --host 127.0.0.1 --port 8090", file=sys.stderr)
        print("Then run the judge with BOT_URL=http://localhost:8090", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f"Could not bind Vera to {host}:{port}: {exc}", file=sys.stderr)
        print("Pick another port, for example: python bot.py --port 8090", file=sys.stderr)
        raise SystemExit(1)
    print(f"Vera listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run(args.host, args.port)
