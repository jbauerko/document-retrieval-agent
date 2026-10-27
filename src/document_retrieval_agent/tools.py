"""Task tools and run state, independent of the model provider."""

import re
import zipfile
from pathlib import Path

CATEGORIES = ("Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings")


def tool(name, description, properties=None):
    properties = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    tool(
        "search_matter",
        "Find one matter and read its metadata and ALL category counts.",
        {"matter_number": {"type": "string", "pattern": "^M[0-9]{5}$"}},
    ),
    tool(
        "list_documents",
        "List up to 10 documents in the requested category, in website order.",
        {"document_type": {"type": "string", "enum": list(CATEGORIES)}},
    ),
    tool(
        "download_document",
        "Download a document ID returned by list_documents. Retry on failure.",
        {"document_id": {"type": "string"}},
    ),
    tool("create_archive", "ZIP successfully downloaded files. Returns verified counts and paths."),
]


def safe_filename(name: str) -> str:
    # Windows paths and ZIP traversal names must also be safe on Linux.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    return name if name and name not in {".", ".."} else "document"


class Tools:
    def __init__(self, browser, output: Path):
        self.browser = browser
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.matter = None
        self.document_type = None
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
        return await getattr(self, name)(**arguments)

    async def search_matter(self, matter_number: str):
        if not isinstance(matter_number, str) or not re.fullmatch(r"M[0-9]{5}", matter_number):
            raise ValueError("Matter number must be M followed by five digits")
        if self.matter:
            if self.matter["matter_number"] != matter_number:
                raise ValueError("One matter per run; start a new run for another matter")
            return self.matter
        self.matter = await self.browser.search_matter(matter_number)
        return self.matter

    async def list_documents(self, document_type: str):
        if not self.matter:
            raise ValueError("Search for a matter first")
        if document_type not in CATEGORIES:
            raise ValueError(f"Document type must be one of {CATEGORIES}")
        if self.document_type and self.document_type != document_type:
            raise ValueError("One document category per run")
        if not self.listed:
            documents = await self.browser.list_documents(document_type)
            self.documents = {d["id"]: d for d in documents[:10]}
            self.document_type = document_type
            self.listed = True
        return {
            "document_type": document_type,
            "total": self.matter["counts"][document_type],
            "documents": list(self.documents.values()),
        }

    async def download_document(self, document_id: str):
        if document_id not in self.documents:
            raise ValueError("Use a document ID from list_documents")
        if document_id in self.downloads:
            return self.downloads[document_id]
        if len(self.downloads) >= 10:
            raise ValueError("The download limit is 10 files per run")
        if self.archive:
            raise ValueError("The archive is finalized")
        directory = self.output / "downloads"
        directory.mkdir(exist_ok=True)
        try:
            result = await self.browser.download_document(document_id, directory)
            path = Path(result["path"]).resolve()
            if path.parent != directory.resolve() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("Download did not produce a nonempty file in the run directory")
        except Exception as exc:
            self.failures[document_id] = str(exc)
            raise
        result = {**result, "id": document_id, "path": str(path), "bytes": path.stat().st_size}
        self.downloads[document_id] = result
        self.failures.pop(document_id, None)
        return result

    async def create_archive(self):
        if not self.matter or not self.listed:
            raise ValueError("Search and list documents before packaging")
        counts = self.matter["counts"]
        if any(
            type(counts.get(category)) is not int or counts[category] < 0 for category in CATEGORIES
        ):
            raise ValueError("Cannot finalize: some category counts are unknown")
        expected = min(10, counts[self.document_type])
        if len(self.documents) != expected:
            raise ValueError("Document listing is incomplete; cannot finalize")
        if expected and not self.downloads:
            raise ValueError("No successful downloads; do not create a misleading empty archive")
        unattempted = set(self.documents) - self.downloads.keys() - self.failures.keys()
        if unattempted:
            raise ValueError("Attempt all listed documents before packaging")
        if not self.archive:
            name = (
                f"{self.matter['matter_number']}_{self.document_type.lower().replace(' ', '_')}.zip"
            )
            path = self.output / name
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
