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
MEMBER_ACTIVE_URL = f"https://wx.zsxq.com/dashboard/{ZSXQ_GROUP_ID}/member_active"
CONTENT_ACTIVE_URL = f"https://wx.zsxq.com/dashboard/{ZSXQ_GROUP_ID}/content_active"
PROMOTION_CODE_URL = f"https://wx.zsxq.com/dashboard/{ZSXQ_GROUP_ID}/promotion_code"
PROMOTION_DATA_URL = f"https://wx.zsxq.com/dashboard/{ZSXQ_GROUP_ID}/promotion_data"

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
    created = 0
    for name in field_names:
        if name not in existing:
            r = requests.post(url, headers=feishu_headers(token), json={"field_name": name, "type": 1})
            if r.json().get("code") == 0:
                created += 1
            time.sleep(0.3)
    if created > 0:
        print(f"    新建 {created} 个字段")
        time.sleep(1)  # 等待字段创建生效


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


def find_or_create_table(token, table_name, fields):
    """查找或创建指定名称的表"""
    url = f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables"
    resp = requests.get(url, headers=feishu_headers(token))
    if resp.json().get("code") == 0:
        for t in resp.json().get("data", {}).get("items", []):
            if t["name"] == table_name:
                return t["table_id"]
    create_resp = requests.post(url, headers=feishu_headers(token), json={
        "table": {
            "name": table_name,
            "default_view_name": "默认视图",
            "fields": [{"field_name": f, "type": 1} for f in fields]
        }
    })
    data = create_resp.json()
    if data.get("code") == 0:
        return data["data"]["table_id"]
    return None


# ──────────────────────────── 爬取知识星球 ────────────────────────────


def is_data_in_reset(text):
    """检测知识星球后台是否处于数据重置期（凌晨~8点）"""
    # 如果总成员数为0且昨日加入成员为负数，说明在重置期
    m = re.search(r"总成员数\s*\n\s*0\s*\n\s*昨日加入成员\s+-\d+", text)
    return m is not None


async def scrape_zsxq():
    """用 Playwright 打开知识星球各页面，提取概览数据、交易明细、成员活跃、内容活跃"""

    if not os.path.exists(STORAGE_STATE_PATH):
        print(f"错误: 找不到 {STORAGE_STATE_PATH}")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        page = await context.new_page()

        # ── 1. 收入页面 ──
        print(f"  打开: {DASHBOARD_URL}")
        await page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(8000)

        if "/login" in page.url:
            print("错误: 登录状态已过期，请重新运行 login_zsxq.py")
            await browser.close()
            sys.exit(1)

        income_text = await page.evaluate("() => document.body.innerText")

        # 检测数据重置期并重试
        if is_data_in_reset(income_text):
            print("  ⚠ 检测到数据重置期（总成员数为0），等待重试...")
            for retry in range(3):
                wait_sec = 120 * (retry + 1)  # 120s, 240s, 360s
                print(f"  等待 {wait_sec} 秒后第 {retry+1} 次重试...")
                await page.wait_for_timeout(wait_sec * 1000)
                await page.reload(wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(8000)
                income_text = await page.evaluate("() => document.body.innerText")
                if not is_data_in_reset(income_text):
                    print("  ✓ 数据已恢复正常")
                    break
            else:
                print("  ⚠ 重试后数据仍在重置期，将使用恢复算法补偿")

        # 提取交易明细（逐页）
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

        # 去重
        seen = set()
        unique_transactions = []
        for t in all_transactions:
            if t not in seen:
                seen.add(t)
                unique_transactions.append(t)

        # ── 2. 成员活跃页面 ──
        print(f"  打开: {MEMBER_ACTIVE_URL}")
        await page.goto(MEMBER_ACTIVE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        member_active_text = await page.evaluate("() => document.body.innerText")

        # 提取成员明细（逐页）
        all_members = []
        for i in range(30):
            rows = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('ul li')).filter(li => {
                    return /20\\d{2}\\//.test(li.innerText) && li.innerText.includes('\\n');
                }).map(li => li.innerText.trim());
            }""")
            if not rows:
                break
            all_members.extend(rows)
            can_next = await page.evaluate("""() => {
                const btn = document.querySelector('.page-next');
                if (!btn || btn.classList.contains('page-next-disabled')) return false;
                btn.click();
                return true;
            }""")
            if not can_next:
                break
            await page.wait_for_timeout(2500)
        seen_m = set()
        unique_members = []
        for m in all_members:
            if m not in seen_m:
                seen_m.add(m)
                unique_members.append(m)

        # ── 3. 内容活跃页面 ──
        print(f"  打开: {CONTENT_ACTIVE_URL}")
        await page.goto(CONTENT_ACTIVE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        content_active_text = await page.evaluate("() => document.body.innerText")

        # 提取内容明细（逐页）
        all_contents = []
        for i in range(30):
            rows = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('ul li')).filter(li => {
                    return /20\\d{2}\\//.test(li.innerText) && li.innerText.includes('\\n');
                }).map(li => li.innerText.trim());
            }""")
            if not rows:
                break
            all_contents.extend(rows)
            can_next = await page.evaluate("""() => {
                const btn = document.querySelector('.page-next');
                if (!btn || btn.classList.contains('page-next-disabled')) return false;
                btn.click();
                return true;
            }""")
            if not can_next:
                break
            await page.wait_for_timeout(2500)
        seen_c = set()
        unique_contents = []
        for c in all_contents:
            if c not in seen_c:
                seen_c.add(c)
                unique_contents.append(c)

        # ── 4. 渠道二维码页面 ──
        print(f"  打开: {PROMOTION_CODE_URL}")
        await page.goto(PROMOTION_CODE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        channel_text = await page.evaluate("() => document.body.innerText")

        # 提取渠道明细（逐页）
        all_channels = []
        for i in range(30):
            rows = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('ul li')).filter(li => {
                    return /20\\d{2}\\//.test(li.innerText) && li.innerText.includes('\\n')
                        && (li.innerText.includes('下载二维码') || li.innerText.includes('导出数据'));
                }).map(li => li.innerText.trim());
            }""")
            if not rows:
                break
            all_channels.extend(rows)
            can_next = await page.evaluate("""() => {
                const btn = document.querySelector('.page-next');
                if (!btn || btn.classList.contains('page-next-disabled')) return false;
                btn.click();
                return true;
            }""")
            if not can_next:
                break
            await page.wait_for_timeout(2500)
        seen_ch = set()
        unique_channels = []
        for ch in all_channels:
            if ch not in seen_ch:
                seen_ch.add(ch)
                unique_channels.append(ch)

        # ── 5. 推广数据页面（获取优惠券数据） ──
        print(f"  打开: {PROMOTION_DATA_URL}")
        await page.goto(PROMOTION_DATA_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        promotion_text = await page.evaluate("() => document.body.innerText")

        # 刷新 cookies
        await context.storage_state(path=STORAGE_STATE_PATH)
        await browser.close()

    return {
        "income_text": income_text,
        "transactions": unique_transactions,
        "member_active_text": member_active_text,
        "members": unique_members,
        "content_active_text": content_active_text,
        "contents": unique_contents,
        "channel_text": channel_text,
        "channels": unique_channels,
        "promotion_text": promotion_text,
    }


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

    # ── 数据重置期恢复：如果值为0但昨日变化为负数，用负数绝对值恢复 ──
    # 总成员数: "总成员数\n0\n昨日加入成员 -164" → 恢复为164
    if record.get("总成员数") == "0":
        m = re.search(r"数据概览.*?总成员数\s*\n\s*0\s*\n\s*昨日加入成员\s+(-\d+)", text, re.DOTALL)
        if m:
            record["总成员数"] = str(abs(int(m.group(1))))
            print(f"  [恢复] 总成员数: 0 → {record['总成员数']}（从负昨日值恢复）")

    # 付费加入成员: "付费加入成员\n0\n昨日加入成员 -52" → 恢复为52
    if record.get("付费加入成员") == "0":
        m = re.search(r"付费加入成员\s*\n\s*0\s*\n\s*昨日加入成员\s+(-\d+)", text)
        if m:
            record["付费加入成员"] = str(abs(int(m.group(1))))
            print(f"  [恢复] 付费加入成员: 0 → {record['付费加入成员']}（从负昨日值恢复）")

    # 提取昨日收入（第一个出现的）
    m = re.search(r"累积收入.*?昨日收入\s+([\d.]+)", text, re.DOTALL)
    if m:
        record["昨日收入(元)"] = m.group(1)
    # 昨日加入成员：提取正值（非负数）
    m = re.search(r"数据概览.*?总成员数.*?昨日加入成员\s+(\d+)", text, re.DOTALL)
    if m:
        record["昨日加入成员"] = m.group(1)
    else:
        # 重置期显示负数时，昨日加入成员设为0
        record["昨日加入成员"] = "0"

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


def parse_member_active(text):
    """从成员活跃页面文本解析概览数据"""
    record = {}
    patterns = {
        "免费加入成员": r"免费加入成员\s*\n\s*(\d+)",
        "退出成员": r"退出成员\s*\n\s*(\d+)",
        "月活跃成员": r"(\d+)\s*\n\s*月活跃成员",
        "未过期活跃成员": r"未过期的活跃成员\s*\n\s*(\d+)",
        "已过期活跃成员": r"已过期的活跃成员\s*\n\s*(\d+)",
        "新成员月流失率": r"([\d.]+%)\s*\n\s*新成员月流失率",
        "近30日新加入成员": r"近30日新加入成员\s*\n\s*(\d+)",
        "新成员月留存数": r"新成员月留存数\s*\n\s*(\d+)",
        "新成员月流失数": r"新成员月流失数\s*\n\s*(\d+)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            record[field] = m.group(1)

    # 数据重置期恢复：免费加入成员和退出成员为0时，从负昨日值恢复
    if record.get("免费加入成员") == "0":
        m = re.search(r"免费加入成员\s*\n\s*0\s*\n\s*昨日加入成员\s+(-\d+)", text)
        if m:
            record["免费加入成员"] = str(abs(int(m.group(1))))
            print(f"  [恢复] 成员-免费加入成员: 0 → {record['免费加入成员']}")
    if record.get("退出成员") == "0":
        m = re.search(r"退出成员\s*\n\s*0\s*\n\s*昨日退出成员\s+(-\d+)", text)
        if m:
            record["退出成员"] = str(abs(int(m.group(1))))
            print(f"  [恢复] 成员-退出成员: 0 → {record['退出成员']}")

    return record


def parse_member_detail(raw_rows):
    """解析成员明细文本行"""
    records = []
    for row in raw_rows:
        parts = [p.strip() for p in row.split("\n") if p.strip()]
        # 典型格式: 昵称 [手机号/微信号] 编号 加入时间 最后活跃 到期时间 已续期数 主题数
        # 有些成员有手机号/微信号，有些没有，需要灵活处理
        # 找到日期字段的位置
        date_indices = [i for i, p in enumerate(parts) if re.match(r'\d{4}/\d{2}/\d{2}', p)]
        if len(date_indices) >= 2:
            first_date_idx = date_indices[0]
            record = {
                "用户昵称": parts[0],
                "成员编号": parts[first_date_idx - 1] if first_date_idx > 1 else "",
                "首次加入时间": parts[first_date_idx] if len(date_indices) >= 1 else "",
                "最后活跃时间": parts[first_date_idx + 1] if first_date_idx + 1 < len(parts) else "",
                "到期时间": parts[first_date_idx + 2] if first_date_idx + 2 < len(parts) else "",
            }
            # 续期数和主题数在到期时间之后
            remaining = parts[first_date_idx + 3:]
            if len(remaining) >= 2:
                record["已续期数"] = remaining[0]
                record["主题数"] = remaining[1]
            records.append(record)
    return records


def parse_content_active(text):
    """从内容活跃页面文本解析概览数据"""
    record = {}
    patterns = {
        "主题数": r"主题数\s*\n\s*(\d+)",
        "文件数": r"文件数\s*\n\s*(\d+)",
        "图片数": r"图片数\s*\n\s*(\d+)",
        "评论数": r"评论数\s*\n\s*(\d+)",
        "点赞数": r"点赞数\s*\n\s*(\d+)",
        "昨日新增主题": r"昨日新增主题\s+(\d+)",
        "昨日新增文件": r"昨日新增文件\s+(\d+)",
        "昨日新增图片": r"昨日新增图片\s+(\d+)",
        "昨日新增评论": r"昨日新增评论\s+(\d+)",
        "昨日新增点赞": r"昨日新增点赞\s+(\d+)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            record[field] = m.group(1)
    return record


def parse_content_detail(raw_rows):
    """解析内容明细文本行"""
    records = []
    for row in raw_rows:
        parts = [p.strip() for p in row.split("\n") if p.strip()]
        # 格式: 主题标题 发布时间 用户昵称 点赞数 评论数 阅读数
        date_indices = [i for i, p in enumerate(parts) if re.match(r'\d{4}/\d{2}/\d{2}', p)]
        if date_indices:
            di = date_indices[0]
            record = {
                "主题": parts[0] if di > 0 else "",
                "发布时间": parts[di],
                "用户昵称": parts[di + 1] if di + 1 < len(parts) else "",
            }
            remaining = parts[di + 2:]
            if len(remaining) >= 3:
                record["点赞数"] = remaining[0]
                record["评论数"] = remaining[1]
                record["阅读数"] = remaining[2]
            records.append(record)
    return records


def parse_channel_overview(text):
    """从渠道二维码页面文本解析概览数据"""
    record = {}
    patterns = {
        "总渠道数": r"总渠道数\s*\n\s*(\d+)",
        "渠道总访问次数": r"渠道总访问次数\s*\n\s*([\d,]+)",
        "渠道昨日新增访问": r"渠道总访问次数\s*\n\s*[\d,]+\s*\n\s*昨日新增访问\s+(\d+)",
        "渠道总加入人数": r"渠道总加入人数\s*\n\s*(\d+)",
        "渠道昨日新增加入": r"渠道总加入人数\s*\n\s*\d+\s*\n\s*昨日新增人数\s+(\d+)",
        "渠道总付费人数": r"渠道总付费人数\s*\n\s*(\d+)",
        "渠道昨日新增付费": r"渠道总付费人数\s*\n\s*\d+\s*\n\s*昨日新增人数\s+(\d+)",
        "渠道总收入(元)": r"渠道总收入\(元\)\s*\n\s*([\d,.]+)",
        "渠道昨日新增收入(元)": r"渠道总收入.*?昨日新增收入\s+([\d,.]+)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text, re.DOTALL)
        if m:
            record[field] = m.group(1).replace(",", "")
    return record


def parse_channel_detail(raw_rows):
    """解析渠道明细文本行"""
    records = []
    for row in raw_rows:
        parts = [p.strip() for p in row.split("\n") if p.strip()]
        # 过滤掉操作按钮文字
        parts = [p for p in parts if p not in ("下载二维码", "复制链接", "导出数据")]
        # 格式: 渠道名 生成时间 访问人数 加入人数/付费人数 收入(元)
        date_indices = [i for i, p in enumerate(parts) if re.match(r'\d{4}/\d{2}/\d{2}', p)]
        if date_indices:
            di = date_indices[0]
            channel_name = parts[0] if di > 0 else ""
            record = {
                "渠道名": channel_name,
                "生成时间": parts[di],
                "访问人数": parts[di + 1] if di + 1 < len(parts) else "0",
            }
            # 加入人数/付费人数
            if di + 2 < len(parts):
                join_pay = parts[di + 2]
                m = re.match(r'(\d+)\s*/\s*(\d+)', join_pay)
                if m:
                    record["加入人数"] = m.group(1)
                    record["付费人数"] = m.group(2)
                else:
                    record["加入人数/付费人数"] = join_pay
            # 收入
            if di + 3 < len(parts):
                record["收入(元)"] = parts[di + 3]
            records.append(record)
    return records


def parse_promotion_overview(text):
    """从推广数据页面解析额外概览数据"""
    record = {}
    patterns = {
        "本月付费加入成员": r"本月付费加入成员\s*\n\s*(\d+)",
        "上月加入成员": r"上月加入成员\s+(\d+)",
        "成员拉新人数": r"成员拉新人数\s*\n\s*(\d+)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            record[field] = m.group(1)
    return record


def parse_coupon_data(text):
    """从推广数据页面文本解析优惠券数据"""
    records = []
    # 找到优惠券数据区域
    coupon_section = re.search(r'优惠券数据.*?类型名称面额.*?\n(.*?)(?:拉新数据报表|$)', text, re.DOTALL)
    if not coupon_section:
        return records
    section_text = coupon_section.group(1)
    # 每个优惠券记录以 "新人券" 或 "续期券" 开头
    coupon_blocks = re.split(r'\n(?=新人券|续期券)', section_text)
    for block in coupon_blocks:
        block = block.strip()
        if not block:
            continue
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) < 5:
            continue
        # 过滤操作按钮
        lines = [l for l in lines if l not in ("生成海报", "生成链接", "停止", "生成海报生成链接停止")]
        record = {"类型": lines[0] if lines else ""}
        # 名称是第二行
        if len(lines) > 1:
            record["名称"] = lines[1]
        # 面额
        for l in lines:
            m = re.match(r'^(\d+)$', l)
            if m and "面额(元)" not in record:
                record["面额(元)"] = m.group(1)
                break
        # 有效期：起/止
        starts = [l for l in lines if l.startswith("起 ")]
        ends = [l for l in lines if l.startswith("止 ")]
        if starts:
            record["有效期起"] = starts[0].replace("起 ", "")
        if ends:
            record["有效期止"] = ends[0].replace("止 ", "")
        # 总数/已用
        for l in lines:
            m = re.match(r'(\d+)/(\d+)', l)
            if m:
                record["总数"] = m.group(1)
                record["已用"] = m.group(2)
                break
        # 状态
        for status in ("进行中", "已过期", "已停止"):
            if status in lines:
                record["状态"] = status
                break
        # 访问数 (单独的数字行, 在总数/已用之后)
        found_total = False
        for l in lines:
            if re.match(r'\d+/\d+', l):
                found_total = True
                continue
            if found_total and re.match(r'^\d+$', l):
                record["访问数"] = l
                break

        if record.get("名称"):
            records.append(record)
    return records


# ──────────────────────────── 主流程 ────────────────────────────


async def main():
    now = datetime.now(BJT)
    print("=" * 60)
    print(f"知识星球 → 飞书 每日数据同步")
    print(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 60)

    # 1. 爬取
    print("\n[1/4] 爬取知识星球后台...")
    data = await scrape_zsxq()
    print(f"  收入页面: {len(data['income_text'])} 字符")
    print(f"  交易明细: {len(data['transactions'])} 条")
    print(f"  成员活跃页面: {len(data['member_active_text'])} 字符")
    print(f"  成员明细: {len(data['members'])} 条")
    print(f"  内容活跃页面: {len(data['content_active_text'])} 字符")
    print(f"  内容明细: {len(data['contents'])} 条")
    print(f"  渠道页面: {len(data['channel_text'])} 字符")
    print(f"  渠道明细: {len(data['channels'])} 条")
    print(f"  推广页面: {len(data['promotion_text'])} 字符")

    # 2. 解析收入概览 + 推广转化
    print("\n[2/4] 解析数据...")
    overview = parse_overview(data["income_text"])
    transactions = parse_transactions(data["transactions"])

    # 3. 解析成员活跃
    member_overview = parse_member_active(data["member_active_text"])
    overview.update({f"成员-{k}": v for k, v in member_overview.items()})
    member_details = parse_member_detail(data["members"])

    # 4. 解析内容活跃
    content_overview = parse_content_active(data["content_active_text"])
    overview.update({f"内容-{k}": v for k, v in content_overview.items()})
    content_details = parse_content_detail(data["contents"])

    # 5. 解析渠道数据
    channel_overview = parse_channel_overview(data["channel_text"])
    overview.update({f"渠道-{k}": v for k, v in channel_overview.items()})
    channel_details = parse_channel_detail(data["channels"])

    # 6. 解析推广数据（额外概览 + 优惠券）
    promo_overview = parse_promotion_overview(data["promotion_text"])
    overview.update({f"推广-{k}": v for k, v in promo_overview.items()})
    coupon_details = parse_coupon_data(data["promotion_text"])

    print(f"  概览字段: {len(overview)} 个")
    for k, v in overview.items():
        print(f"    {k}: {v}")
    print(f"  交易记录: {len(transactions)} 条")
    print(f"  成员记录: {len(member_details)} 条")
    print(f"  内容记录: {len(content_details)} 条")
    print(f"  渠道记录: {len(channel_details)} 条")
    print(f"  优惠券记录: {len(coupon_details)} 条")

    # 3. 写入飞书
    print("\n[3/4] 写入飞书多维表格...")
    token = get_tenant_token()

    # 写概览（收入 + 推广转化 + 成员活跃 + 内容活跃）
    ensure_fields(token, FEISHU_TABLE_ID, overview.keys())
    print("  写入概览数据...")
    add_records(token, FEISHU_TABLE_ID, [overview])

    # 写收入明细
    if transactions:
        detail_table_id = find_or_create_detail_table(token)
        if detail_table_id:
            print(f"  写入 {len(transactions)} 条交易明细...")
            for i in range(0, len(transactions), 500):
                add_records(token, detail_table_id, transactions[i:i+500])

    # 写成员明细
    if member_details:
        member_table_id = find_or_create_table(
            token, "成员活跃明细",
            ["用户昵称", "成员编号", "首次加入时间", "最后活跃时间", "到期时间", "已续期数", "主题数"]
        )
        if member_table_id:
            ensure_fields(token, member_table_id, member_details[0].keys())
            print(f"  写入 {len(member_details)} 条成员明细...")
            for i in range(0, len(member_details), 500):
                add_records(token, member_table_id, member_details[i:i+500])

    # 写内容明细
    if content_details:
        content_table_id = find_or_create_table(
            token, "内容活跃明细",
            ["主题", "发布时间", "用户昵称", "点赞数", "评论数", "阅读数"]
        )
        if content_table_id:
            ensure_fields(token, content_table_id, content_details[0].keys())
            print(f"  写入 {len(content_details)} 条内容明细...")
            for i in range(0, len(content_details), 500):
                add_records(token, content_table_id, content_details[i:i+500])

    # 写渠道明细
    if channel_details:
        channel_table_id = find_or_create_table(
            token, "获客渠道明细",
            ["渠道名", "生成时间", "访问人数", "加入人数", "付费人数", "收入(元)"]
        )
        if channel_table_id:
            ensure_fields(token, channel_table_id, channel_details[0].keys())
            print(f"  写入 {len(channel_details)} 条渠道明细...")
            for i in range(0, len(channel_details), 500):
                add_records(token, channel_table_id, channel_details[i:i+500])

    # 写优惠券数据
    if coupon_details:
        coupon_table_id = find_or_create_table(
            token, "优惠券数据",
            ["类型", "名称", "面额(元)", "有效期起", "有效期止", "总数", "已用", "访问数", "状态"]
        )
        if coupon_table_id:
            ensure_fields(token, coupon_table_id, coupon_details[0].keys())
            print(f"  写入 {len(coupon_details)} 条优惠券数据...")
            for i in range(0, len(coupon_details), 500):
                add_records(token, coupon_table_id, coupon_details[i:i+500])

    print("\n" + "=" * 60)
    print("同步完成！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
