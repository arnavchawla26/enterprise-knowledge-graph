"""Import kibana/trust_dashboard.ndjson into Kibana and screenshot the dashboard headlessly.

    uv run python scripts/screenshot_kibana.py [--out reports/trust_dashboard.png]

Uses the saved-objects import API (overwrite) and Playwright's bundled Chromium
(`uv run playwright install chromium` once). No browser pane, no manual step.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ekg.config import settings  # noqa: E402


def wait_for_kibana(url: str, timeout: int = 240) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"{url}/api/status", timeout=5)
            if r.status_code == 200 and r.json().get("status", {}).get("overall", {}).get("level") == "available":
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit("kibana not available")


def import_objects(url: str, path: Path) -> dict:
    with open(path, "rb") as f:
        r = httpx.post(f"{url}/api/saved_objects/_import", params={"overwrite": "true"}, headers={"kbn-xsrf": "true"},
                       files={"file": (path.name, f, "application/ndjson")}, timeout=60)
    r.raise_for_status()
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "trust_dashboard.png")
    ap.add_argument("--ndjson", type=Path, default=ROOT / "kibana" / "trust_dashboard.ndjson")
    a = ap.parse_args(argv)
    url = settings().kibana_url.rstrip("/")
    wait_for_kibana(url)
    res = import_objects(url, a.ndjson)
    print(f"import: success={res.get('success')} count={res.get('successCount')} errors={res.get('errors', [])}")
    if not res.get("success"):
        return 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1250}, device_scale_factor=1)
        page.goto(f"{url}/app/dashboards#/view/ekg-trust-dashboard?_g=(time:(from:now-30d,to:now))", wait_until="networkidle", timeout=120_000)
        page.wait_for_selector("[data-test-subj='dashboardPanel']", timeout=60_000)
        for _ in range(40):
            if page.locator("[data-render-complete='true']").count() >= 6 and page.locator("[data-loading='true']").count() == 0:
                break
            time.sleep(1)
        time.sleep(3)
        dismiss = page.get_by_role("button", name="Dismiss")   # the "data is not secure" toast on a security-less dev stack
        if dismiss.count():
            dismiss.first.click()
            time.sleep(1)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(a.out), full_page=True)
        browser.close()
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
