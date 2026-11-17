import json
import re
import subprocess
from pathlib import Path
import pytest
from playwright.async_api import async_playwright
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env


@pytest.mark.asyncio
async def test_real_question_renderer_preserves_case_wait_and_other_node_execution():
    if not Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').exists():
        pytest.skip('Isolated headless Edge unavailable')
    root = Path(__file__).resolve().parents[1]
    html = (root / 'frontend/static/index.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S | re.I)
    fixture = subprocess.run(['node', '-p', "require('./tests/ask_user_choices_renderer.cjs')"],
        cwd=root, check=True, capture_output=True, text=True, encoding='utf-8').stdout
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            await page.set_content(html)
            result = await page.evaluate(fixture)
            assert result == {'choices': True, 'stableIds': True, 'custom': True,
                'legacy': True, 'waitStatus': True}
        finally:
            await browser.close()
