"""Opt-in task-tool checks against the public site, without a model or email."""

import os
import zipfile

import pytest

from document_retrieval_agent.retrieval.browser import BrowserSession
from document_retrieval_agent.retrieval.tools import Tools

pytestmark = pytest.mark.skipif(
    os.getenv("DOCUMENT_RETRIEVAL_AGENT_LIVE_TEST") != "1", reason="Live website checks are opt-in"
)


async def test_real_matter_downloads_and_empty_category(tmp_path):
    async with BrowserSession() as browser:
        tools = Tools(browser, tmp_path / "other")
        result = await tools.execute(
            "search_matter", {"matter_number": "M12205", "document_type": "Other Documents"}
        )
        assert "Halifax Regional Water Commission" in result["matter"]["metadata"]["title"]
        listing = await tools.execute("open_category", {"category": "Other Documents"})
        assert len(listing["documents"]) == 10
        for document in listing["documents"]:
            result = await tools.execute("download_document", {"document_id": document["id"]})
            assert "observation" not in result
            print(f"Downloaded {document['id']}: {result['download']['bytes']} bytes", flush=True)
        result = await tools.create_archive()
        assert result["status"] == "complete"
        with zipfile.ZipFile(result["archive"]) as archive:
            assert len(archive.namelist()) == 10
            assert all(archive.read(name).startswith(b"%PDF") for name in archive.namelist())
        empty = Tools(browser, tmp_path / "empty")
        await empty.search_matter("M12205", "Transcripts")
        assert (await empty.open_category("Transcripts"))["documents"] == []
        assert (await empty.create_archive())["status"] == "complete"


async def test_missing_matter_is_not_silently_recorded(tmp_path):
    async with BrowserSession() as browser:
        tools = Tools(browser, tmp_path)
        with pytest.raises(Exception) as error:
            await tools.execute(
                "search_matter", {"matter_number": "M99999", "document_type": "Other Documents"}
            )
        assert "No Records Found" in error.value.observation["text"]
        assert tools.matter is None
