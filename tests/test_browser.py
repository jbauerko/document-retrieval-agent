"""Browser actions against a local HTML fixture; no model or external network."""

import pytest

from document_retrieval_agent.retrieval.browser import BrowserActionError, BrowserSession

HTML = """
<html><body><label>Matter number <input aria-label="Matter number"></label>
<button onclick="document.querySelector('#result').textContent='M12205'">Search</button>
<div id="result"></div>
<a href="https://outside.example/">External</a>
<table><tr><td>102674 Application <button onclick="document.querySelector('[role=dialog]').hidden=false">GO GET IT</button></td></tr></table>
<div role="dialog" hidden><button onclick="document.querySelector('[role=dialog]').hidden=true">Close</button>
<button onclick="const a=document.createElement('a'); a.href='data:application/pdf;base64,JVBERi1maXh0dXJl'; a.download='application.pdf'; a.click()">application.pdf</button></div>
</body></html>
"""


@pytest.fixture
async def browser():
    async with BrowserSession(site_url="https://fixture.test/") as session:
        await session._start()

        async def serve(route):
            await route.fulfill(body=HTML, content_type="text/html")

        await session.page.route("https://fixture.test/**", serve)
        yield session


def ref(observation, label):
    return next(e["ref"] for e in observation["elements"] if e["label"] == label)


async def test_observe_act_refresh_and_stale_references(browser):
    observation = await browser.open_site()
    assert observation["rows"][0]["text"] == "102674 Application GO GET IT"
    old_ref = ref(observation, "Search")
    result = await browser.fill(ref(observation, "Matter number"), "M12205")
    assert (
        next(e for e in result["observation"]["elements"] if e["label"] == "Matter number")["value"]
        == "M12205"
    )
    with pytest.raises(BrowserActionError, match="stale") as caught:
        await browser.click(old_ref)
    assert caught.value.observation["snapshot_id"] > observation["snapshot_id"]
    latest = caught.value.observation
    result = await browser.click(ref(latest, "Search"))
    assert "M12205" in result["observation"]["text"]


async def test_dialog_download_and_scope(browser, tmp_path):
    observation = await browser.open_site()
    with pytest.raises(BrowserActionError, match="outside") as caught:
        await browser.click(ref(observation, "External"))
    observation = caught.value.observation
    result = await browser.click(ref(observation, "GO GET IT"))
    observation = result["observation"]
    assert observation["dialog"]
    assert {e["label"] for e in observation["elements"]} == {"Close", "application.pdf"}
    result = await browser.download(ref(observation, "application.pdf"), tmp_path, "102674")
    assert result["filename"] == "102674_application.pdf"
    assert (tmp_path / result["filename"]).read_bytes() == b"%PDF-fixture"
    with pytest.raises(ValueError, match="Supported keys"):
        await browser.press_key("Control+L")
