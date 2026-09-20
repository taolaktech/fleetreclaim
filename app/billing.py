"""Stripe subscription billing on top of Firebase identity.

There is no application database: Firebase answers "who is this user" and Stripe
answers "has this user paid". The bridge between them is the Firebase uid, stored
on the Stripe customer as ``metadata.firebaseUid`` and looked up with the Stripe
search API, so the same Google account resolves to the same customer on any
device, after any sign-out, forever.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request

from .auth import AuthUser, require_firebase_user

log = logging.getLogger("billing")

UID_KEY = "firebaseUid"

# Statuses Stripe can report, grouped by what they mean for access.
ENTITLED_STATUSES = {"active", "trialing"}
GRACE_STATUSES = {"past_due"}            # keep access, warn about the payment
BLOCKING_STATUSES = {"incomplete", "incomplete_expired", "unpaid", "canceled", "paused"}
# A checkout should not be started while one of these is on the account.
LIVE_STATUSES = ENTITLED_STATUSES | GRACE_STATUSES | {"incomplete"}


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_env: str

    @property
    def price_id(self) -> str:
        return os.environ.get(self.price_env, "")


# Plan keys are the only thing the browser may send; price ids stay server-side.
PLANS: dict[str, Plan] = {
    "starter": Plan("starter", "Starter", "STRIPE_PRICE_STARTER_MONTHLY"),
    "pro": Plan("pro", "Pro", "STRIPE_PRICE_PRO_MONTHLY"),
}


def configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def available_plans() -> list[dict[str, str]]:
    """Plans with a configured Stripe price, for the browser to render."""
    return [{"key": p.key, "name": p.name} for p in PLANS.values() if p.price_id]


def _stripe() -> Any:
    import stripe

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Billing is not configured on the server.")
    stripe.api_key = key
    stripe.max_network_retries = 2
    return stripe


def _fail(exc: Exception, message: str) -> HTTPException:
    """Log the Stripe detail, hand the user something safe."""
    log.warning("Stripe call failed: %s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=502, detail=message)


def find_customer(user: AuthUser) -> Any | None:
    """The Stripe customer carrying this Firebase uid, or None."""
    stripe = _stripe()
    try:
        found = stripe.Customer.search(
            query=f"metadata['{UID_KEY}']:'{user.uid}'", limit=1
        )
    except Exception as exc:
        raise _fail(exc, "Could not reach Stripe. Try again in a moment.") from exc
    return found.data[0] if found.data else None


def get_or_create_customer(user: AuthUser) -> Any:
    """Idempotent: the same uid always ends up on one customer."""
    existing = find_customer(user)
    if existing:
        return existing
    stripe = _stripe()
    try:
        return stripe.Customer.create(
            email=user.email or None,
            name=user.name or None,
            metadata={UID_KEY: user.uid},
            # Two rapid checkout clicks must not mint two customers.
            idempotency_key=f"customer:{user.uid}",
        )
    except Exception as exc:
        raise _fail(exc, "Could not start billing. Try again in a moment.") from exc


def _plan_for_price(price_id: str) -> Plan | None:
    return next((p for p in PLANS.values() if p.price_id and p.price_id == price_id), None)


def _relevant(subscriptions: list[Any]) -> Any | None:
    """The subscription that decides access: entitled first, then most recent."""
    if not subscriptions:
        return None
    ranked = sorted(
        subscriptions,
        key=lambda s: (s.status in ENTITLED_STATUSES, s.status in GRACE_STATUSES, s.created),
    )
    return ranked[-1]


def _period_end(subscription: Any) -> int | None:
    """Stripe moved the period onto the items in 2025-era API versions."""
    end = subscription.get("current_period_end")
    if end:
        return end
    items = (subscription.get("items") or {}).get("data") or []
    ends = [item.get("current_period_end") for item in items if item.get("current_period_end")]
    return max(ends) if ends else None


def _price_id(subscription: Any) -> str:
    items = (subscription.get("items") or {}).get("data") or []
    return items[0]["price"]["id"] if items else ""


def subscription_status(user: AuthUser) -> dict[str, Any]:
    """Normalized billing state for a Firebase user, straight from Stripe."""
    customer = find_customer(user)
    if customer is None:
        return no_subscription()
    stripe = _stripe()
    try:
        subscriptions = stripe.Subscription.list(customer=customer.id, status="all", limit=20)
    except Exception as exc:
        raise _fail(exc, "Could not read your subscription. Try again in a moment.") from exc

    subscription = _relevant(list(subscriptions.data))
    if subscription is None:
        return no_subscription()

    plan = _plan_for_price(_price_id(subscription))
    status = subscription.status
    return {
        "hasSubscription": True,
        "status": status,
        "isActive": status in ENTITLED_STATUSES,
        "inGrace": status in GRACE_STATUSES,
        "plan": plan.key if plan else "",
        "planName": plan.name if plan else "",
        "cancelAtPeriodEnd": bool(subscription.get("cancel_at_period_end")),
        "currentPeriodEnd": _period_end(subscription),
        "plans": available_plans(),
    }


def no_subscription() -> dict[str, Any]:
    return {
        "hasSubscription": False,
        "status": "none",
        "isActive": False,
        "inGrace": False,
        "plan": "",
        "planName": "",
        "cancelAtPeriodEnd": False,
        "currentPeriodEnd": None,
        "plans": available_plans(),
    }


def _live_subscription(customer_id: str) -> Any | None:
    stripe = _stripe()
    try:
        subscriptions = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    except Exception as exc:
        raise _fail(exc, "Could not read your subscription. Try again in a moment.") from exc
    live = [s for s in subscriptions.data if s.status in LIVE_STATUSES]
    return _relevant(live)


def create_checkout(user: AuthUser, plan_key: str, base_url: str) -> str:
    """Checkout URL for a plan, refusing to sell a second subscription."""
    plan = PLANS.get(plan_key)
    if plan is None or not plan.price_id:
        raise HTTPException(status_code=400, detail="That plan is not available.")

    customer = get_or_create_customer(user)
    if _live_subscription(customer.id) is not None:
        raise HTTPException(
            status_code=409, detail="You already have a subscription — manage it from Billing."
        )

    stripe = _stripe()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer.id,
            line_items=[{"price": plan.price_id, "quantity": 1}],
            success_url=f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/?view=billing",
            client_reference_id=user.uid,
            subscription_data={"metadata": {UID_KEY: user.uid, "plan": plan.key}},
            metadata={UID_KEY: user.uid, "plan": plan.key},
            allow_promotion_codes=True,
        )
    except Exception as exc:
        raise _fail(exc, "Could not start checkout. Try again in a moment.") from exc
    return session.url


def _owned_subscription(user: AuthUser) -> Any:
    customer = find_customer(user)
    subscription = _live_subscription(customer.id) if customer else None
    if subscription is None:
        raise HTTPException(status_code=404, detail="No active subscription to change.")
    return subscription


def set_cancel_at_period_end(user: AuthUser, cancel: bool) -> dict[str, Any]:
    """Cancel keeps access to the end of the paid period; resume undoes it."""
    subscription = _owned_subscription(user)
    stripe = _stripe()
    try:
        stripe.Subscription.modify(subscription.id, cancel_at_period_end=cancel)
    except Exception as exc:
        raise _fail(exc, "Could not update your subscription. Try again in a moment.") from exc
    return subscription_status(user)


def create_portal_session(user: AuthUser, base_url: str) -> str:
    customer = find_customer(user)
    if customer is None:
        raise HTTPException(status_code=404, detail="No billing account yet.")
    stripe = _stripe()
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer.id, return_url=f"{base_url}/?view=billing"
        )
    except Exception as exc:
        raise _fail(exc, "Could not open the billing portal. Try again in a moment.") from exc
    return session.url


def handle_event(event: dict[str, Any]) -> None:
    """Record verified Stripe events.

    Subscription state is never mirrored anywhere: every read goes to Stripe, so
    there is nothing here to keep in sync. Events are logged for operations and
    give later features (emails, analytics) a place to hook in.
    """
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    interesting = {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
    }
    if kind in interesting:
        log.info(
            "Stripe %s customer=%s uid=%s status=%s",
            kind,
            obj.get("customer", ""),
            (obj.get("metadata") or {}).get(UID_KEY, ""),
            obj.get("status", ""),
        )


def gated_features() -> set[str]:
    """Features that need a paid subscription, e.g. ``PAID_FEATURES=parse,crop``."""
    raw = os.environ.get("PAID_FEATURES", "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def requires_subscription(feature: str):
    """FastAPI dependency factory gating one feature behind Stripe.

    Nothing is gated until the feature is named in ``PAID_FEATURES``, so the
    infrastructure ships without changing who can use the app today.
    """

    def dependency(request: Request) -> AuthUser:
        user = require_firebase_user(request)
        if feature not in gated_features() or not configured():
            return user
        state = subscription_status(user)
        if not (state["isActive"] or state["inGrace"]):
            raise HTTPException(status_code=402, detail="This feature needs an active subscription.")
        return user

    return Depends(dependency)


def verify_webhook(payload: bytes, signature: str) -> dict[str, Any]:
    """Construct a Stripe event from the raw body, or 400."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Webhooks are not configured.")
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as exc:
        log.warning("Rejected Stripe webhook: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Invalid signature.") from exc
