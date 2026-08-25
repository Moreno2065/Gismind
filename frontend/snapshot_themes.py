from playwright.sync_api import sync_playwright

OUT_DARK = r"C:\tmp\gismind-dark.png"
OUT_LIGHT = r"C:\tmp\gismind-light.png"


def shoot(page, out_path):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    page.screenshot(path=out_path, full_page=False)
    print(f"saved {out_path}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page = ctx.new_page()

    page.goto("http://localhost:5173", wait_until="networkidle")
    page.wait_for_timeout(500)

    # 默认 — 暗色主题
    shoot(page, OUT_DARK)

    # 切到 light 主题 — 直接写 localStorage 再 reload，比点按钮稳
    page.evaluate("localStorage.setItem('gismind.theme','light')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(800)
    shoot(page, OUT_LIGHT)

    # 视觉对比：检查主题切换控件的 aria-checked
    dark_btn = page.locator('button[role=radio][aria-checked=true]')
    print("active radio text:", dark_btn.text_content())

    browser.close()

print("done")
