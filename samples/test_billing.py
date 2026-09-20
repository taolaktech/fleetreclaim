"""Checks on Stripe billing: uid isolation, plan safety, status, webhooks.

Stripe itself is replaced by a small fake so the logic — not the network — is
under test.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import billing  # noqa: E402
from app.auth import AuthUser  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_ENV = {
    "FIREBASE_PROJECT_ID": "demo-project",
    "FIREBASE_CLIENT_EMAIL": "admin@demo-project.iam.gserviceaccount.com",
    "FIREBASE_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n",
}
STRIPE_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_fake",
    "STRIPE_WEBHOOK_SECRET": "whsec_fake",
    "STRIPE_PRICE_MONTHLY": "price_monthly",
    "STRIPE_PRICE_YEARLY": "price_yearly",
}
ALICE = AuthUser(uid="uid-alice", email="alice@example.com", name="Alice")
BOB = AuthUser(uid="uid-bob", email="bob@example.com", name="Bob")


class Obj(dict):
    """Stripe objects answer to both attribute and key access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class FakeStripe:
    def __init__(self) -> None:
        self.api_key = ""
        self.max_network_retries = 0
        self.customers: list[Obj] = []
        self.subscriptions: list[Obj] = []
        self.modified: list[tuple[str, bool]] = []
        self.idempotency_keys: list[str] = []
        self.checkout_args: dict = {}
        self.portal_args: dict = {}
        self.signature_ok = True

        stripe = self

        class Customer:
            @staticmethod
            def search(query, limit=1):
                uid = query.split("'")[-2]
                hits = [c for c in stripe.customers if c["metadata"].get(billing.UID_KEY) == uid]
                return Obj(data=hits[:limit])

            @staticmethod
            def create(idempotency_key=None, **fields):
                stripe.idempotency_keys.append(idempotency_key)
                customer = Obj(id=f"cus_{len(stripe.customers)}", **fields)
                stripe.customers.append(customer)
                return customer

        class Subscription:
            @staticmethod
            def list(customer, status="all", limit=20):
                return Obj(data=[s for s in stripe.subscriptions if s["customer"] == customer])

            @staticmethod
            def modify(subscription_id, **fields):
                stripe.modified.append((subscription_id, fields["cancel_at_period_end"]))
                for sub in stripe.subscriptions:
                    if sub["id"] == subscription_id:
                        sub.update(fields)
                return Obj(id=subscription_id)

        class Session:
            @staticmethod
            def create(**fields):
                stripe.checkout_args = fields
                return Obj(id="cs_test", url="https://checkout.stripe.test/session")

        class PortalSession:
            @staticmethod
            def create(**fields):
                stripe.portal_args = fields
                return Obj(id="bps_test", url="https://portal.stripe.test/session")

        class Webhook:
            @staticmethod
            def construct_event(payload, signature, secret):
                if not stripe.signature_ok:
                    raise ValueError("bad signature")
                return {"type": "customer.subscription.updated", "data": {"object": {}}}

        self.Customer = Customer
        self.Subscription = Subscription
        self.checkout = types.SimpleNamespace(Session=Session)
        self.billing_portal = types.SimpleNamespace(Session=PortalSession)
        self.Webhook = Webhook

    def add_customer(self, user: AuthUser) -> Obj:
        customer = Obj(
            id=f"cus_{user.uid}", email=user.email, metadata={billing.UID_KEY: user.uid}
        )
        self.customers.append(customer)
        return customer

    def add_subscription(self, customer_id: str, status: str, price="price_yearly", **extra) -> Obj:
        sub = Obj(
            id=f"sub_{len(self.subscriptions)}",
            customer=customer_id,
            status=status,
            created=len(self.subscriptions),
            cancel_at_period_end=False,
            current_period_end=1800000000,
            items={"data": [{"price": {"id": price}, "current_period_end": 1800000000}]},
            **extra,
        )
        self.subscriptions.append(sub)
        return sub


def setup(**env) -> FakeStripe:
    for key in [*ADMIN_ENV, *STRIPE_ENV, "DEV_AUTH_BYPASS", "PAID_FEATURES"]:
        os.environ.pop(key, None)
    os.environ.update(env)
    fake = FakeStripe()
    sys.modules["stripe"] = fake  # type: ignore[assignment]
    return fake


def test_billing_endpoints_reject_anonymous_callers() -> None:
    setup(**ADMIN_ENV, **STRIPE_ENV)
    client = TestClient(app)
    for path in ["/api/billing/checkout", "/api/billing/cancel", "/api/billing/resume",
                 "/api/billing/portal"]:
        assert client.post(path, json={"plan": "yearly"}).status_code == 401, path
    assert client.get("/api/billing/status").status_code == 401


def test_status_is_empty_without_a_stripe_customer() -> None:
    setup(**STRIPE_ENV)
    state = billing.subscription_status(ALICE)
    assert state["hasSubscription"] is False and state["isActive"] is False
    assert [p["key"] for p in state["plans"]] == ["monthly", "yearly"]


def test_status_reflects_the_users_own_subscription_only() -> None:
    fake = setup(**STRIPE_ENV)
    fake.add_subscription(fake.add_customer(ALICE).id, "active")
    fake.add_customer(BOB)

    alice = billing.subscription_status(ALICE)
    assert alice["isActive"] is True and alice["plan"] == "yearly"
    assert billing.subscription_status(BOB)["hasSubscription"] is False


def test_canceled_and_past_due_are_not_treated_as_paid() -> None:
    fake = setup(**STRIPE_ENV)
    customer = fake.add_customer(ALICE)
    fake.add_subscription(customer.id, "canceled")
    assert billing.subscription_status(ALICE)["isActive"] is False

    fake.add_subscription(customer.id, "past_due")
    state = billing.subscription_status(ALICE)
    assert state["isActive"] is False and state["inGrace"] is True


def test_period_end_falls_back_to_the_subscription_item() -> None:
    fake = setup(**STRIPE_ENV)
    sub = fake.add_subscription(fake.add_customer(ALICE).id, "active")
    del sub["current_period_end"]
    assert billing.subscription_status(ALICE)["currentPeriodEnd"] == 1800000000


def test_unknown_and_unconfigured_plans_are_refused() -> None:
    setup(STRIPE_SECRET_KEY="sk_test_fake", STRIPE_PRICE_YEARLY="price_yearly")
    for plan in ["enterprise", "monthly", "price_yearly", ""]:
        try:
            billing.create_checkout(ALICE, plan, "http://localhost:8080")
        except HTTPException as exc:
            assert exc.status_code == 400, plan
        else:
            raise AssertionError(f"accepted {plan!r}")


def test_checkout_uses_the_uid_customer_and_server_side_price() -> None:
    fake = setup(**STRIPE_ENV)
    url = billing.create_checkout(ALICE, "yearly", "http://localhost:8080")
    assert url == "https://checkout.stripe.test/session"
    args = fake.checkout_args
    assert args["mode"] == "subscription"
    assert args["line_items"] == [{"price": "price_yearly", "quantity": 1}]
    assert args["customer"] == fake.customers[0].id
    assert fake.customers[0]["metadata"][billing.UID_KEY] == ALICE.uid
    assert fake.idempotency_keys == [f"customer:{ALICE.uid}"]


def test_existing_customer_is_reused_across_sessions() -> None:
    fake = setup(**STRIPE_ENV)
    existing = fake.add_customer(ALICE)
    billing.create_checkout(ALICE, "yearly", "http://localhost:8080")
    assert len(fake.customers) == 1 and fake.checkout_args["customer"] == existing.id


def test_second_subscription_is_blocked() -> None:
    fake = setup(**STRIPE_ENV)
    fake.add_subscription(fake.add_customer(ALICE).id, "active")
    try:
        billing.create_checkout(ALICE, "yearly", "http://localhost:8080")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("sold a duplicate subscription")


def test_cancel_keeps_access_until_the_period_ends_and_resume_undoes_it() -> None:
    fake = setup(**STRIPE_ENV)
    sub = fake.add_subscription(fake.add_customer(ALICE).id, "active")

    state = billing.set_cancel_at_period_end(ALICE, True)
    assert fake.modified[-1] == (sub.id, True)
    assert state["cancelAtPeriodEnd"] is True and state["isActive"] is True

    state = billing.set_cancel_at_period_end(ALICE, False)
    assert fake.modified[-1] == (sub.id, False)
    assert state["cancelAtPeriodEnd"] is False


def test_cancel_without_a_subscription_is_a_clean_404() -> None:
    setup(**STRIPE_ENV)
    try:
        billing.set_cancel_at_period_end(ALICE, True)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("cancelled a subscription that does not exist")


def test_portal_session_uses_the_callers_own_customer() -> None:
    fake = setup(**STRIPE_ENV)
    fake.add_customer(BOB)
    customer = fake.add_customer(ALICE)
    billing.create_portal_session(ALICE, "http://localhost:8080")
    assert fake.portal_args["customer"] == customer.id


def test_webhook_requires_a_valid_signature() -> None:
    fake = setup(**ADMIN_ENV, **STRIPE_ENV)
    client = TestClient(app)

    fake.signature_ok = False
    bad = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert bad.status_code == 400 and bad.json()["detail"] == "Invalid signature."

    fake.signature_ok = True
    good = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert good.status_code == 200, good.text


def test_cancel_and_resume_endpoints_return_full_billing_state() -> None:
    fake = setup(**STRIPE_ENV, DEV_AUTH_BYPASS="1")
    fake.add_subscription(fake.add_customer(billing_dev_user()).id, "active")
    client = TestClient(app)
    for path, expected in [("/api/billing/cancel", True), ("/api/billing/resume", False)]:
        body = client.post(path).json()
        assert body["billingEnabled"] is True, body
        assert body["cancelAtPeriodEnd"] is expected, body


def billing_dev_user() -> AuthUser:
    from app.auth import DEV_USER

    return DEV_USER


def test_features_are_ungated_until_named_in_paid_features() -> None:
    setup(**STRIPE_ENV, DEV_AUTH_BYPASS="1")
    assert billing.gated_features() == set()
    os.environ["PAID_FEATURES"] = "parse, evidence"
    assert billing.gated_features() == {"parse", "evidence"}


def test_status_endpoint_reports_disabled_billing_without_stripe_keys() -> None:
    setup(DEV_AUTH_BYPASS="1")
    body = TestClient(app).get("/api/billing/status").json()
    assert body["billingEnabled"] is False and body["isActive"] is False


def test_no_stripe_secret_reaches_the_browser() -> None:
    setup(**ADMIN_ENV, **STRIPE_ENV)
    body = TestClient(app).get("/api/config").text
    assert "sk_test_fake" not in body and "whsec_fake" not in body
    assert "price_yearly" not in body
    static = Path(__file__).resolve().parent.parent / "static"
    for path in static.glob("*"):
        if path.is_file():
            assert "sk_" + "test" not in path.read_text(errors="ignore"), path


def main() -> int:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("ok:", name)
    print("billing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
