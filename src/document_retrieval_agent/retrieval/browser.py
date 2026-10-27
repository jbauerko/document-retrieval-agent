"""Observe a browser page and act on references chosen by the model."""

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from .documents import safe_filename
from .observations import OBSERVATION_SCRIPT

SITE_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
ALLOWED_KEYS = {
    "Enter",
    "Tab",
    "Shift+Tab",
    "Escape",
    "ArrowDown",
    "ArrowUp",
    "ArrowLeft",
    "ArrowRight",
    "PageDown",
    "PageUp",
    "Home",
    "End",
}


class BrowserActionError(ValueError):
    def __init__(self, message: str, observation: dict | None = None):
        super().__init__(message)
        self.observation = observation


class BrowserSession:
    def __init__(self, *, headless=True, site_url=SITE_URL):
        self.headless = headless
        self.site_url = site_url
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.generation = 0
        self.elements = {}
        self.expecting_download = False
        self.unexpected_downloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    def allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme == "blob":
            parsed = urlparse(parsed.path)
        return parsed.scheme == "https" and parsed.hostname == urlparse(self.site_url).hostname

    async def _start(self):
        if self.page:
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(
            accept_downloads=True, viewport={"width": 1500, "height": 1300}
        )
        await self.context.route("**/*", self._guard_navigation)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("download", self._track_download)

    async def _guard_navigation(self, route):
        request = route.request
        if request.is_navigation_request() and not self.allowed_url(request.url):
            await route.abort()
        else:
            await route.continue_()

    def _track_download(self, download):
        if not self.expecting_download:
            self.unexpected_downloads.append(download)

    async def _settle(self):
        await self.page.wait_for_function(
            "document.body && document.body.innerText.trim().length > 0", timeout=60000
        )
        previous = None
        for _ in range(12):
            await self.page.wait_for_timeout(400)
            text = await self.page.locator("body").inner_text()
            if text and text == previous:
                return
            previous = text

    async def read_text(self) -> str:
        return await self.page.locator("body").inner_text()

    async def observe_page(self) -> dict:
        if not self.page:
            raise ValueError("Call open_site first")
        for download in self.unexpected_downloads:
            await download.cancel()
        self.unexpected_downloads.clear()
        await self._settle()
        self.generation += 1
        observation = await self.page.evaluate(OBSERVATION_SCRIPT, self.generation)
        self.elements = {element["ref"]: element for element in observation["elements"]}
        return {
            "snapshot_id": self.generation,
            "url": self.page.url,
            "title": await self.page.title(),
            **observation,
        }

    async def open_site(self) -> dict:
        await self._start()
        await self.page.goto(self.site_url, wait_until="domcontentloaded", timeout=60000)
        return await self.observe_page()

    def _element(self, ref: str):
        if ref not in self.elements:
            raise ValueError("Unknown or stale element reference; use the latest observation")
        element = self.elements[ref]
        href = element.get("href")
        if href and not self.allowed_url(urljoin(self.page.url, href)):
            raise ValueError("Navigation outside the allowed regulatory website is blocked")
        return element, self.page.locator(f'[data-agent-ref="{ref}"]')

    async def _failure(self, error: Exception):
        try:
            observation = await self.observe_page()
        except PlaywrightError:
            observation = None
        raise BrowserActionError(str(error), observation) from error

    async def click(self, element_ref: str) -> dict:
        try:
            element, locator = self._element(element_ref)
            await locator.click()
            observation = await self.observe_page()
            return {"target": element, "observation": observation}
        except (ValueError, PlaywrightError) as exc:
            await self._failure(exc)

    async def fill(self, element_ref: str, text: str) -> dict:
        try:
            _, locator = self._element(element_ref)
            await locator.fill(text)
            return {"observation": await self.observe_page()}
        except (ValueError, PlaywrightError) as exc:
            await self._failure(exc)

    async def press_key(self, key: str) -> dict:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Supported keys: {', '.join(sorted(ALLOWED_KEYS))}")
        await self.page.keyboard.press(key)
        return {"observation": await self.observe_page()}

    async def scroll(self, direction: str, element_ref: str | None = None) -> dict:
        if direction not in {"up", "down"}:
            raise ValueError("Scroll direction must be up or down")
        try:
            if element_ref:
                _, locator = self._element(element_ref)
                await locator.hover()
            else:
                await self.page.mouse.move(750, 900)
            await self.page.mouse.wheel(0, 650 if direction == "down" else -650)
            return {"observation": await self.observe_page()}
        except (ValueError, PlaywrightError) as exc:
            await self._failure(exc)

    async def download(self, element_ref: str, directory: Path, document_id: str) -> dict:
        path = None
        try:
            _, locator = self._element(element_ref)
            self.expecting_download = True
            async with self.page.expect_download(timeout=60000) as info:
                await locator.click()
            download = await info.value
            path = (
                directory
                / f"{safe_filename(document_id)}_{safe_filename(download.suggested_filename)}"
            )
            await download.save_as(path)
            failure = await download.failure()
            if failure or path.stat().st_size == 0:
                raise ValueError(f"Download failed: {failure or 'empty file'}")
            return {
                "path": str(path.resolve()),
                "filename": path.name,
                "observation": await self.observe_page(),
            }
        except (ValueError, PlaywrightError) as exc:
            if path:
                path.unlink(missing_ok=True)
            await self._failure(exc)
        finally:
            self.expecting_download = False

    async def search_matter(self, matter_number: str) -> dict:
        """Known-site search mechanics; the model chooses the matter and next task."""
        field = self.page.locator(".fm_object_254 .text")
        await field.click()
        await expect(field).to_have_attribute("contenteditable", "true")
        await field.fill(matter_number)
        await field.press("Tab")
        await self.page.locator(".fm_object_258 button").click()
        await expect(self.page.locator(".fm_object_286 .text")).to_have_text(
            matter_number, timeout=30000
        )
        fields = {
            "type": 287,
            "status": 289,
            "title": 290,
            "date_received": 292,
            "decision_date": 294,
            "outcome": 295,
            "category": 298,
        }
        return {
            name: (await self.page.locator(f'[id^="b0p0o{object_id}i"]').inner_text()).strip()
            or None
            for name, object_id in fields.items()
        }

    async def category_documents(self, category: str, count: int) -> list[dict]:
        """Open a category and read at most ten rows in displayed order."""
        await self.page.get_by_role(
            "button", name=re.compile(rf"^{re.escape(category)}\s*-\s*[\d,]+$")
        ).click()
        if count == 0:
            dialog = self.page.get_by_role("dialog")
            await dialog.get_by_role("button", name="OK", exact=True).click()
            await dialog.wait_for(state="hidden")
            return []
        await expect(self.page.get_by_role("grid")).to_have_attribute("aria-rowcount", str(count))
        rows = self.page.locator("tr.v-grid-row-has-data")
        await expect(rows.nth(min(10, count) - 1)).to_be_attached()
        documents = []
        for index in range(min(10, count)):
            row = rows.nth(index)
            documents.append(
                {
                    "id": (
                        await row.locator(".fm-textarea.hand-cursor .text").inner_text()
                    ).strip(),
                    "title": (await row.locator(".fm_object_202 .text").inner_text()).strip(),
                }
            )
        return documents
