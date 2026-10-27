"""Opt-in real-site checks: DOCUMENT_RETRIEVAL_AGENT_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s."""

import os
import zipfile

import pytest

from document_retrieval_agent.browser import UARBBrowser
from document_retrieval_agent.tools import Tools

pytestmark = pytest.mark.skipif(
    os.getenv("DOCUMENT_RETRIEVAL_AGENT_LIVE_TEST") != "1", reason="Live website checks are opt-in"
)


async def test_real_matter_downloads_and_empty_category(tmp_path):
    async with UARBBrowser() as browser:
        tools = Tools(browser, tmp_path / "other_documents")
        matter = await tools.search_matter("M12205")
        assert matter["matter_number"] == "M12205"
        assert "Halifax Regional Water Commission" in matter["metadata"]["title"]
        listing = await tools.list_documents("Other Documents")
        assert len(listing["documents"]) == 10
        for document in listing["documents"]:
            result = await tools.download_document(document["id"])
            print(f"Downloaded {result['id']}: {result['bytes']} bytes", flush=True)
        result = await tools.create_archive()
        assert result["status"] == "complete"
        with zipfile.ZipFile(result["archive"]) as archive:
            assert len(archive.namelist()) == 10
            for name in archive.namelist():
                if name.lower().endswith(".pdf"):
                    assert archive.read(name).startswith(b"%PDF")
        # The adapter supports tab changes; each agent run still requests just one category.
        assert await browser.list_documents("Transcripts") == []
        assert await browser.list_documents("Recordings") == []
        keys = await browser.list_documents("Key Documents")
        assert len(keys) == min(10, matter["counts"]["Key Documents"])
        print(f"Listed {len(keys)} Key Documents", flush=True)
        (tmp_path / "keys").mkdir()
        assert (await browser.download_document(keys[0]["id"], tmp_path / "keys"))["path"]
        exhibits = await browser.list_documents("Exhibits")
        assert len(exhibits) == min(10, matter["counts"]["Exhibits"])
        (tmp_path / "exhibits").mkdir()
        assert (await browser.download_document(exhibits[0]["id"], tmp_path / "exhibits"))["path"]


async def test_missing_matter_is_not_silently_replaced():
    async with UARBBrowser() as browser:
        with pytest.raises(ValueError, match="Could not open requested matter M99999"):
            await browser.search_matter("M99999")
