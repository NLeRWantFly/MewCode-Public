"""Real Chromium acceptance driver for serve_acceptance; writes reviewable evidence."""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright


async def main(url, directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1600, 'height': 1000})
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        try:
            await page.goto(url + '/workflow-acceptance')
            result = await page.evaluate(Path(__file__).with_name('scenario.js').read_text(encoding='utf-8'))
            await page.screenshot(path=output / 'workbench.png')
            await page.reload()
            await page.wait_for_selector('.wb-workflow-card[data-node-id="database"][data-status="已完成"]')
            assert await page.locator('.wb-workflow-card').count() == 7
            result.update(restored=True, errors=errors)
            (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            assert not errors, errors
            print(json.dumps(result))
        except BaseException:
            await page.screenshot(path=output / 'failure.png')
            (output / 'failure.html').write_text(await page.content(), encoding='utf-8')
            print(errors)
            raise
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], sys.argv[2]))
