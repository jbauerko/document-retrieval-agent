"""Run state and task/recovery tools for verified document retrieval."""

import re
import zipfile
from pathlib import Path

from .documents import CATEGORIES, METADATA_FIELDS, document_in_text, normalize, parse_counts
from .schemas import TOOL_SCHEMAS


class Tools:
    def __init__(self, browser, output: Path):
        self.browser = browser
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.requested_matter = None
        self.document_type = None
        self.selected_category = None
        self.pending_document_id = None
        self.matter = None
        self.documents = {}
        self.listed = False
        self.downloads = {}
        self.failures = {}
        self.archive = None

    async def execute(self, name: str, arguments: dict):
        if name not in {t["function"]["name"] for t in TOOL_SCHEMAS}:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        try:
            return await getattr(self, name)(**arguments)
        except Exception as exc:
            # Task failures expose the current browser state for model-directed recovery.
            if not getattr(exc, "observation", None) and getattr(self.browser, "page", None):
                try:
                    exc.observation = await self.browser.observe_page()
                except Exception:
                    pass
            raise

    async def open_site(self, matter_number: str, document_type: str):
        if not re.fullmatch(r"M[0-9]{5}", matter_number):
            raise ValueError("Matter number must be M followed by five digits")
        if document_type not in CATEGORIES:
            raise ValueError(f"Document type must be one of {CATEGORIES}")
        if self.requested_matter and (
            matter_number != self.requested_matter or document_type != self.document_type
        ):
            raise ValueError("One matter and one document category per run")
        self.requested_matter, self.document_type = matter_number, document_type
        self.selected_category = self.pending_document_id = None
        return {"observation": await self.browser.open_site()}

    async def search_matter(self, matter_number: str, document_type: str):
        if self.matter or self.listed or self.downloads:
            raise ValueError(
                "Matter already recorded; continue with open_category or browser recovery"
            )
        await self.open_site(matter_number, document_type)
        metadata = await self.browser.search_matter(matter_number)
        result = await self.record_matter(metadata)
        return {"matter": result["matter"]}

    async def open_category(self, category: str):
        if not self.matter or category != self.document_type:
            raise ValueError("Search the matter first, then open only the requested category")
        if self.listed:
            return {
                "documents": list(self.documents.values()),
                "total": self.matter["counts"][category],
            }
        self._verify_matter(await self.browser.read_text())
        documents = await self.browser.category_documents(category, self.matter["counts"][category])
        self.selected_category = category
        self.pending_document_id = None
        result = await self.record_documents(documents)
        return {"documents": result["documents"], "total": result["total"]}

    async def download_document(self, document_id: str):
        if document_id not in self.documents:
            raise ValueError("Use a document ID recorded from the observed listing")
        if document_id in self.downloads:
            return {"download": self.downloads[document_id]}
        if self.selected_category != self.document_type or self.archive:
            raise ValueError(
                "Open the requested category before downloading; archive must not be finalized"
            )
        try:
            observation = await self.browser.observe_page()
            self._verify_matter(observation["text"])
            if observation.get("dialog"):
                raise ValueError(
                    "A dialog is open; inspect and close it with browser tools before retrying"
                )
            controls = [
                e
                for e in observation["elements"]
                if e["label"] == "GO GET IT"
                and document_in_text(self.documents[document_id], e["context"])
            ]
            if len(controls) != 1:
                raise ValueError(
                    "Could not identify this document's Go Get It control; inspect the page"
                )
            observation = (await self.click(controls[0]["ref"]))["observation"]
            files = [
                e
                for e in observation["elements"]
                if e["label"] != "Close"
                and (e["tag"] in {"button", "a"} or e.get("role") == "button")
            ]
            if not observation.get("dialog") or len(files) != 1:
                raise ValueError("Expected a download dialog with one attachment; inspect the page")
            result = await self.download(files[0]["ref"], document_id)
        except Exception as exc:
            self.failures[document_id] = str(exc)
            raise
        # Saving succeeded even if closing the dialog fails. Never repeat a saved download.
        observation = result["observation"]
        try:
            close = next(e for e in observation["elements"] if e["label"] == "Close")
            await self.click(close["ref"])
        except Exception as exc:
            return {
                "download": result["download"],
                "warning": f"File saved; close dialog before continuing: {exc}",
                "observation": await self.browser.observe_page(),
            }
        return {"download": result["download"], "downloaded_count": len(self.downloads)}

    async def observe_page(self):
        return {"observation": await self.browser.observe_page()}

    async def click(self, element_ref: str):
        result = await self.browser.click(element_ref)
        target = result["target"]
        for category in CATEGORIES:
            if re.fullmatch(rf"{re.escape(category)}\s*-\s*[\d,]+", target["label"]):
                self.selected_category = category
                self.pending_document_id = None
        for document in self.documents.values():
            if document_in_text(document, target["context"]):
                self.pending_document_id = document["id"]
                break
        return result

    async def fill(self, element_ref: str, text: str):
        return await self.browser.fill(element_ref, text)

    async def press_key(self, key: str):
        return await self.browser.press_key(key)

    async def scroll(self, direction: str, element_ref: str | None = None):
        return await self.browser.scroll(direction, element_ref)

    def _verify_matter(self, text: str) -> dict:
        if not self.requested_matter or not re.search(rf"\b{self.requested_matter}\b", text):
            raise ValueError("The requested matter number is not present on the page")
        counts = parse_counts(text)
        if self.matter and counts != self.matter["counts"]:
            raise ValueError("Page counts changed; do not download from a different matter")
        return counts

    async def record_matter(self, metadata: dict):
        observation = await self.browser.observe_page()
        text = observation["text"]
        counts = self._verify_matter(text)
        if not isinstance(metadata, dict) or set(metadata) != set(METADATA_FIELDS):
            raise ValueError("Use the declared matter metadata fields")
        if not metadata["title"]:
            raise ValueError("Record the observed matter title")
        for value in metadata.values():
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or normalize(value) not in normalize(text)
            ):
                raise ValueError(
                    "Metadata must be copied from observed page text, or null if unavailable"
                )
        self.matter = {
            "matter_number": self.requested_matter,
            "metadata": metadata,
            "counts": counts,
            "source": self.browser.site_url,
        }
        return {"matter": self.matter, "observation": observation}

    async def record_documents(self, documents: list[dict]):
        if not self.matter or self.selected_category != self.document_type:
            raise ValueError("Record the matter and click the requested document category first")
        observation = await self.browser.observe_page()
        self._verify_matter(observation["text"])
        expected = min(10, self.matter["counts"][self.document_type])
        if not isinstance(documents, list) or len(documents) != expected:
            raise ValueError(f"Record exactly {expected} documents from the requested category")
        matched_rows = []
        for document in documents:
            if (
                not isinstance(document, dict)
                or set(document) != {"id", "title"}
                or not isinstance(document["id"], str)
                or not isinstance(document["title"], str)
                or not document["title"].strip()
            ):
                raise ValueError("Each document needs an observed id and title")
            matches = [
                i
                for i, row in enumerate(observation["rows"])
                if document_in_text(document, row["text"])
            ]
            if not matches:
                raise ValueError(f"Document {document['id']} was not found in an observed row")
            matched_rows.append(matches[0])
        if matched_rows != list(range(expected)) or len({d["id"] for d in documents}) != expected:
            raise ValueError("Record the first documents in observed row order, without duplicates")
        if self.listed and documents != list(self.documents.values()):
            raise ValueError("The document selection is already recorded")
        self.documents = {d["id"]: d for d in documents}
        self.listed = True
        return {
            "documents": documents,
            "total": self.matter["counts"][self.document_type],
            "observation": observation,
        }

    async def download(self, element_ref: str, document_id: str):
        if document_id not in self.documents:
            raise ValueError("Use a document ID recorded from the observed listing")
        if document_id in self.downloads:
            return {"download": self.downloads[document_id]}
        if self.archive or len(self.downloads) >= 10:
            raise ValueError("Archive finalized or the 10-file download limit reached")
        if self.selected_category != self.document_type or self.pending_document_id != document_id:
            raise ValueError(
                "Click this document's observed row control in the requested category first"
            )
        self._verify_matter(await self.browser.read_text())
        directory = self.output / "downloads"
        directory.mkdir(exist_ok=True)
        try:
            result = await self.browser.download(element_ref, directory, document_id)
            observation = result.pop("observation")
            path = Path(result["path"]).resolve()
            if path.parent != directory.resolve() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("Download did not produce a nonempty file in the run directory")
        except Exception as exc:
            self.failures[document_id] = str(exc)
            raise
        result.update(
            id=document_id, title=self.documents[document_id]["title"], bytes=path.stat().st_size
        )
        self.downloads[document_id] = result
        self.failures.pop(document_id, None)
        self.pending_document_id = None
        return {
            "download": result,
            "downloaded_count": len(self.downloads),
            "observation": observation,
        }

    async def create_archive(self):
        if not self.matter or not self.listed:
            raise ValueError("Record the matter and document listing before packaging")
        expected = min(10, self.matter["counts"][self.document_type])
        if expected and not self.downloads:
            raise ValueError("No successful downloads; do not create a misleading empty archive")
        if set(self.documents) - self.downloads.keys() - self.failures.keys():
            raise ValueError("Attempt all recorded documents before packaging")
        if not self.archive:
            path = (
                self.output
                / f"{self.requested_matter}_{self.document_type.lower().replace(' ', '_')}.zip"
            )
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for download in self.downloads.values():
                    file = Path(download["path"])
                    archive.write(file, arcname=file.name)
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None or len(archive.namelist()) != len(self.downloads):
                    raise ValueError("ZIP validation failed")
            self.archive = str(path)
        return self.result()

    def result(self):
        complete = bool(self.archive) and len(self.downloads) == min(
            10, self.matter["counts"][self.document_type]
        )
        return {
            "status": "complete" if complete else "partial" if self.downloads else "incomplete",
            "matter": self.matter,
            "document_type": self.document_type,
            "total_files": sum(self.matter["counts"].values()) if self.matter else None,
            "downloaded_count": len(self.downloads),
            "downloads": list(self.downloads.values()),
            "failures": self.failures,
            "archive": self.archive,
        }
