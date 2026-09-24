import os
import re
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

PORT_COUNTER = [18776]


def next_port():
    PORT_COUNTER[0] += 1
    return PORT_COUNTER[0]


def start_server(data_dir, port, extra=None):
    env = {**os.environ, "GEARSMITH_DATA_DIR": str(data_dir), "GEARSMITH_NOTIFY_WORKER": "false", **(extra or {})}
    proc = subprocess.Popen(["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(url + "/api/status").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return proc, url


@pytest.fixture()
def app_url(tmp_path):
    proc, url = start_server(tmp_path / "data", next_port())
    yield url
    proc.terminate()
    proc.wait(timeout=5)


def sign_in(page, url):
    page.goto(url)
    page.locator("[name=username]").wait_for()
    page.locator("[name=username]").fill("admin-test")
    page.locator("[name=password]").fill("password-123")
    if page.get_by_role("heading", name=re.compile("Set up")).count():
        page.get_by_role("button", name="Create administrator").click()
    else:
        page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Gear", exact=True)).to_be_visible()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_main_screens(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)

        # Gear list: seed gear grouped by type, both chip states visible
        expect(page.get_by_role("heading", name=re.compile("Guitars"))).to_be_visible()
        expect(page.get_by_role("link", name=re.compile("Starling"))).to_be_visible()
        expect(page.locator(".string-chip.overdue").first).to_be_visible()
        expect(page.locator(".string-chip.fresh").first).to_be_visible()

        # Guitar detail: specs, strings card, big chip
        page.get_by_role("link", name=re.compile("Starling")).first.click()
        expect(page.get_by_role("heading", name="Starling", exact=True)).to_be_visible()
        expect(page.get_by_text("3-tone sunburst")).to_be_visible()
        expect(page.get_by_role("button", name="Log a restring")).to_be_visible()

        # Log a restring through the UI and the chip resets
        page.get_by_role("button", name="Log a restring").click()
        page.locator("#rs-brand").fill("Ernie Ball")
        page.locator("#rs-gauge").fill("10-46")
        page.get_by_role("button", name="Log restring", exact=True).click()
        expect(page.locator(".string-chip.fresh.big").first).to_be_visible()
        expect(page.get_by_text("Ernie Ball 10-46").first).to_be_visible()

        # Sets page shows the seeded set with members
        page.get_by_role("link", name="Sets", exact=True).click()
        expect(page.get_by_text("Practice board")).to_be_visible()
        expect(page.locator(".set-member").first).to_be_visible()

        # Settings: hide the picks section, it leaves the gear list
        page.get_by_role("link", name="Settings", exact=True).click()
        expect(page.get_by_role("heading", name="Features")).to_be_visible()
        page.locator("[data-feature=feature_picks]").click()
        page.wait_for_timeout(400)
        page.get_by_role("link", name="Gear", exact=True).first.click()
        expect(page.get_by_role("heading", name=re.compile("Picks"))).to_have_count(0)
        expect(page.get_by_role("heading", name=re.compile("Guitars"))).to_be_visible()

        # Add gear through the UI
        page.get_by_role("button", name="Add gear").click()
        page.locator("#gf-name").fill("Ui Amp")
        page.locator("#gf-type").select_option("amp")
        page.locator("#gf-make").fill("Acme")
        page.locator("#gf-spec-wattage").fill("15W")
        page.locator("#gear-form").get_by_role("button", name="Add gear", exact=True).click()
        expect(page.get_by_role("heading", name="Ui Amp", exact=True)).to_be_visible()
        expect(page.get_by_text("15W")).to_be_visible()

        browser.close()


def test_mobile_add_restring_from_due(app_url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        sign_in(page, app_url)

        page.get_by_role("link", name="Strings", exact=True).click()
        expect(page.get_by_role("heading", name="Strings", exact=True)).to_be_visible()
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_token_create_shows_value_once_and_revoke(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": width, "height": height})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=app_url)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())
        sign_in(page, app_url)
        page.goto(app_url + "/#/settings")
        expect(page.get_by_role("heading", name="API tokens")).to_be_visible()

        page.locator("#tk-name").fill("my phone")
        page.get_by_role("button", name="Create token").click()
        value = page.locator("#token-value")
        expect(value).to_be_visible()
        expect(value).to_have_text(re.compile(r"^gs_\S{20,}$"))
        token = value.inner_text()
        # the value must survive the list re-render, not flash and vanish
        page.wait_for_timeout(1500)
        expect(value).to_be_visible()
        expect(value).to_have_text(token)
        # the list shows only the prefix, never the full value
        row = page.locator("#token-list .list-row", has_text="my phone")
        expect(row).to_contain_text(token[:8] + "...")
        assert token not in page.locator("#token-list").inner_text()
        # the token actually works
        assert httpx.get(app_url + "/api/v1/gear", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        # copy affordance
        page.get_by_role("button", name="Copy token").click()
        expect(page.get_by_role("button", name="Copied")).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == token

        # revoking a different token keeps the new one on screen
        page.locator("#tk-name").fill("old laptop")
        page.get_by_role("button", name="Create token").click()
        expect(page.locator("#token-value")).not_to_have_text(token)
        token2 = page.locator("#token-value").inner_text()
        page.locator("#token-list .list-row", has_text="my phone").get_by_role("button", name="Revoke").click()
        expect(page.locator("#token-list .list-row", has_text="my phone")).to_have_count(0)
        expect(page.locator("#token-value")).to_have_text(token2)
        assert httpx.get(app_url + "/api/v1/gear", headers={"Authorization": f"Bearer {token}"}).status_code == 401

        # Done hides it, and it is never shown again
        page.get_by_role("button", name="Done").click()
        expect(page.locator("#token-value")).to_have_count(0)
        page.reload()
        expect(page.get_by_role("heading", name="API tokens")).to_be_visible()
        expect(page.locator("#token-value")).to_have_count(0)
        assert token2 not in page.content()
        browser.close()
