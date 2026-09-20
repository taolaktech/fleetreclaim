"""Check the browser side of auth against a server started with Firebase config.

Run a second server with Firebase web config set (a throwaway project id is fine,
no sign-in happens here) and point BASE at it.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8081")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:29229")
        page = browser.contexts[0].new_page()

        # Signed out: the dashboard must never show, only the login page.
        page.goto(BASE, wait_until="load")
        page.wait_for_url("**/login", timeout=30_000)
        assert page.locator("#googleBtn").is_visible(), "no Continue with Google button"
        print("signed out ->", page.url, "|", page.locator("#googleBtn").inner_text().strip())

        # The dashboard markup must stay hidden until Firebase has answered.
        page.goto(BASE, wait_until="commit")
        hidden = page.locator("#app.hidden").count()
        print("dashboard hidden on load:", hidden, "| url:", page.url)
        page.wait_for_url("**/login", timeout=30_000)

        # Nothing Firebase-Admin-shaped may reach the browser.
        config = page.evaluate("fetch('/api/config').then(r => r.text())")
        for secret in ("private_key", "BEGIN PRIVATE KEY", "client_email", "iam.gserviceaccount.com"):
            assert secret not in config, f"{secret} exposed to the browser"
        print("config keys:", sorted(page.evaluate(
            "fetch('/api/config').then(r => r.json()).then(j => Object.keys(j.firebase))")))

        status = page.evaluate(
            "fetch('/api/match', {method:'POST',headers:{'Content-Type':'application/json'},"
            "body:'{\"tolls\":[],\"trips\":[]}'}).then(r => r.status)")
        assert status == 401, status
        print("unauthenticated /api/match ->", status)
        page.close()
    print("auth UI check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
