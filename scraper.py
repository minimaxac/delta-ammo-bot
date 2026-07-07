# -*- coding: utf-8 -*-
"""
三角洲子弹导购小助手 v4.0 — GitHub Actions 版
=============================================
数据源: orzice.com (SSR HTML, 与游戏交易行一致)
预测: 仅用自采 CSV 数据, 去尾淘汰 30%, 不用任何外部参考
采集: 热时段 11-23 每5分, 冷时段 23-11 每10分, 全部 +1 分错峰
推送: 每日 22:00 Server酱 → 微信

用法: python scraper.py [--push]
"""
import requests, csv, os, sys, re
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ============================================
# 配置
# ============================================
SENDKEY = os.environ.get("SENDKEY", "")
CSV_FILE = "ammo_prices.csv"
MIN_ITEMS = 35  # 过滤赛季子弹后约40-45颗常规弹
MAX_PRICE_DEV = 0.50
TZ_CN = timezone(timedelta(hours=8))
TRIM_RATE = 0.30       # 去尾淘汰 30%
MIN_DAYS_PREDICT = 14  # 至少 14 天数据才做买入预测
FEE = {3: 0.85, 4: 0.85, 5: 0.88}
GRADE_CN = {3: "3级弹", 4: "4级弹", 5: "5级弹"}
WEEKDAY_CN = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}

# ============================================
# 赛季限定子弹黑名单 (S7-S10 全部 + 过期变种)
# ============================================
BLACK_METHOD = {"特勤处回收"}

SEASON_NAMES = {
    # S10 赛季限定
    "9x19mm CT", ".45 ACP CT",
    "6.8x51mm PLY-I", "6.8x51mm PLY-II", "6.8x51mm PLY-III",
    "5.8x42mm DBP10+P", "5.8x42mm DVC12+P",
    "9x39mm PAB-7", "9x39mm PAB-9",
    # 过期赛季限定变种
    "5.56x45mm M855A1 APC+", "5.45x39mm BS ST+",
}

# ============================================
# 等级推测 (副源用, 主源从 HTML data-grade 读)
# ============================================
def guess_grade(name):
    if "_5" in name: return 5
    if "_4" in name: return 4
    if "_3" in name: return 3
    if "AP-20" in name: return 4
    for kw in ["M62","M995","SS190","Hybrid","DVC12","FTX","穿甲箭","PLY-III","AP SX","PS12B"]:
        if kw in name: return 5
    for kw in ["M80","M855A1","SS193","DBP10","FMJ SX","LPS","SP6","PBP","刺骨箭","龙息弹","PS12 ","PD12"]:
        if kw in name: return 4
    for kw in ["PLY-II","PAB-9"]:
        if kw in name: return 4
    for kw in ["PLY-I","PAB-7"]:
        if kw in name: return 3
    return 3

# ============================================
# 采集: orzice (主源, SSR HTML)
# ============================================
def scrape_orice():
    items = []
    for page in range(1, 8):
        try:
            url = f"https://orzice.com/v/ammo?p={page}"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for row in soup.select("tr.table-row"):
                name_el = row.select_one(".item-name")
                method_el = row.select_one("[class*='ShopSellType']")
                price_el = row.select_one(".icon-gold")
                img_el = row.select_one("img[data-grade]")

                if not name_el or not price_el:
                    continue
                name = name_el.text.strip()
                method = method_el.text.strip() if method_el else ""
                try:
                    price = int(price_el.text.strip().replace(",", ""))
                except ValueError:
                    continue
                if price <= 0:
                    continue

                grade = 0
                if img_el and img_el.get("data-grade"):
                    try: grade = int(img_el["data-grade"])
                    except ValueError: pass

                # 过滤
                if method in BLACK_METHOD: continue
                if name in SEASON_NAMES: continue
                if grade > 0 and grade not in (3, 4, 5): continue

                items.append({
                    "name": name,
                    "price": price,
                    "grade": grade if grade > 0 else guess_grade(name),
                    "source": "orzice"
                })
        except Exception as e:
            print(f"  orzice p{page}: {e}")
    return items

# ============================================
# 采集: deltaforcetools (副源, SSR 表格)
# ============================================
def scrape_deltaforce():
    items = []
    for page in range(1, 10):
        try:
            url = "https://deltaforcetools.gg/auction-house/ammo"
            if page > 1: url += f"?page={page}"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("table tbody tr") or soup.select("tr")
            page_items = 0
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3: continue
                name = cols[1].get_text(strip=True) if len(cols) > 1 else ""
                price_text = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                if not name or not price_text: continue
                try: price = int(float(price_text.replace(",","").replace("$","")))
                except ValueError: continue
                if price <= 0: continue
                if name in SEASON_NAMES: continue

                grade = guess_grade(name)
                if grade not in (3, 4, 5): continue
                items.append({"name": name, "price": price, "grade": grade, "source": "deltaforce"})
                page_items += 1

            if page_items == 0: break
        except Exception as e:
            print(f"  deltaforce p{page}: {e}")
            break
    return items

# ============================================
# 采集入口: 双源 + 验证 + 切换
# ============================================
def fetch_prices():
    now = datetime.now(TZ_CN)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    collected = []

    print("[采集] 主源 orzice...")
    items1 = scrape_orice()
    ok, reason = validate_source(items1, yesterday)
    if ok:
        collected = items1
        print(f"  ✅ orzice: {len(items1)} 种子弹")
    else:
        print(f"  ❌ orzice: {reason}")
        print("[采集] 副源 deltaforcetools...")
        items2 = scrape_deltaforce()
        ok, reason = validate_source(items2, yesterday)
        if ok:
            collected = items2
            print(f"  ⚠️ deltaforce: {len(items2)} 种")
        else:
            print(f"  ❌ deltaforce: {reason}")
            fallback = load_fallback()
            if fallback:
                collected = fallback
                print(f"  🆘 CSV快照: {len(collected)} 种")
            else:
                print("  💀 全部失效")
                return {"timestamp": now.strftime("%Y-%m-%d %H:%M"), "total": 0, "items": []}

    return {"timestamp": now.strftime("%Y-%m-%d %H:%M"), "total": len(collected), "items": collected}

def validate_source(items, yesterday_ref):
    if not items or len(items) < MIN_ITEMS:
        return False, f"数量不足({len(items)}<{MIN_ITEMS})"
    zero_count = sum(1 for it in items if it["price"] <= 0)
    if zero_count > len(items) * 0.2:
        return False, f"空值过多({zero_count}/{len(items)})"
    # 抽查 5 颗与昨日同比
    check = ["7.62x39mm PS", "5.45x39mm PS", "9x19mm AP6.3", ".45 ACP FMJ", "12 Gauge独头 AP-20"]
    suspicious = 0
    for name in check:
        found = [it for it in items if it["name"] == name]
        if not found: continue
        today_p = found[0]["price"]
        yesterday_p = get_yesterday_price(name, yesterday_ref)
        if yesterday_p and yesterday_p > 0:
            dev = abs(today_p - yesterday_p) / max(today_p, yesterday_p)
            if dev > MAX_PRICE_DEV: suspicious += 1
    if suspicious >= 3: return False, f"价格偏差过大({suspicious})"
    return True, "ok"

def get_yesterday_price(name, ref_time):
    if not os.path.exists(CSV_FILE): return None
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            for row in reversed(rows):
                ts = row.get("timestamp", "")
                if ref_time[:10] in ts:
                    v = row.get(name, "")
                    try: return int(float(v))
                    except: pass
    except: pass
    return None

def load_fallback():
    if not os.path.exists(CSV_FILE): return None
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            if not rows: return None
        last = rows[-1]
        items = []
        for name, price_str in last.items():
            if name == "timestamp": continue
            try:
                p = int(float(price_str))
                if p > 0: items.append({"name": name, "price": p, "grade": guess_grade(name), "source": "fallback"})
            except: pass
        return items if len(items) >= MIN_ITEMS else None
    except: return None

# ============================================
# 存储: CSV 时序
# ============================================
def save_csv(items, timestamp):
    row = {"timestamp": timestamp}
    for it in items:
        row[it["name"]] = it["price"]

    existing = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    all_cols = set()
    if existing: all_cols.update(existing[0].keys())
    all_cols.update(row.keys())
    cols = ["timestamp"] + sorted(c for c in all_cols if c != "timestamp")

    existing.append(row)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in existing:
            writer.writerow(r)
    print(f"  CSV: {len(existing)} 行")

# ============================================
# 分析: 纯自采数据预测引擎
# ============================================
def load_all_prices():
    """加载CSV中所有子弹的时序价格, 返回 {name: [(datetime, price), ...]}"""
    if not os.path.exists(CSV_FILE):
        return {}
    data = defaultdict(list)
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("timestamp", "")
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            for name, price_str in row.items():
                if name == "timestamp": continue
                if not price_str: continue
                try:
                    p = int(float(price_str))
                    if p > 0:
                        data[name].append((dt, p))
                except (ValueError, TypeError):
                    pass

    # 按时间排序
    for name in data:
        data[name].sort(key=lambda x: x[0])
    return data

def get_period_prices(history, current_dt, days=14):
    """从历史数据中取最近 N 天的价格, 按工作日/周末分类"""
    cutoff = current_dt - timedelta(days=days)
    weekday_prices = []
    weekend_prices = []

    for dt, price in history:
        if dt >= cutoff:
            # Python weekday(): 0=Mon .. 4=Fri, 5=Sat, 6=Sun
            if dt.weekday() >= 5:
                weekend_prices.append(price)
            else:
                weekday_prices.append(price)

    return weekday_prices, weekend_prices

def trim_tail_avg(prices):
    """去尾淘汰法: 弃最低 TRIM_RATE% 条数据, 剩余取平均"""
    if not prices:
        return None
    sorted_p = sorted(prices)
    trim_n = max(1, int(len(sorted_p) * TRIM_RATE))
    kept = sorted_p[trim_n:]
    return round(sum(kept) / len(kept)) if kept else None

def analyze(items, now=None):
    """买入建议分析 — 仅用自采CSV数据"""
    if now is None:
        now = datetime.now(TZ_CN)

    all_history = load_all_prices()
    total_days = estimate_data_days(all_history)

    buys = []
    for it in items:
        name = it["name"]
        grade = it.get("grade", 3)
        price = it["price"]
        history = all_history.get(name, [])

        if not history or total_days < 3:
            # 数据太少, 跳过
            continue

        weekday_p, weekend_p = get_period_prices(history, now, days=14)

        # 周末均价 (去尾淘汰)
        weekend_avg = trim_tail_avg(weekend_p) if weekend_p else None

        # 工作日均价
        weekday_avg = round(sum(weekday_p) / len(weekday_p)) if weekday_p else price

        # 周内最低参考
        week_low = min(weekday_p) if weekday_p else price

        # 预期利润
        if weekend_avg and weekend_avg > 0:
            predicted = weekend_avg
        elif weekday_avg:
            # 没有周末数据时, 不做预测
            predicted = None
        else:
            predicted = None

        gain_pct = round((price - week_low) / week_low * 100, 1) if week_low > 0 else 0

        if predicted:
            profit = round(predicted * FEE.get(grade, 0.85) - price)

            # 只在利润 > 0 时加入买入建议
            if profit > 0:
                buys.append({
                    "name": name, "grade": grade, "price": price,
                    "predicted": predicted, "profit": profit,
                    "gain_pct": gain_pct, "week_low": week_low,
                })
        else:
            # 数据不足, 只记录行情
            buys.append({
                "name": name, "grade": grade, "price": price,
                "predicted": 0, "profit": 0,
                "gain_pct": gain_pct, "week_low": week_low,
            })

    buys.sort(key=lambda x: x["profit"], reverse=True)
    return buys, total_days

def estimate_data_days(all_history):
    """估算数据覆盖了多少天"""
    all_times = set()
    for history in all_history.values():
        for dt, _ in history:
            all_times.add(dt.date())
    return max(1, len(all_times))

# ============================================
# 推送: Server酱微信日报 v4.1 视觉增强版
# ============================================
GRADE_ICON = {3: "🔵", 4: "🟣", 5: "🟡"}
PROFIT_ICONS = {">500": "🔥", ">200": "💰", ">0": "📈"}
BAR = "▔" * 14

def short_name(name, max_len=20):
    """截断长子弹名适配手机窄屏"""
    if len(name) <= max_len:
        return name
    return name[:max_len-2] + ".."

def push_report(buys, total, timestamp, total_days):
    if not SENDKEY:
        print("[推送] 跳过, 未设置 SENDKEY")
        return

    now = datetime.now(TZ_CN)
    wd = WEEKDAY_CN.get(now.weekday(), "")
    date_str = now.strftime("%m/%d")

    profit_buys = [b for b in buys if b["profit"] > 0]
    profit_buys.sort(key=lambda x: x["profit"], reverse=True)

    ready = total_days >= MIN_DAYS_PREDICT

    # ─── 日报头 ───
    header = [
        f"**🔥 三角洲子弹导购 · {date_str} {wd}**",
        "",
        f"📅 {date_str} {wd}  ⏰ {timestamp}",
        f"🎯 常规子弹 {total} 种",
        f"📈 数据 {total_days}/{MIN_DAYS_PREDICT} 天"
        f"{' ✅' if ready else ' ⏳'}",
        "",
        BAR,
        "",
    ]

    # ─── 日内行情摘要 ───
    if buys:
        prices = [b["price"] for b in buys]
        avg_p = sum(prices) // len(prices)
        max_b = max(buys, key=lambda x: x["price"])
        min_b = min(buys, key=lambda x: x["price"])
        up_n = sum(1 for b in buys if b["gain_pct"] > 0)
        down_n = sum(1 for b in buys if b["gain_pct"] < 0)

        summary = [
            f"📊 **行情摘要**",
            f"均价 {avg_p}  |  📈{up_n}涨 📉{down_n}跌",
            f"最高 {max_b['name']} {max_b['price']}",
            f"最低 {min_b['name']} {min_b['price']}",
            "",
        ]
    else:
        summary = ["📊 暂无数据", ""]

    # ─── 主体表格 ───
    if ready and profit_buys:
        # 🎯 完整预测模式
        table_header = [
            f"🎯 **买入建议 Top 10**",
            "",
            "| # | 子弹 | 买入 | 预估 | 利润 |",
            "|---|------|------|------|------|",
        ]
        table_rows = []
        for i, b in enumerate(profit_buys[:10], 1):
            g_icon = GRADE_ICON.get(b["grade"], "⚪")
            sname = short_name(b["name"])
            profit_icon = "🔥" if b["profit"] > 300 else ("💰" if b["profit"] > 100 else "📈")
            table_rows.append(
                f"| {i} | {g_icon}{sname} | {b['price']} | "
                f"{b['predicted']} | {profit_icon}+{b['profit']} |"
            )
        body_lines = header + summary + table_header + table_rows + [""]

    elif buys:
        # 📊 行情速览模式 (数据不足)
        trend_buys = sorted(buys, key=lambda x: abs(x["gain_pct"]), reverse=True)

        table_header = [
            f"📋 **行情速览 Top 10**" if not ready else f"📋 **日内波动 Top 10**",
            f"⚠️ 数据积累中, 预测功能待 {MIN_DAYS_PREDICT - total_days} 天后激活",
            "",
            "| # | 子弹 | 当前 | 周低 | 涨跌 |",
            "|---|------|------|------|------|",
        ]
        table_rows = []
        for i, b in enumerate(trend_buys[:10], 1):
            g_icon = GRADE_ICON.get(b["grade"], "⚪")
            sname = short_name(b["name"])
            if b["gain_pct"] > 5: arrow = "🔺"
            elif b["gain_pct"] > 0: arrow = "📈"
            elif b["gain_pct"] < -5: arrow = "🔻"
            elif b["gain_pct"] < 0: arrow = "📉"
            else: arrow = "➖"
            gain = f"{arrow}{b['gain_pct']:+.1f}%"
            table_rows.append(
                f"| {i} | {g_icon}{sname} | {b['price']} | "
                f"{b['week_low']} | {gain} |"
            )
        body_lines = header + summary + table_header + table_rows + [""]

    else:
        body_lines = header + summary + ["暂无数据", ""]

    # ─── 页脚 ───
    day_tip = {
        0: "💡 周一低谷, 关注跌幅最大 Top 5", 1: "💡 周二筑底, 逢低关注",
        2: "💡 周三启动, 关注涨价趋势", 3: "💡 周四加速, 谨慎追高",
        4: "💡 周五已近峰值, 注意止盈", 5: "💡 周六高位, 可考虑出货",
        6: "💡 周日顶峰, 等待下周回调",
    }.get(now.weekday(), "")

    footer = [
        BAR,
        f"{day_tip}",
        f"📡 数据源 orzice  ⚙️ v4.1  🕐 下次推送 明晚22:00",
    ]

    lines = body_lines + footer
    body = {"title": f"🔥 三角洲导购 {date_str} {wd}", "desp": "\n".join(lines)}

    try:
        url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
        resp = requests.post(url, data=body, timeout=10)
        print(f"[推送] 成功: {resp.text[:100]}")
    except Exception as e:
        print(f"[推送] 失败: {e}")

# ============================================
# 入口
# ============================================
def main():
    force_push = "--push" in sys.argv

    print("=" * 50)
    print("Delta Ammo Bot v4.0 (GitHub Actions)")
    print(f"启动: {datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    data = fetch_prices()
    if data["total"] == 0:
        print("[失败] 无数据")
        sys.exit(1)

    items = data["items"]
    ts = data["timestamp"]
    print(f"  有效: {len(items)} 种子弹")

    print("[存储] CSV...")
    save_csv(items, ts)

    print("[分析] 预测引擎...")
    now = datetime.now(TZ_CN)
    buys, total_days = analyze(items, now)
    profit_buys = [b for b in buys if b["profit"] > 0]
    print(f"  数据覆盖: {total_days} 天 | 买入建议: {len(profit_buys)} 种")

    hour = now.hour
    if force_push or hour == 22:
        print("[推送] 日报...")
        push_report(buys, len(items), ts, total_days)
    else:
        print(f"[跳过] 非推送时段 ({hour}时)")

    for b in (profit_buys[:5] if profit_buys else buys[:5]):
        print(f"  {GRADE_CN.get(b['grade'])} {b['name']}: "
              f"买入{b['price']} 利润+{b['profit']} 已涨{b['gain_pct']:+.1f}%")

    print("=" * 50)
    print("完成")

if __name__ == "__main__":
    main()
