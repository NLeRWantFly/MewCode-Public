from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect


@pytest.mark.asyncio
async def test_review_consent_shows_exact_action_without_persistent_grant():
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    functions = source[source.index("  function formatPermissionText("):
                       source.index("  function replayPendingConversationPrompt(")]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<div id="bar" class="hidden"><span id="perm-tool"></span>'
                                   '<span id="perm-desc"></span><button id="perm-always">始终允许</button>'
                                   '<button id="perm-directory">授权目录</button></div>')
            await page.add_script_tag(content='const $=id=>document.getElementById(id); '
                                     'const permBar=$("bar"); let currentPermissionConversationId; ' + functions)
            await page.evaluate('showPermissionMessage({tool:"WriteFile", description:"result.txt", reason:"常规授权"}, "owner")')
            await expect(page.locator("#perm-always")).to_be_visible()
            await page.evaluate('showPermissionMessage({tool:"WriteFile", description:"result.txt", '
                                'reason:"主 Agent 请求一次复审", source:"review_missing_authorization"}, "owner")')
            await expect(page.locator("#perm-always")).to_be_hidden()
            await expect(page.locator("#perm-directory")).to_be_hidden()
            await expect(page.locator("#perm-desc")).to_have_text("result.txt · 主 Agent 请求一次复审")
        finally:
            await browser.close()
