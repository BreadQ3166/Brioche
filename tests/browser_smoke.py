"""Real Chromium HTTPS/WebSocket/H.264/input/screen-switch test on isolated X11.

Requires the development-only playwright package and local Google Chrome. It does
not install browser extensions or use the user's browser profile.
"""
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from PySide6.QtGui import QImage
from Xlib import X, XK
from Xlib.display import Display
from Xlib.ext import xtest
from brioche.session import Host
from test_multiscreen import MONITORS, RegionCapture

root = Path(__file__).resolve().parents[1]


async def until(condition, timeout=5):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if condition():
            return True
        await asyncio.sleep(.02)
    return False


async def main():
    with tempfile.TemporaryDirectory() as state, patch("brioche.capture.list_monitors", return_value=MONITORS), patch("brioche.media.DesktopCapture", RegionCapture):
        host = Host(port=0, folder=state, view_only=False, web_enabled=True, web_port=0)
        host.start()
        assert await asyncio.to_thread(host.ready.wait, 10) and not host.error, host.error
        d = Display()
        previous_focus = d.get_input_focus().focus
        window = d.screen().root.create_window(80, 80, 400, 160, 0, d.screen().root_depth,
                    X.InputOutput, X.CopyFromParent, override_redirect=True,
                    background_pixel=0xbd713d, event_mask=X.KeyPressMask | X.KeyReleaseMask)
        window.map()
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        d.sync()
        code = d.keysym_to_keycode(XK.string_to_keysym("b"))
        report = {"browser": "Chromium", "transport": "HTTPS / WebSocket / H.264 WebCodecs + JPEG fallback",
                  "scope": "same-machine isolated X11 desktop, virtual landscape/portrait regions",
                  "certificate": "browser test context trusts test certificate errors; SAN/trust verified separately by HTTPS tests"}
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                                                           args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
                page = await context.new_page()
                errors, origins = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: origins.append(urlsplit(request.url).netloc))
                await page.goto(host.browser_url, wait_until="networkidle")
                assert not urlsplit(page.url).fragment, "Token was retained in address bar"
                await page.screenshot(path=str(root / "artifacts/browser-login-04.png"))
                await page.select_option("#quality", "720p")
                await page.click("#connect")
                await page.locator("#session").wait_for(state="visible", timeout=15000)
                await page.locator("#overlay").wait_for(state="hidden", timeout=15000)
                await expect(page.locator("#metrics")).to_contain_text("H.264", timeout=10000)
                report["h264_webcodecs"] = "passed: actual browser video decode"
                # Inspect rendered pixels without asking the page to execute eval (CSP stays enforced).
                box = await page.locator("#desktop").bounding_box()
                screenshot = QImage.fromData(await page.locator("#desktop").screenshot())
                scale = min(box["width"]/1280, box["height"]/720)
                color = screenshot.pixelColor(round((box["width"]-1280*scale)/2+150*scale),
                                               round((box["height"]-720*scale)/2+130*scale))
                pixel = [color.red(),color.green(),color.blue()]
                assert pixel[0] > 150 and 60 < pixel[1] < 160 and pixel[2] < 120, pixel
                report["desktop_pixels"] = "verified actual captured test-window color"
                await page.click("#control")
                box = await page.locator("#desktop").bounding_box()
                await page.mouse.move(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
                def pointer_correct():
                    p = d.screen().root.query_pointer()
                    return abs(p.root_x - 640) <= 1 and abs(p.root_y - 360) <= 1
                assert await until(pointer_correct), "Browser mouse did not reach remote coordinates"
                report["mouse"] = "passed"
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                d.sync()
                await page.keyboard.down("b")
                assert await until(lambda: d.query_keymap()[code // 8] & (1 << (code % 8))), "Browser keyboard down failed"
                await page.click("#release")
                assert await until(lambda: not d.query_keymap()[code // 8] & (1 << (code % 8))), "Browser release failed"
                await page.keyboard.up("b")
                report["keyboard_and_release"] = "passed"
                await page.screenshot(path=str(root / "artifacts/browser-session-04.png"))
                await page.select_option("#monitors", "2")
                await expect(page.locator("#desktop")).to_have_attribute("width", "720", timeout=15000)
                await expect(page.locator("#desktop")).to_have_attribute("height", "1280", timeout=15000)
                await page.locator("#overlay").wait_for(state="hidden", timeout=15000)
                report["portrait_switch"] = "passed: 720x1280 without reconnect"
                await page.screenshot(path=str(root / "artifacts/browser-portrait-04.png"))
                assert await page.locator("#control").inner_text() == "启用控制"
                await asyncio.sleep(.4)
                await page.select_option("#monitors", "3")
                await expect(page.locator("#monitors")).to_have_value("2",timeout=10000)
                await page.locator("#overlay").wait_for(state="hidden",timeout=10000)
                await expect(page.locator("#metrics")).to_contain_text("H.264")
                report["failed_switch"] = "old H.264 dependency chain preserved"
                await page.click("#disconnect")
                await page.locator("#welcome").wait_for(state="visible", timeout=8000)
                assert await until(lambda: not host.active)
                report["disconnect"] = "passed, native session released"
                # Verify both preflight capability and runtime decoder fallback,
                # without weakening the product page's CSP.
                cases = {
                    "capability_fallback": 'Object.defineProperty(window,"VideoDecoder",{value:undefined});',
                    "runtime_fallback": 'const original=VideoDecoder.isConfigSupported.bind(VideoDecoder); VideoDecoder.isConfigSupported=c=>c.codedWidth?Promise.resolve({supported:false,config:c}):original(c);',
                }
                for name, initialization in cases.items():
                    fallback_context=await browser.new_context(ignore_https_errors=True,viewport={"width":1280,"height":900})
                    await fallback_context.add_init_script(initialization)
                    fallback_page=await fallback_context.new_page()
                    await fallback_page.goto(host.browser_url,wait_until="networkidle")
                    await fallback_page.select_option("#quality","720p")
                    await fallback_page.click("#connect")
                    await fallback_page.locator("#session").wait_for(state="visible",timeout=15000)
                    await fallback_page.locator("#overlay").wait_for(state="hidden",timeout=15000)
                    await expect(fallback_page.locator("#metrics")).to_contain_text("JPEG",timeout=15000)
                    report[name]="passed: automatic JPEG reconnect"
                    await fallback_page.click("#disconnect")
                    assert await until(lambda:not host.active)
                    await fallback_context.close()
                assert not errors, errors
                assert all(origin == urlsplit(host.browser_url).netloc for origin in origins), "External page dependency detected"
                report["external_page_dependencies"] = "none"
                await browser.close()
        finally:
            xtest.fake_input(d, X.KeyRelease, code)
            d.set_input_focus(previous_focus, X.RevertToParent, X.CurrentTime)
            window.destroy()
            d.sync()
            d.close()
            host.stop()
            await asyncio.to_thread(host.join, 10)
            assert not host._thread.is_alive()
        report["pass"] = True
        (root / "artifacts/browser-smoke-04.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
