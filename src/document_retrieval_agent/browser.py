"""FileMaker WebDirect adapter. All site-specific selectors live here."""

import re
from pathlib import Path

from playwright.async_api import async_playwright, expect

from .tools import CATEGORIES, safe_filename

SITE_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"


def parse_counts(text: str) -> dict[str, int]:
    counts = {}
    for category in CATEGORIES:
        match = re.search(rf"(?m)^\s*{re.escape(category)}\s*-\s*([\d,]+)\s*$", text)
        if not match:
            raise ValueError(f"Could not read the live count for {category}")
        counts[category] = int(match[1].replace(",", ""))
    return counts


class UARBBrowser:
    def __init__(self, *, headless=True):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.matter = None
        self.document_type = None
        self.documents = {}

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

    async def _start(self):
        if self.page:
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        # At least ten document rows must fit; FileMaker virtualizes rows outside the viewport.
        self.context = await self.browser.new_context(
            accept_downloads=True,
            viewport={"width": 1500, "height": 1300},
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)

    async def search_matter(self, matter_number: str):
        if not re.fullmatch(r"M[0-9]{5}", matter_number):
            raise ValueError("Invalid matter number")
        await self._start()
        await self.page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
        field = self.page.locator(".fm_object_254 .text")
        await field.click()
        # Clicking activates the editor asynchronously; typing before this loses the value.
        await expect(field).to_have_attribute("contenteditable", "true")
        await field.fill(matter_number)
        await field.press("Tab")
        await self.page.locator(".fm_object_258 button").click()
        try:
            await expect(self.page.locator(".fm_object_286 .text")).to_have_text(matter_number)
        except Exception as exc:
            text = (await self.page.locator("body").inner_text())[-1500:]
            raise ValueError(
                f"Could not open requested matter {matter_number}. Site says: {text}"
            ) from exc

        fields = {
            "type": 287,
            "status": 289,
            "title": 290,
            "date_received": 292,
            "decision_date": 294,
            "outcome": 295,
            "category": 298,
        }
        metadata = {}
        for name, object_id in fields.items():
            value = (await self.page.locator(f'[id^="b0p0o{object_id}i"]').inner_text()).strip()
            metadata[name] = value or None
        counts = parse_counts(await self.page.locator("body").inner_text())
        self.matter = {
            "matter_number": matter_number,
            "metadata": metadata,
            "counts": counts,
            "source": SITE_URL,
        }
        self.document_type = None
        self.documents = {}
        return self.matter

    async def list_documents(self, document_type: str):
        if not self.matter or document_type not in CATEGORIES:
            raise ValueError("Search a matter first, then select a supported document category")
        if self.document_type == document_type:
            return list(self.documents.values())
        count = self.matter["counts"][document_type]
        await self.page.get_by_role(
            "button", name=re.compile(rf"^{re.escape(document_type)}\s*-\s*[\d,]+$")
        ).click()
        if count == 0:
            dialog = self.page.get_by_role("dialog", name="No Matching Records", exact=True)
            await dialog.get_by_role("button", name="OK", exact=True).click()
            await dialog.wait_for(state="hidden")
            self.documents = {}
            self.document_type = document_type
            return []
        # Switching a tab changes the entire FileMaker layout, not just the row contents.
        await expect(self.page.get_by_role("grid")).to_have_attribute("aria-rowcount", str(count))
        expected = min(10, count)
        rows = self.page.locator("tr.v-grid-row-has-data")
        if expected:
            await expect(rows.nth(expected - 1)).to_be_attached()
        documents = {}
        for index in range(expected):
            row = rows.nth(index)
            record = {}
            for name, object_id in {
                "title": 202,
                "date": 203,
                "security": 204,
            }.items():
                record[name] = (
                    await row.locator(f".fm_object_{object_id} .text").inner_text()
                ).strip()
            # FileMaker uses different object numbers for the document identifier in
            # Exhibits, Key Documents and Other Documents, but the field is clickable.
            record["id"] = (
                await row.locator(".fm-textarea.hand-cursor .text").inner_text()
            ).strip()
            if not record["id"] or record["id"] in documents:
                raise ValueError("Could not identify unique document rows")
            documents[record["id"]] = record
        self.documents = documents
        self.document_type = document_type
        return list(documents.values())

    async def download_document(self, document_id: str, directory: Path):
        if document_id not in self.documents:
            raise ValueError("Document was not observed in this matter's listing")
        row = self.page.locator("tr.v-grid-row-has-data").filter(
            has=self.page.locator(".fm-textarea.hand-cursor .text").filter(
                has_text=re.compile(rf"^\s*{re.escape(document_id)}\s*$")
            )
        )
        await row.get_by_role("button", name="GO GET IT", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="Download Files", exact=True)
        await dialog.wait_for()
        try:
            buttons = dialog.locator(".fm-download-button")
            await expect(buttons.first).to_be_visible()
            if await buttons.count() != 1:
                raise ValueError("Expected one attachment per document; found multiple files")
            async with self.page.expect_download(timeout=60000) as info:
                await buttons.first.click()
            download = await info.value
            filename = f"{safe_filename(document_id)}_{safe_filename(download.suggested_filename)}"
            path = directory / filename
            try:
                await download.save_as(path)
                failure = await download.failure()
                if failure:
                    raise ValueError(f"Download failed: {failure}")
                if path.stat().st_size == 0:
                    raise ValueError("Website returned an empty file")
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        finally:
            await dialog.get_by_role("button", name="Close", exact=True).click()
            await dialog.wait_for(state="hidden")
        return {
            "path": str(path.resolve()),
            "filename": filename,
            "title": self.documents[document_id]["title"],
        }
