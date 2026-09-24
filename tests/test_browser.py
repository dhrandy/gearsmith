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
        expect(page.get_by_role("heading", name="Sets", exact=True)).to_be_visible()
        expect(page.locator(".set-card", has_text="Practice board")).to_be_visible()
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

        page.get_by_role("link", name="Restrings", exact=True).click()
        expect(page.get_by_role("heading", name="Restrings", exact=True)).to_be_visible()
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


# Checks every element that carries user text: it must stay inside its card/row and the page
# must never scroll sideways. Returns a list of offenders so a failure says what broke.
OVERFLOW_JS = """() => {
  const out = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1) out.push(`page scrolls sideways: ${document.documentElement.scrollWidth} > ${vw}`);
  const boxes = '.gear-card, .set-card, .set-member, .set-chip, .due, .list-row, .restring-row, .fact, .card';
  document.querySelectorAll('#view ' + boxes.split(', ').join(', #view ')).forEach((box) => {
    const b = box.getBoundingClientRect();
    if (b.right > vw + 1) out.push(`${box.className} runs past the viewport (${Math.round(b.right)} > ${vw})`);
    box.querySelectorAll('*').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (!r.width || el.closest(boxes) !== box) return;
      if (r.right > b.right + 1) out.push(`${el.className || el.tagName} runs out of ${box.className}: "${el.textContent.trim().slice(0, 40)}"`);
    });
  });
  return out;
}"""

LONG_WORD = "Supercalifragilisticexpialidociousoverdriveunit"


def assert_no_overflow(page, where):
    page.wait_for_timeout(300)
    problems = page.evaluate(OVERFLOW_JS)
    assert problems == [], f"{where}: {problems}"


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_long_text_truncates_or_wraps_inside_cards(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)
        req = page.request
        amp = req.post(app_url + "/api/gear", data={
            "type": "amp", "name": "Bassbreaker " + LONG_WORD,
            "make": "Fender Musical Instruments Corporation", "model": "Bassbreaker 15 Combo Limited Edition Tweed",
            "notes": LONG_WORD * 3,
        }).json()
        gtr = req.post(app_url + "/api/gear", data={
            "type": "guitar", "name": "Overdue " + LONG_WORD, "make": "Positive Grid",
            "model": "HT Studio 20 (Venue Edition) Extra Long Model", "restring_interval_days": 30,
        }).json()
        old = page.evaluate("() => new Date(Date.now() - 60 * 864e5).toISOString().slice(0, 10)")
        assert req.post(app_url + f"/api/gear/{gtr['id']}/restrings", data={
            "brand": "Ernie Ball " + LONG_WORD, "gauge": "10-46", "date": old, "note": "note " + LONG_WORD,
        }).ok
        assert req.post(app_url + "/api/sets", data={
            "name": "Gig rig " + LONG_WORD, "notes": "Notes " + LONG_WORD, "item_ids": [amp["id"], gtr["id"]],
        }).ok
        assert req.post(app_url + "/api/tokens", data={"name": "token-" + LONG_WORD}).ok

        # Gear list: make/model subtitle is clipped with an ellipsis, full text kept in the tooltip
        page.goto(app_url + "/#/")
        meta = page.locator(".gear-card .gc-meta", has_text="Fender Musical")
        expect(meta).to_be_visible()
        expect(meta).to_have_attribute("title", re.compile("Bassbreaker 15 Combo Limited Edition Tweed"))
        clipped = meta.evaluate("el => ({ css: getComputedStyle(el).textOverflow, over: el.scrollWidth > el.clientWidth })")
        assert clipped == {"css": "ellipsis", "over": True}
        assert_no_overflow(page, "gear list")

        for route in [f"#/gear/{amp['id']}", f"#/gear/{gtr['id']}", "#/sets", "#/due", "#/settings"]:
            page.goto(app_url + "/" + route)
            page.locator("#view h1").first.wait_for()
            assert_no_overflow(page, route)

        browser.close()


def section_names(page, heading):
    grid = page.locator("h2.section-title", has_text=heading).locator("xpath=following-sibling::div[1]")
    return grid.locator(".gc-name").all_inner_texts()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_star_toggle_floats_to_top_and_favorites_filter(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("dialog", lambda d: d.accept())
        sign_in(page, app_url)
        assert section_names(page, "Guitars") == ["Heron", "Starling"]

        # one tap on the card star, no page reload: Starling moves to the top of Guitars
        page.evaluate("window.__noReload = true")
        star = page.get_by_role("button", name="Add Starling to favorites")
        expect(star).to_have_attribute("aria-pressed", "false")
        star.click()
        filled = page.get_by_role("button", name="Remove Starling from favorites")
        expect(filled).to_have_attribute("aria-pressed", "true")
        expect(filled).to_have_text("★")
        assert section_names(page, "Guitars") == ["Starling", "Heron"]
        assert page.evaluate("window.__noReload") is True
        box = page.locator(".gear-cell", has_text="Starling")
        assert_no_overflow(page, "gear list with a star")
        # the star sits inside its card and doesn't cover the name
        cb, sb, nb = (box.locator(s).bounding_box() for s in (".gear-card", ".star", ".gc-name"))
        assert sb["x"] + sb["width"] <= cb["x"] + cb["width"] + 1 and nb["x"] + nb["width"] <= sb["x"] + 1

        # it sticks after a reload
        page.reload()
        expect(page.get_by_role("button", name="Remove Starling from favorites")).to_be_visible()
        assert section_names(page, "Guitars") == ["Starling", "Heron"]

        # favorites only: just the starred gear, remembered across visits
        page.get_by_role("button", name="Favorites only").click()
        expect(page.get_by_role("button", name="Favorites only")).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".gear-card")).to_have_count(1)
        expect(page.get_by_role("heading", name=re.compile("Amps"))).to_have_count(0)
        page.reload()
        expect(page.locator(".gear-card")).to_have_count(1)

        # detail page toggle: unstar there, the filtered list comes up empty
        page.get_by_role("link", name=re.compile("Starling")).click()
        detail = page.locator("#gd-fav-wrap button")
        expect(detail).to_have_attribute("aria-pressed", "true")
        page.evaluate("window.__noReload = true")
        detail.click()
        expect(page.locator("#gd-fav-wrap button")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#gd-fav-wrap button")).to_contain_text("☆")
        assert page.evaluate("window.__noReload") is True
        page.get_by_role("link", name="Gear", exact=True).first.click()
        expect(page.get_by_text("No favorites yet")).to_be_visible()
        page.get_by_role("button", name="Favorites only").click()
        expect(page.get_by_role("button", name="Add Starling to favorites")).to_be_visible()
        assert section_names(page, "Guitars") == ["Heron", "Starling"]
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_song_editor_prefills_knobs_and_recall_sheet(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("dialog", lambda d: d.accept())
        sign_in(page, app_url)
        req = page.request
        gear = {g["name"]: g for g in req.get(app_url + "/api/gear").json()}
        drive = gear["Demo Drive"]["id"]

        # controls live on the gear page
        page.goto(app_url + f"/#/gear/{drive}")
        page.get_by_role("button", name="Edit controls").click()
        page.locator("[data-ct-name='0']").fill("Gain")
        for name in ("Tone", "Level"):
            page.get_by_role("button", name="Add a control").click()
            page.locator("[data-ct-name]").last.fill(name)
        page.get_by_role("button", name="Save controls").click()
        expect(page.locator("#gd-controls .set-chip")).to_have_count(3)
        page.locator("#gd-modeler").check()
        page.wait_for_timeout(300)
        assert req.get(app_url + f"/api/gear/{drive}").json()["modeler"] is True

        # new song: pick the drive, knob names come prefilled, type values only
        page.get_by_role("link", name="Songs", exact=True).click()
        page.get_by_role("link", name="Add song").click()
        page.locator("#sg-title").fill("Test Song")
        page.locator("#sg-artist").fill("Test Band")
        page.locator("#sg-tuning").fill("Drop D")
        page.locator("#sg-capo").fill("2")
        page.locator("#sg-guitar").select_option(label="Starling")
        expect(page.locator(".rig-row")).to_have_count(1)
        page.locator("#rig-pick").select_option(label="Demo Drive")
        names = page.locator("[data-r-kname^='1:']")
        expect(names).to_have_count(3)
        assert [names.nth(i).input_value() for i in range(3)] == ["Gain", "Tone", "Level"]
        page.locator("[data-r-kval='1:0']").fill("2:00")
        page.locator("[data-r-kval='1:1']").fill("noon")
        page.locator("[data-r-kval='1:2']").fill("max")
        page.locator("[data-rig-eng='1:toggle']").click()
        page.locator("[data-rig-up='1']").click()  # drive first, then the guitar
        page.locator("#patch-pick").select_option(label="Demo Drive")
        page.locator("#p-ref-0").fill("12B")
        page.locator("#p-scenes-0").fill("verse, solo")
        assert_no_overflow(page, "song editor")
        page.get_by_role("button", name="Add song").click()

        # recall sheet: chain in order with the settings
        expect(page.get_by_role("heading", name="Test Song")).to_be_visible()
        chain = page.locator(".chain-item")
        expect(chain).to_have_count(2)
        expect(chain.nth(0)).to_contain_text("Demo Drive")
        expect(chain.nth(0)).to_contain_text("2:00")
        expect(chain.nth(0)).to_contain_text("noon")
        expect(chain.nth(0).locator(".engaged")).to_have_text("Toggle")
        expect(chain.nth(1)).to_contain_text("Starling")
        expect(page.locator(".patch-ref")).to_have_text("12B")
        expect(page.locator(".patch-card .chip")).to_have_count(2)
        expect(page.locator(".chips").first).to_contain_text("Capo 2")
        assert_no_overflow(page, "song recall sheet")

        # edit round trip keeps everything, list shows the song
        page.get_by_role("link", name="Edit", exact=True).click()
        expect(page.locator("[data-r-kval='0:0']")).to_have_value("2:00")
        page.locator("#sg-bpm").fill("96")
        page.get_by_role("button", name="Save song").click()
        expect(page.locator(".chips").first).to_contain_text("96 bpm")
        page.get_by_role("link", name="Songs", exact=True).click()
        expect(page.locator(".song-card", has_text="Test Song")).to_be_visible()
        assert_no_overflow(page, "songs list")

        # the gear page lists the song
        page.goto(app_url + f"/#/gear/{drive}")
        expect(page.locator("#gd-songs")).to_contain_text("Test Song")
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_want_and_sold_views(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)
        # add a wanted guitar from the Want view
        page.locator(".seg").get_by_role("link", name="Want").click()
        page.get_by_role("button", name="Add to want list").click()
        expect(page.locator("#gf-life")).to_have_value("want")
        page.locator("#gf-name").fill("Dream Guitar")
        page.locator("#gf-wprice").fill("1500")
        page.get_by_role("button", name="Add gear", exact=True).click()
        expect(page.locator(".life-banner.want")).to_contain_text("$1,500")
        page.goto(app_url + "/#/want")
        expect(page.locator(".gear-card", has_text="Dream Guitar")).to_contain_text("Want · $1,500")
        page.goto(app_url + "/#/")
        expect(page.locator(".gear-card", has_text="Dream Guitar")).to_have_count(0)

        # sell Starling through the edit form: it leaves the main page and the strings list
        page.get_by_role("link", name=re.compile("Starling")).first.click()
        page.get_by_role("button", name="Edit", exact=True).click()
        page.locator("#gf-life").select_option("sold")
        expect(page.locator("#gf-sprice")).to_be_visible()
        expect(page.locator("#gf-wprice")).to_be_hidden()
        page.locator("#gf-sdate").fill("2026-01-15")
        page.locator("#gf-sprice").fill("650")
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator(".life-banner.sold")).to_contain_text("$650")
        page.goto(app_url + "/#/")
        expect(page.locator(".gear-card", has_text="Starling")).to_have_count(0)
        page.goto(app_url + "/#/due")
        expect(page.locator(".due", has_text="Starling")).to_have_count(0)
        page.goto(app_url + "/#/sold")
        cell = page.locator(".gear-cell.sold", has_text="Starling")
        expect(cell).to_contain_text("$650")
        expect(cell.locator(".star")).to_have_count(0)
        assert float(cell.locator(".gear-card").evaluate("el => getComputedStyle(el).opacity")) < 1
        assert_no_overflow(page, "sold view")

        # hiding the sold archive removes its tab
        req = page.request
        req.put(app_url + "/api/settings", data={"feature_sold": False})
        page.goto(app_url + "/#/")
        page.reload()
        expect(page.locator(".seg").get_by_role("link", name="Sold")).to_have_count(0)
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_share_link_create_view_and_turn_off(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("dialog", lambda d: d.accept())
        sign_in(page, app_url)
        page.get_by_role("link", name=re.compile("Starling")).first.click()
        box = page.locator("#gd-share")
        box.locator("[data-share-expiry-new]").select_option("30")
        box.get_by_role("button", name="Create link").click()
        url = box.locator("[data-share-url]").inner_text()
        assert re.search(r"/share/[A-Za-z0-9_-]{32}$", url)
        expect(box).to_contain_text("Expires")
        assert_no_overflow(page, "gear page with share link")

        viewer = browser.new_context(viewport={"width": width, "height": height})
        guest = viewer.new_page()
        resp = guest.goto(url)
        assert "noindex" in resp.headers["x-robots-tag"]
        expect(guest.get_by_role("heading", name="Starling")).to_be_visible()
        expect(guest.get_by_text("3-tone sunburst")).to_be_visible()
        assert guest.locator("a, button, form, input").count() == 0
        assert guest.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        box.get_by_role("button", name="Turn off").click()
        expect(box.get_by_role("button", name="Create link")).to_be_visible()
        assert guest.goto(url).status == 404

        # sets share the same way
        page.get_by_role("link", name="Sets", exact=True).click()
        page.locator(".set-card", has_text="Practice board").get_by_role("button", name="Share").click()
        page.locator("#share-box").get_by_role("button", name="Create link").click()
        set_url = page.locator("#share-box [data-share-url]").inner_text()
        guest.goto(set_url)
        expect(guest.get_by_role("heading", name="Practice board")).to_be_visible()
        expect(guest.locator(".share-item")).to_have_count(4)
        assert guest.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        viewer.close()
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_presets_follow_songs_and_artist_view(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)
        req = page.request
        req.post(app_url + "/api/songs", data={"title": "Other Song", "artist": "Second Band"})

        # build a preset in the UI
        page.get_by_role("link", name="Songs", exact=True).click()
        page.get_by_role("link", name="Presets", exact=True).click()
        expect(page.get_by_role("heading", name="Presets")).to_be_visible()
        page.get_by_role("link", name="Add preset").click()
        page.locator("#sg-name").fill("Test Crunch")
        page.locator("#sg-artist").fill("Test Band")
        page.locator("#sg-amp").select_option(label="Club 20")
        page.locator("#rig-pick").select_option(label="Demo Drive")
        page.locator("[data-r-kname='1:0']").fill("Gain")
        page.locator("[data-r-kval='1:0']").fill("2:00")
        assert_no_overflow(page, "preset editor")
        page.get_by_role("button", name="Add preset").click()
        expect(page.get_by_role("heading", name="Test Crunch")).to_be_visible()
        expect(page.locator(".chain-item")).to_have_count(2)
        preset_id = int(page.url.rstrip("/").split("/")[-1])

        # a song that uses it, with a label
        page.get_by_role("link", name="Songs", exact=True).click()
        page.get_by_role("link", name="Add song").click()
        page.locator("#sg-title").fill("Preset Song")
        page.locator("#sg-artist").fill("test band")
        page.locator("#preset-pick").select_option(label="Test Crunch (Test Band)")
        page.locator("#sp-label-0").fill("Verse")
        assert_no_overflow(page, "song editor with preset")
        page.get_by_role("button", name="Add song").click()
        expect(page.get_by_role("heading", name="Preset Song")).to_be_visible()
        use = page.locator(".preset-use")
        expect(use).to_contain_text("Verse")
        expect(use).to_contain_text("Test Crunch")
        expect(use.locator(".chain-item", has_text="Demo Drive")).to_contain_text("2:00")
        assert_no_overflow(page, "song with preset")
        song_url = page.url

        # edit the preset once, the song follows
        use.get_by_role("link", name="Test Crunch").click()
        expect(page.locator("#pd-songs")).to_contain_text("Preset Song")
        page.get_by_role("link", name="Edit", exact=True).click()
        page.locator("[data-r-kval='1:0']").fill("max")
        page.get_by_role("button", name="Save preset").click()
        expect(page.get_by_role("heading", name="Test Crunch")).to_be_visible()
        page.goto(song_url)
        expect(page.locator(".preset-use .chain-item", has_text="Demo Drive")).to_contain_text("max")

        # artists view groups case-insensitively, songs and presets together
        page.get_by_role("link", name="Songs", exact=True).click()
        page.get_by_role("link", name="Artists", exact=True).click()
        groups = page.locator(".artist-group")
        expect(groups).to_have_count(2)
        band = page.locator(".artist-group", has_text="Test Band")
        expect(band.locator(".song-card", has_text="Preset Song")).to_be_visible()
        expect(band.locator(".preset-card", has_text="Test Crunch")).to_be_visible()
        expect(band.locator("h2")).to_contain_text("1 song · 1 preset")
        assert_no_overflow(page, "artists view")
        page.locator("#artist-search").fill("second")
        expect(groups).to_have_count(1)

        # save a song's own chain as a preset, switching the song over
        other = [s for s in req.get(app_url + "/api/songs").json() if s["title"] == "Other Song"][0]
        req.patch(app_url + f"/api/songs/{other['id']}", data={"rig": [{"gear_name": "Loose Pedal", "knobs": [{"name": "Level", "value": "noon"}]}]})
        page.goto(app_url + f"/#/songs/{other['id']}")
        page.get_by_role("button", name="Save as preset").click()
        page.locator("#sp-name").fill("Loose tone")
        page.get_by_role("button", name="Save preset").click()
        expect(page.locator(".preset-use")).to_contain_text("Loose tone")
        assert req.get(app_url + f"/api/songs/{other['id']}").json()["rig"] == []

        # copy back into the song
        page.get_by_role("button", name="Copy into song").click()
        page.locator("#cp-confirm").click()
        expect(page.locator(".preset-use")).to_have_count(0)
        expect(page.locator(".chain-item")).to_contain_text("noon")
        assert req.get(app_url + f"/api/presets/{preset_id}").json()["song_count"] == 1

        # presets list
        page.goto(app_url + "/#/presets")
        expect(page.locator(".preset-card")).to_have_count(2)
        assert_no_overflow(page, "presets list")
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_strings_type_and_guitar_picks_strings(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)

        # the demo strings show in their own section with gauge and type badges
        expect(page.get_by_role("heading", name=re.compile("^Strings"))).to_be_visible()
        card = page.locator(".gear-card", has_text="Demo Strings 10-46")
        expect(card.get_by_text("Electric")).to_be_visible()

        # add a strings item through the form
        page.get_by_role("button", name="Add gear").click()
        page.locator("#gf-type").select_option("strings")
        expect(page.locator("#gf-make-label")).to_have_text("Brand")
        expect(page.locator("#gf-strings-wrap")).to_be_hidden()
        page.locator("#gf-name").fill("Ui Strings 9-42")
        page.locator("#gf-make").fill("Ui Brand")
        page.locator("#gf-spec-gauge").fill("9-42")
        page.locator("#gf-spec-string_type").select_option("electric")
        page.locator("#gf-spec-sets_per_pack").fill("2")
        page.locator("#gear-form").get_by_role("button", name="Add gear", exact=True).click()
        expect(page.get_by_role("heading", name="Ui Strings 9-42", exact=True)).to_be_visible()
        expect(page.get_by_text("No guitar uses these yet")).to_be_visible()
        strings_url = page.url

        # search matches the gauge, the string-type filter narrows the strings section
        page.goto(app_url + "/#/")
        page.locator("#gear-search").fill("9-42")
        expect(page.locator(".gear-card")).to_have_count(1)
        page.locator("#gear-search").fill("")
        page.locator("#string-type-filter").select_option("bass")
        expect(page.locator(".gear-card", has_text="Ui Strings 9-42")).to_have_count(0)
        expect(page.locator(".gear-card", has_text="Starling")).to_have_count(1)
        page.locator("#string-type-filter").select_option("")
        page.screenshot(path=f"/tmp/strings-list-{width}.png", full_page=True)

        # pick the strings on a guitar
        page.locator(".gear-card", has_text="Heron").click()
        expect(page.get_by_role("heading", name="Heron", exact=True)).to_be_visible()
        page.get_by_role("button", name="Edit", exact=True).click()
        expect(page.locator("#gf-strings option", has_text="Ui Strings 9-42")).to_have_count(1)
        page.locator("#gf-strings").select_option(label="Ui Strings 9-42")
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator("#gd-strings-used").get_by_role("link", name="Ui Strings 9-42")).to_be_visible()

        # log a restring from your strings: the picker is preset and fills brand and gauge
        page.get_by_role("button", name="Log a restring").click()
        expect(page.locator("#rs-gauge")).to_have_value("9-42")
        expect(page.locator("#rs-brand")).to_have_value("Ui Brand")
        page.locator("#rs-strings").select_option(label="Demo Strings 10-46")
        expect(page.locator("#rs-gauge")).to_have_value("10-46")
        page.get_by_role("button", name="Log restring").click()
        expect(page.locator("#restring-history").get_by_role("link", name="Demo Strings 10-46")).to_be_visible()
        expect(page.locator("#gd-strings-used").get_by_role("link", name="Demo Strings 10-46")).to_be_visible()
        page.screenshot(path=f"/tmp/strings-guitar-{width}.png", full_page=True)

        # the strings page lists the guitar that uses them
        page.goto(app_url + "/#/")
        page.locator(".gear-card", has_text="Demo Strings 10-46").click()
        expect(page.locator("#gd-used-on").get_by_role("link", name="Heron")).to_be_visible()
        expect(page.locator("#gd-used-on").get_by_role("link", name="Starling")).to_be_visible()
        page.screenshot(path=f"/tmp/strings-detail-{width}.png", full_page=True)
        assert strings_url

        # no sideways scrolling on a phone
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 1
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_set_chips_link_to_set_pages(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)
        req = page.request
        amp = req.post(app_url + "/api/gear", data={"type": "amp", "name": "Chip Amp"}).json()
        pedal = req.post(app_url + "/api/gear", data={"type": "pedal", "name": "Chip Drive"}).json()
        compact = req.post(app_url + "/api/sets", data={
            "name": "Compact Setup", "notes": "Small bag rig", "item_ids": [amp["id"], pedal["id"]],
        }).json()
        long_set = req.post(app_url + "/api/sets", data={
            "name": "Long " + LONG_WORD, "notes": "Notes " + LONG_WORD, "item_ids": [amp["id"]],
        }).json()
        song = req.post(app_url + "/api/songs", data={"title": "Chip Song", "set_id": compact["id"]}).json()

        # gear page: every set chip is a link to its set page
        page.goto(app_url + f"/#/gear/{amp['id']}")
        chips = page.locator("#gd-sets a.set-chip")
        expect(chips).to_have_count(2)
        assert_no_overflow(page, "gear page with set chips")
        chips.filter(has_text="Compact Setup").click()
        expect(page).to_have_url(re.compile(f"#/sets/{compact['id']}$"))
        expect(page.get_by_role("heading", name="Compact Setup", exact=True)).to_be_visible()
        expect(page.get_by_text("Small bag rig")).to_be_visible()
        expect(page.locator(".tab.active")).to_have_attribute("data-tab", "sets")
        expect(page.locator(".set-member")).to_have_count(2)
        assert_no_overflow(page, "set page")

        # set page members lead back to gear
        page.locator(".set-member", has_text="Chip Drive").click()
        expect(page.get_by_role("heading", name="Chip Drive", exact=True)).to_be_visible()

        # the "in ..." line under the gear name links too
        page.goto(app_url + f"/#/gear/{amp['id']}")
        page.locator("#view p.muted a", has_text="Compact Setup").click()
        expect(page).to_have_url(re.compile(f"#/sets/{compact['id']}$"))

        # song page: the Set fact links to the set
        page.goto(app_url + f"/#/songs/{song['id']}")
        page.locator(".fact a", has_text="Compact Setup").click()
        expect(page.get_by_role("heading", name="Compact Setup", exact=True)).to_be_visible()

        # sets list: the set name opens its page
        page.goto(app_url + "/#/sets")
        page.locator(".set-card a.set-name", has_text="Compact Setup").click()
        expect(page).to_have_url(re.compile(f"#/sets/{compact['id']}$"))

        # editing from the set page stays on the set page
        page.get_by_role("button", name="Edit").click()
        page.locator("#sf-name").fill("Compact Setup 2")
        page.get_by_role("button", name="Save changes").click()
        expect(page.get_by_role("heading", name="Compact Setup 2", exact=True)).to_be_visible()
        expect(page).to_have_url(re.compile(f"#/sets/{compact['id']}$"))

        # long names stay inside the page on the set page
        page.goto(app_url + f"/#/sets/{long_set['id']}")
        page.locator("#view h1").first.wait_for()
        assert_no_overflow(page, "set page with long name")

        # deleting from the set page goes back to the list; the old link says it is gone
        page.goto(app_url + f"/#/sets/{compact['id']}")
        page.get_by_role("button", name="Delete").click()
        page.locator("#del-confirm").click()
        expect(page).to_have_url(re.compile("#/sets$"))
        expect(page.locator(".set-card", has_text="Compact Setup 2")).to_have_count(0)
        page.goto(app_url + f"/#/sets/{compact['id']}")
        expect(page.get_by_role("heading", name="Set not found")).to_be_visible()

        # the song keeps its copy of the set name as plain text once the set is gone
        page.goto(app_url + f"/#/songs/{song['id']}")
        expect(page.locator(".fact", has_text="Compact Setup")).to_be_visible()
        expect(page.locator(".fact a", has_text="Compact Setup")).to_have_count(0)

        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_preset_cards_show_rig_tag_and_chain(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)
        req = page.request
        gear = {g["name"]: g for g in req.get(app_url + "/api/gear").json()}
        assert req.post(app_url + "/api/presets", data={
            "name": "Dexter and The Moonrocks Extended Long Title (Ampero Mini)", "artist": "Dexter and The Moonrocks",
            "amp_id": gear["Club 20"]["id"],
            "patches": [{"gear_name": "Ampero Mini", "patch_name": "Dexter", "blocks": [
                {"block_type": "Drive", "model": "Big Pie"}, {"block_type": "Amp", "model": "Marshell 800"},
                {"block_type": "Delay", "model": "Recaller"}, {"block_type": "Reverb", "model": "Hall"}]}],
        }).ok
        assert req.post(app_url + "/api/presets", data={
            "name": "No Rig Suffix", "artist": "Dexter and The Moonrocks",
            "rig": [{"gear_id": gear["Demo Drive"]["id"]}],
        }).ok

        page.goto(app_url + "/#/songs/artists")
        card = page.locator(".preset-card", has_text="Ampero Mini")
        expect(card.locator(".gc-name")).to_have_text("Dexter and The Moonrocks Extended Long Title")
        expect(card.locator(".rig-tag")).to_have_text("Ampero Mini")
        expect(card.locator(".chain-line")).to_have_text("Big Pie › Marshell 800 › Recaller › Hall › Club 20")
        # the title wraps to at most two lines instead of cutting off on one
        lines = card.locator(".gc-name").evaluate(
            "el => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))")
        assert 1 <= lines <= 2
        plain = page.locator(".preset-card", has_text="No Rig Suffix")
        expect(plain.locator(".rig-tag")).to_have_count(0)
        expect(plain.locator(".chain-line")).to_have_text("Demo Drive")
        assert_no_overflow(page, "artists view preset cards")

        page.goto(app_url + "/#/presets")
        expect(page.locator(".preset-card .rig-tag", has_text="Ampero Mini")).to_be_visible()
        assert_no_overflow(page, "presets list")
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_stock_countdown_manual_link_and_maintenance(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)

        # the strings card shows the stock badge
        card = page.locator(".gear-card", has_text="Demo Strings 10-46")
        expect(card.get_by_text("3 sets on hand")).to_be_visible()

        # the strings page adjusts the count by hand
        card.click()
        expect(page.get_by_role("heading", name="Stock", exact=True)).to_be_visible()
        page.get_by_role("button", name="One fewer set").click()
        expect(page.locator("#gd-stock").get_by_text("2 sets on hand")).to_be_visible()
        page.once("dialog", lambda d: d.accept("1"))
        page.get_by_role("button", name="Set count").click()
        expect(page.locator("#gd-stock").get_by_text("1 set left")).to_be_visible()

        # a restring with these strings uses the last set: the page flags it out of stock
        page.goto(app_url + "/#/")
        page.locator(".gear-card", has_text="Heron").click()
        page.get_by_role("button", name="Log a restring").click()
        page.locator("#rs-strings").select_option(label="Demo Strings 10-46")
        page.get_by_role("button", name="Log restring").click()
        expect(page.locator("#gd-strings-used").get_by_role("link", name="Demo Strings 10-46")).to_be_visible()
        page.locator("#gd-strings-used").get_by_role("link", name="Demo Strings 10-46").click()
        expect(page.locator("#gd-stock").get_by_text("Out of sets")).to_be_visible()
        page.screenshot(path=f"/tmp/stock-detail-{width}.png", full_page=True)

        # maintenance: log, edit and delete an entry on an amp
        page.goto(app_url + "/#/")
        page.locator(".gear-card", has_text="Club 20").click()
        expect(page.get_by_role("heading", name="Maintenance", exact=True)).to_be_visible()
        page.get_by_role("button", name="Log maintenance").click()
        page.locator("#mt-cat").select_option("tubes")
        page.locator("#mt-note").fill("New EL84 pair, biased")
        page.locator("#mt-form").get_by_role("button", name="Log maintenance").click()
        row = page.locator(".maint-row", has_text="New EL84 pair, biased")
        expect(row.get_by_text("Tubes", exact=True)).to_be_visible()
        row.get_by_role("button", name="Edit").click()
        page.locator("#mt-note").fill("New EL84 pair, biased at 35mA")
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator(".maint-row", has_text="biased at 35mA")).to_be_visible()
        page.screenshot(path=f"/tmp/maintenance-{width}.png", full_page=True)
        page.locator(".maint-row").get_by_role("button", name="Delete").click()
        expect(page.get_by_text("Nothing logged yet.")).to_be_visible()

        # manual link: set it in the form, it becomes a button on the page
        page.get_by_role("button", name="Edit", exact=True).click()
        page.locator("#gf-manual").fill("https://example.com/club20-manual.pdf")
        page.get_by_role("button", name="Save changes").click()
        manual = page.get_by_role("link", name=re.compile("Manual"))
        expect(manual).to_be_visible()
        assert manual.get_attribute("href") == "https://example.com/club20-manual.pdf"
        page.screenshot(path=f"/tmp/manual-link-{width}.png", full_page=True)

        # no sideways scrolling on a phone
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 1
        browser.close()


@pytest.mark.parametrize("width,height", [(1920, 1080), (390, 844)])
def test_share_qr_backup_and_tuner(app_url, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        page = browser.new_page(viewport={"width": width, "height": height})
        sign_in(page, app_url)

        # share QR: make a link, show its QR code
        page.locator(".gear-card", has_text="Starling").click()
        page.locator("#gd-share").get_by_role("button", name="Create link").click()
        page.get_by_role("button", name="QR code").click()
        expect(page.locator(".qr-box svg")).to_be_visible()
        page.screenshot(path=f"/tmp/share-qr-{width}.png", full_page=True)

        # settings: backup download and the tuner toggle
        page.goto(app_url + "/#/settings")
        backup = page.get_by_role("link", name="Download backup (JSON)")
        expect(backup).to_be_visible()
        assert backup.get_attribute("href") == "/api/export"

        # tuner: its tab opens the page and the fake mic starts the readout
        page.get_by_role("link", name="Tuner", exact=True).click()
        expect(page.get_by_role("heading", name="Tuner", exact=True)).to_be_visible()
        page.get_by_role("button", name="Start tuning").click()
        expect(page.get_by_role("button", name="Stop")).to_be_visible()
        expect(page.locator("#tuner-error")).to_have_text("")
        page.screenshot(path=f"/tmp/tuner-{width}.png", full_page=True)

        # hiding the feature drops the tab and closes the route
        page.goto(app_url + "/#/settings")
        page.locator("[data-feature=feature_tuner]").uncheck()
        expect(page.get_by_role("link", name="Tuner", exact=True)).to_be_hidden()
        page.goto(app_url + "/#/tuner")
        expect(page.get_by_role("heading", name="Gear", exact=True)).to_be_visible()
        page.goto(app_url + "/#/settings")
        page.locator("[data-feature=feature_tuner]").check()
        expect(page.get_by_role("link", name="Tuner", exact=True)).to_be_visible()

        # no sideways scrolling on a phone
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 1
        browser.close()
