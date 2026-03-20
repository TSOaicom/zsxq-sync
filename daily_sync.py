"""
daily_sync.py — 每日自动爬取知识星球后台数据并写入飞书多维表格
无需手动操作，使用已保存的 cookies 自动登录。
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from playwright.async_api import async_playwright

# ──────────────────────────── 配置 ────────────────────────────

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a919d0c80978dccb")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "KmsAeBDZUYCabxkM1B0V3djLMpbPcMW2")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "WNT3b5w1Ya9qFdsNlUAcOG4TnQe")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "tblZ1KmICNtpVEMX")

ZSXQ_GROUP_ID = os.getenv("ZSXQ_GROUP_ID", "28885442528241")
STORAGE_STATE_PATH = os.getenv("STORAGE_STATE_PATH", "zsxq_storage_state.json")
DASHBOARD_URL = f"https://wx.zsxq.com/dashboard/{ZSXQ_GROUP_ID}/income"

BJT = timezone(timedelta(hours=8))
BASE = "https://open.feishu.cn/open-apis"

# ──────────────────────────── 飞书 API ────────────────────────────


def get_tenant_token():
    resp = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal", json={
        "app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET
    })
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    return data["tenant_access_token"]


def feishu_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


def add_records(token, table_id, records):
    url = f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}/records/batch_create"
    payload = {"records": [{"fields": r} for r in records]}
    resp = requests.post(url, headers=feishu_headers(token), json=payload)
    data = resp.json()
    if data.get("code") != 0:
        print(f"写入失败: {data}")
        return data
    print(f"成功写入 {len(records)} 条记录")
    return data


def ensure_fields(token, table_id, field_names):
    """确保多维表格中存在所需字段，缺失则自动创建"""
    url = f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}/fields"
    resp = requests.get(url, headers=feishu_headers(token))
    existing = set()
    if resp.json().get("code") == 0:
        for f in resp.json().get("data", {}).get("items", []):
            existing.add(f["field_name"])
    for name in field_names:
        if name not in existing:
            requests.post(url, headers=feishu_headers(token), json={"field_name": name, "type": 1})
            time.sleep(0.2)


def find_or_create_detail_table(token):
    """查找或创建收入明细表"""
    url = f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables"
    resp = requests.get(url, headers=feishu_headers(token))
    if resp.json().get("code") == 0:
        for t in resp.json().get("data", {}).get("items", []):
            if t["name"] == "收入明细":
                return t["table_id"]
    # 不存在则创建
    create_resp = requests.post(url, headers=feishu_headers(token), json={
        "table": {
            "name": "收入明细",
            "default_view_name": "默认视图",
            "fields": [
                {"field_name": "时间", "type": 1},
                {"field_name": "类型", "type": 1},
                {"field_name": "用户昵称", "type": 1},
                {"field_name": "支付金额(元)", "type": 1},
                {"field_name": "星主收入(元)", "type": 1},
                {"field_name": "订单状态", "type": 1},
            ]
        }
    })
    data = create_resp.json()
    if data.get("code") == 0:
        return data["data"]["table_id"]
    return None


# ──────────────────────────── 爬取知识星球 ────────────────────────────


async def scrape_zsxq():
    """用 Playwright 打开知识星球收入页面，提取概览数据和交易明细"""

    if not os.path.exists(STORAGE_STATE_PATH):
        print(f"错误: 找不到 {STORAGE_STATE_PATH}")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        page = await context.new_page()

        print(f"打开: {DASHBOARD_URL}")
        await page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(8000)

        if "/login" in page.url:
            print("错误: 登录状态已过期，请重新运行 login_zsxq.py")
            await browser.close()
            sys.exit(1)

        # ── 提取概览数据 ──
        page_text = await page.evaluate("() => document.body.innerText")

        # ── 提取交易明细（逐页） ──
        all_transactions = []
        for i in range(30):
            rows = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('ul li')).filter(li => {
                    return /20\\d{2}\\//.test(li.innerText) && li.innerText.includes('\\n');
                }).map(li => li.innerText.trim());
            }""")
            if not rows:
                break
            all_transactions.extend(rows)
            can_next = await page.evaluate("""() => {
                const btn = document.querySelector('.page-next');
                if (!btn || btn.classList.contains('page-next-disabled')) return false;
                btn.click();
                return true;
            }""")
            if not can_next:
                break
            await page.wait_for_timeout(2500)

        # 去重（分页可能重复抓取同一页）
        seen = set()
        unique_transactions = []
        for t in all_transactions:
            if t not in seen:
                seen.add(t)
                unique_transactions.append(t)

        # 刷新 cookies
        await context.storage_state(path=STORAGE_STATE_PATH)
        await browser.close()

    return page_text, unique_transactions


def parse_overview(text):
    """从页面文本解析概览数据"""
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    record = {"日期": today}

    patterns = {
        "累积收入(元)": r"累积收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "本周收入(元)": r"本周收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "上周收入(元)": r"上周收入\s+([\d,]+\.?\d*)",
        "本月收入(元)": r"本月收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "上月收入(元)": r"上月收入\s+([\d,]+\.?\d*)",
        "付费加入收入(元)": r"付费加入收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "续期收入(元)": r"续期收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "赞赏收入(元)": r"赞赏收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "付费提问收入(元)": r"付费提问收入\(元\)\s*\n\s*([\d,]+\.?\d*)",
        "总成员数": r"总成员数\s*\n\s*(\d+)\s*\n\s*昨日加入",
        "付费加入成员": r"付费加入成员\s*\n\s*(\d+)",
        "本月续期成员": r"本月续期成员\s*\n\s*(\d+)",
        "昨日续期成员": r"昨日续期成员\s*\n\s*(\d+)",
        "访问预览页人数": r"(\d+)\s*\n\s*访问星球预览页的人数",
        "点击加入按钮人数": r"(\d+)\s*\n\s*点击「加入星球」按钮人数",
        "成功支付人数": r"(\d+)\s*\n\s*成功加入星球的人数",
        "流量转化率": r"流量转化率\s*\n\s*([\d.]+%)",
        "支付成功率": r"支付成功率\s*\n\s*([\d.]+%)",
        "30日付费转化率": r"30日付费转化率\s*\n\s*([\d.]+%)",
        "续期转化率": r"30日续期转化率\s*\n\s*([\d.]+%)",
        "有效期内成员数": r"(\d+)\s*\n\s*总成员数\s*\n\s*总成员数\s*\n\s*(\d+)",
        "近7天活跃成员": r"(\d+)\s*\n\s*近 7 天活跃成员数",
        "成员周活跃比例": r"有效期内成员周活跃比例\s*\n\s*([\d.]+%)",
        "App下载成员数": r"(\d+)\s*\n\s*下载过 App 的成员数",
        "App下载率": r"总成员的 App 下载率\s*\n\s*([\d.]+%)",
        "新成员月留存率": r"新成员月留存率:\s*([\d.]+%)",
    }

    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            record[field] = m.group(1).replace(",", "")

    # 提取昨日收入（第一个出现的）
    m = re.search(r"累积收入.*?昨日收入\s+([\d.]+)", text, re.DOTALL)
    if m:
        record["昨日收入(元)"] = m.group(1)
    m = re.search(r"总成员数.*?昨日加入成员\s+(\d+)", text, re.DOTALL)
    if m:
        record["昨日加入成员"] = m.group(1)

    # 特殊处理：有效期内成员数
    m = re.search(r"(\d+)\s*\n\s*总成员数\s*\n\s*总成员数\s*\n\s*(\d+)", text)
    if m:
        record["有效期内成员数"] = m.group(1)
    # 总成员数(含免费)
    m = re.search(r"(\d+)\s*\n\s*总成员数\s*\n\s*总成员数 = 付费加入成员", text)
    if m:
        record["总成员数(含免费)"] = m.group(1)

    return record


def parse_transactions(raw_rows):
    """解析交易明细文本行"""
    records = []
    for row in raw_rows:
        parts = row.split("\n")
        if len(parts) >= 6:
            records.append({
                "时间": parts[0].strip(),
                "类型": parts[1].strip(),
                "用户昵称": parts[2].strip(),
                "支付金额(元)": parts[3].strip(),
                "星主收入(元)": parts[4].strip(),
                "订单状态": parts[5].strip(),
            })
    return records


# ──────────────────────────── 主流程 ────────────────────────────


async def main():
    now = datetime.now(BJT)
    print("=" * 60)
    print(f"知识星球 → 飞书 每日数据同步")
    print(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 60)

    # 1. 爬取
    print("\n[1/3] 爬取知识星球后台...")
    page_text, raw_transactions = await scrape_zsxq()
    print(f"  页面文本: {len(page_text)} 字符")
    print(f"  交易明细: {len(raw_transactions)} 条（去重后）")

    # 2. 解析
    print("\n[2/3] 解析数据...")
    overview = parse_overview(page_text)
    transactions = parse_transactions(raw_transactions)
    print(f"  概览字段: {len(overview)} 个")
    for k, v in overview.items():
        print(f"    {k}: {v}")
    print(f"  交易记录: {len(transactions)} 条")

    # 3. 写入飞书
    print("\n[3/3] 写入飞书多维表格...")
    token = get_tenant_token()

    # 写概览
    ensure_fields(token, FEISHU_TABLE_ID, overview.keys())
    print("  写入概览数据...")
    add_records(token, FEISHU_TABLE_ID, [overview])

    # 写明细
    if transactions:
        detail_table_id = find_or_create_detail_table(token)
        if detail_table_id:
            print(f"  写入 {len(transactions)} 条交易明细...")
            # 分批写入（每批最多 500 条）
            for i in range(0, len(transactions), 500):
                batch = transactions[i:i+500]
                add_records(token, detail_table_id, batch)
        else:
            print("  无法获取明细表，跳过交易明细")

    print("\n" + "=" * 60)
    print("同步完成！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
