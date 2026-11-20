"""Read-only 20-second real-desktop browser loopback; saves metrics, never images/tokens."""
import asyncio
import json
from pathlib import Path
import re
import statistics
import tempfile
from playwright.async_api import async_playwright, expect
from brioche.session import Host


async def main():
    samples, encoded, errors = [], [], []
    with tempfile.TemporaryDirectory() as state:
        host = Host(port=0, folder=state, view_only=True, web_enabled=True, web_port=0)
        host.start()
        try:
            assert await asyncio.to_thread(host.ready.wait, 10) and not host.error, host.error
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                def received(event):
                    if isinstance(event, str):
                        value = json.loads(event)
                        if value.get("type") == "stats":
                            encoded.append(value)
                page.on("websocket", lambda socket: socket.on("framereceived", received))
                await page.goto(host.browser_url, wait_until="networkidle")
                await page.select_option("#quality", "1440p")
                await page.click("#connect")
                await page.locator("#overlay").wait_for(state="hidden", timeout=15000)
                await expect(page.locator("#metrics")).to_contain_text("H.264")
                await asyncio.sleep(3)
                for _ in range(10):
                    await asyncio.sleep(2)
                    metric = await page.locator("#metrics").inner_text()
                    match = re.search(r"提交 ([\d.]+) FPS", metric)
                    assert match, metric
                    samples.append(float(match[1]))
                assert not errors, errors
                width = await page.locator("#desktop").get_attribute("width")
                height = await page.locator("#desktop").get_attribute("height")
                await page.click("#disconnect")
                await browser.close()
        finally:
            host.stop()
            await asyncio.to_thread(host.join, 10)
    report = {"version":"0.4.0", "scope":"same-machine physical X11 desktop, ordinary mostly static desktop; headless Chrome",
        "resolution":f"{width}x{height}", "requested_fps":60, "encoder":"libx264 software",
        "browser_codec":"H.264 WebCodecs; hardware acceleration not established",
        "sample_duration_seconds":20, "canvas_submission_fps_samples":samples,
        "mean_canvas_submission_fps":round(statistics.mean(samples),1),
        "mean_server_encoded_fps":round(statistics.mean(s["encoded_fps"] for s in encoded),1),
        "limits":"Canvas submission is not display refresh or end-to-end latency; no dynamic workload or dual-machine network acceptance.",
        "input_injected":False, "desktop_images_saved":False}
    path=Path(__file__).resolve().parents[1]/"artifacts/browser-performance-04.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(report,ensure_ascii=False,indent=2))


asyncio.run(main())
