"""Screenshot of a restored test site, for the console's timeline card.

Jonas 16:23: "when hovering over a point on the timeline we see a screenshot of the site at that version". The
picture has to be taken right after that backup's restore, because the test site holds only the latest restore.
Runs under its own venv with Playwright (RESTOREGUARD_SHOT_PY): a logged-in desk page needs the API token as a
header, which a bare `chromium --screenshot` cannot send, and the runtime's own venv stays as it is.
The token comes on stdin, never argv (ps and the journal show argv).

  <shot venv>/bin/python erp_shot.py <site url> <desk route> <out.png> < token
"""
import os
import sys

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/restoreguard/ms-playwright")  # where install.sh puts it
from playwright.sync_api import sync_playwright  # noqa: E402

url, route, out = sys.argv[1:4]
token = sys.stdin.read().strip()
with sync_playwright() as p:
    browser = p.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 800}, extra_http_headers={"Authorization": f"token {token}"})
        page.goto(f"{url.rstrip('/')}/{route.lstrip('/')}", wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1000)  # the desk paints its list after the last request settles
        # The desk's realtime socket has no token header and pops "Session Expired" over a page that loaded fine.
        page.add_style_tag(content=".modal, .modal-backdrop {display: none !important} body.modal-open {overflow: auto}")
        page.screenshot(path=out)
    finally:
        browser.close()
