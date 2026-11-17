"""Exercise the built native WebView against a controlled local backend (no model calls)."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess

from playwright.async_api import async_playwright, expect


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--theme', choices=['light', 'dark'], default='light')
    parser.add_argument('--cancel', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'ui-state.json').write_text(json.dumps({'theme': args.theme}), encoding='utf-8')
    if not args.cancel:
        (output / 'fail-startup').write_text('test first failure', encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        debug_port = sock.getsockname()[1]
    env = dict(os.environ, MEWCODE_USER_DATA_DIR=str(output), MEWCODE_REPO_ROOT=str(root),
               MEWCODE_BACKEND=str(args.backend.resolve()),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={debug_port}')
    process = subprocess.Popen([str(root / 'src-tauri/target/release/mewcode-tauri.exe')],
        cwd=root, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (output / 'launch.json').write_text(json.dumps({'pid': process.pid, 'debug_port': debug_port}), encoding='utf-8')
    async with async_playwright() as pw:
        for attempt in range(60):
            try:
                browser = await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{debug_port}', timeout=1500)
                break
            except Exception:
                if process.poll() is not None or attempt == 59:
                    raise
                await asyncio.sleep(.25)
        page = browser.contexts[0].pages[0]
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        await expect(page.locator('h1')).to_have_text('MewCode')
        await expect(page.locator('html')).to_have_attribute('data-theme', args.theme)
        if args.cancel:
            await expect(page.locator('#message')).to_have_text('正在启动本地服务')
            await page.screenshot(path=str(output / 'loading.png'))
            result = dict(status='ready_to_close', pid=process.pid, errors=errors)
        else:
            await expect(page.locator('#message')).to_have_text('启动未完成', timeout=20_000)
            await expect(page.get_by_role('button', name='重试启动')).to_be_enabled()
            await page.get_by_text('启动详情', exact=True).click()
            await expect(page.locator('#detail')).to_contain_text('exit code: 7')
            await page.screenshot(path=str(output / 'failure.png'))
            (output / 'fail-startup').unlink()
            await page.get_by_role('button', name='重试启动').click()
            await expect(page.locator('#message')).to_have_text('正在启动本地服务')
            await page.screenshot(path=str(output / 'loading.png'))
            # Health has not been released: loading remains interactive in the same native window.
            assert await page.locator('#actions').is_hidden()
            (output / 'ready').write_text('allow health', encoding='utf-8')
            await expect(page.get_by_role('heading', name='MewCode 启动验证通过')).to_be_visible(timeout=20_000)
            await page.screenshot(path=str(output / 'ready.png'))
            assert (output / 'fixture-attempt.txt').read_text() == '2'
            receipts = [json.loads(path.read_text()) for path in output.glob('fixture-child-*.json')]
            assert len(receipts) == 2 and all(row['console_window'] == 0 for row in receipts)
            assert not errors, errors
            result = dict(status='passed', pid=process.pid, attempts=2, console_windows=0,
                          failure_visible=True, retry_worked=True, automatic_navigation=True, errors=errors)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        # Leave the native window for the explicit close/cancellation check.


if __name__ == '__main__':
    asyncio.run(main())
