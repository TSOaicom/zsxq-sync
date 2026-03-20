"""
login_zsxq.py — 首次登录知识星球，保存浏览器状态（cookies）供后续自动化使用。
使用方法：python login_zsxq.py
运行后会弹出浏览器窗口，扫码登录后自动保存 cookies。
"""

import asyncio
from playwright.async_api import async_playwright

STORAGE_STATE_PATH = "zsxq_storage_state.json"
DASHBOARD_URL = "https://wx.zsxq.com/dashboard/28885442528241/income"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://wx.zsxq.com/login")
        print("请在浏览器中扫码登录知识星球...")
        print("登录成功后将自动保存 cookies。")

        # Wait until redirected away from login page (max 120s for scanning)
        await page.wait_for_url("**/dashboard/**", timeout=120_000)
        print("登录成功！正在保存浏览器状态...")

        # Navigate to income page to ensure all relevant cookies are set
        await page.goto(DASHBOARD_URL)
        await page.wait_for_load_state("networkidle")

        # Save storage state (cookies + localStorage)
        await context.storage_state(path=STORAGE_STATE_PATH)
        print(f"浏览器状态已保存至 {STORAGE_STATE_PATH}")
        print("后续运行 sync_zsxq_to_feishu.py 即可自动爬取数据。")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
