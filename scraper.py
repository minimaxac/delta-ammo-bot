# -*- coding: utf-8 -*-
"""
三角洲子弹导购小助手 v3.0 — GitHub Actions 版
=============================================
功能:
  1. 双源采集 (orzice 主 + deltaforcetools 副)
  2. 源切换验证 (存活检测 + 历史偏差对比)
  3. 赛季子弹过滤 + 分层预测引擎
  4. CSV 时序存储 + COS 备份
  5. 每日22:00 Server酱微信推送

用法: python scraper.py [--push]
  --push  强制推送日报 (测试用, 否则只在22:00推送)
"""
import requests, csv, json, os, re, sys
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ============================================
# 配置
# ============================================
SENDKEY = os.environ.get("SENDKEY", "")
CSV_FILE = "ammo_prices.csv"
MIN_ITEMS = 45          # 最少子弹数, 少于说明源挂了
MAX_PRICE_DEV = 0.50   # 与昨天同比最大偏差, 超50%判可疑
TZ_CN = timezone(timedelta(hours=8))

# 赛季子弹黑名单
BLACK_METHOD = {"特勤处回收"}
BLACK_IDS = {1281, 1292}
TARGET_GRADES = {3, 4, 5}

# 预测参数
FEE = {3: 0.85, 4: 0.85, 5: 0.88}
PROFIT_MIN = 50
GRADE_CN = {3: "3级弹", 4: "4级弹", 5: "5级弹"}
WEEKDAY_CN = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}

def guess_grade(name):
    """推测子弹等级 (副源用, 主源从 HTML data-grade 读)"""
    if "_5" in name: return 5
    if "_4" in name: return 4
    if "_3" in name: return 3
    if "AP-20" in name: return 4
    for kw in ["M62","M995","SS190","Hybrid","DVC12","FTX","穿甲箭","PLY-III","AP SX"]:
        if kw in name: return 5
    for kw in ["M80","M855A1","SS193","DBP10","FMJ SX","LPS","SP6","PBP","刺骨箭","龙息弹","PS12 ","PD12"]:
        if kw in name: return 4
    return 3

# ============================================
# orzice 历史参考数据集 (本地扒取, 2026-07-07)
# ============================================
ORZICE_REF = {
    "12.7x55mm PS12A": {"swing":1.27}, "45-70 Govt RN": {"swing":1.05},
    ".300BLK_3": {"swing":1.14}, ".300BLK_4": {"swing":1.12},
    ".300BLK_5": {"swing":1.05}, ".357 Magnum JHP": {"swing":1.38},
    ".45 ACP FMJ": {"swing":1.17}, "12 Gauge 箭形弹": {"swing":1.36},
    "5.45x39mm PS": {"swing":1.38}, "5.56x45mm M855": {"swing":1.12},
    "5.7x28mm L191": {"swing":1.27}, "5.7x28mm R37.F": {"swing":1.24},
    "5.8x42mm DVP88": {"swing":1.23}, "7.62x39mm PS": {"swing":1.32},
    "7.62x51mm BPZ": {"swing":1.20}, "9x19mm AP6.3": {"swing":1.15},
    "9x39mm SP5": {"swing":1.41}, "玻纤柳叶箭矢": {"swing":1.18},
    "12 Gauge 龙息弹": {"swing":1.42}, "12 Gauge独头 AP-20": {"swing":1.39},
    "5.45x39mm BT": {"swing":1.05}, "5.56x45mm M855A1": {"swing":1.06},
    "7.62x39mm BP": {"swing":1.27}, "9x39mm SP6": {"swing":1.07},
    "5.8x42mm DVC12": {"swing":1.05}, "7.62x39mm AP": {"swing":1.05},
    "9x39mm BP": {"swing":1.05}, "6.8x51mm PLY-I": {"swing":1.82},
    "6.8x51mm PLY-III": {"swing":1.10}, "6.8x51mm FMJ": {"swing":1.05},
    "6.8x51mm Hybrid": {"swing":1.05}, "12.7x55mm PD12双头弹": {"swing":1.06},
    "5.7x28mm SS193": {"swing":1.04}, "5.8x42mm DBP10": {"swing":1.16},
    "7.62x54R LPS": {"swing":1.14}, "7.62x51mm M80": {"swing":1.05},
}
GRADE_FALLBACK_SWING = {3: 1.25, 4: 1.15, 5: 1.08}

# ============================================
# 采集模块: orzice (主源)
# ============================================
def scrape_orice():
    """从 orzice.com 采集子弹价格"""
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

                # 提取等级 (来自 data-grade 属性)
                grade = 0
                if img_el and img_el.get("data-grade"):
                    try:
                        grade = int(img_el["data-grade"])
                    except ValueError:
                        pass

                # 如果能识别出等级但不是3/4/5，跳过
                if grade > 0 and grade not in TARGET_GRADES:
                    continue

                # 过滤
                if method in BLACK_METHOD:
                    continue

                items.append({"name": name, "price": price, "grade": grade if grade > 0 else guess_grade(name), "source": "orzice"})

        except Exception as e:
            print(f"  orzice p{page} 失败: {e}")

    return items

# ============================================
# 采集模块: deltaforcetools (副源)
# ============================================
def scrape_deltaforce():
    """从 deltaforcetools.gg 采集子弹价格"""
    items = []
    for page in range(1, 10):
        try:
            url = "https://deltaforcetools.gg/auction-house/ammo"
            if page > 1:
                url += f"?page={page}"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("table tbody tr")
            if not rows:
                rows = soup.select("tr")

            page_items = 0
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                name = cols[1].get_text(strip=True) if len(cols) > 1 else ""
                price_text = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                if not name or not price_text:
                    continue
                try:
                    price = int(float(price_text.replace(",", "").replace("$", "")))
                except ValueError:
                    continue
                if price <= 0:
                    continue

                # deltaforce 的等级通过名称猜测
                grade = guess_grade(name)
                if grade not in TARGET_GRADES:
                    continue

                items.append({"name": name, "price": price, "grade": grade, "source": "deltaforce"})
                page_items += 1

            if page_items == 0:
                break  # 空页，停止

        except Exception as e:
            print(f"  deltaforce p{page} 失败: {e}")
            break

    return items

# ============================================
# 采集入口: 双源 + 验证
# ============================================
def fetch_prices():
    """双源采集, 带切换验证"""
    now = datetime.now(TZ_CN)
    collected = []

    # 先读昨天数据(用于偏差对比)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    # --- 主源 ---
    print("[采集] 主源 orzice...")
    items1 = scrape_orice()
    ok, reason = validate_source(items1, "orzice", yesterday)
    if ok:
        collected = items1
        print(f"  ✅ orzice: {len(items1)} 种子弹, 验证通过")
    else:
        print(f"  ❌ orzice 不可用: {reason}")

    # --- 副源 ---
    if not collected:
        print("[采集] 副源 deltaforcetools...")
        items2 = scrape_deltaforce()
        ok, reason = validate_source(items2, "deltaforce", yesterday)
        if ok:
            collected = items2
            print(f"  ⚠️ 切换到 deltaforce: {len(items2)} 种子弹")
        else:
            print(f"  ❌ deltaforce 也不可用: {reason}")

    # --- 兜底: 读 CSV 最后一行 ---
    if not collected:
        fallback = load_fallback()
        if fallback:
            collected = fallback
            print(f"  🆘 双源失效, 使用 CSV 快照: {len(collected)} 种子弹")
        else:
            print("  💀 全部失效, 无数据可用")

    # 补充等级信息(从名字推测, orzice 没直接给等级)
    for it in collected:
        if "grade" not in it:
            it["grade"] = guess_grade(it["name"])

    # 过滤: 只保留 Lv.3/4/5
    collected = [it for it in collected if it.get("grade", 0) in TARGET_GRADES]

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "total": len(collected),
        "items": collected,
    }

def validate_source(items, source_name, yesterday_ref):
    """源验证: 存活检测 + 偏差对比"""
    if not items or len(items) < MIN_ITEMS:
        return False, f"数量不足({len(items)}<{MIN_ITEMS})"

    # 检查空值比例
    zero_count = sum(1 for it in items if it["price"] <= 0)
    if zero_count > len(items) * 0.2:
        return False, f"空值过多({zero_count}/{len(items)})"

    # 简单历史偏差对比: 抽查5颗常见子弹
    check_names = ["7.62x39mm PS", "5.45x39mm PS", "9x19mm AP6.3",
                   ".45 ACP FMJ", "12 Gauge独头 AP-20"]
    suspicious = 0
    for name in check_names:
        found = [it for it in items if it["name"] == name]
        if not found:
            continue
        today_price = found[0]["price"]

        # 读昨天同时段价格
        yesterday_price = get_yesterday_price(name, yesterday_ref)
        if yesterday_price and yesterday_price > 0:
            dev = abs(today_price - yesterday_price) / max(today_price, yesterday_price)
            if dev > MAX_PRICE_DEV:
                suspicious += 1
                print(f"    ⚠️ {name}: 今天{today_price} vs 昨天~{yesterday_price} 偏差{dev:.0%}")

    if suspicious >= 3:  # 3颗以上异常
        return False, f"价格偏差过大(sus={suspicious}/5)"

    return True, "ok"

def get_yesterday_price(name, ref_time):
    """从 CSV 读昨天同时间约的价格"""
    if not os.path.exists(CSV_FILE):
        return None
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                return None
            # 找最接近的昨天记录
            for row in reversed(rows):
                ts = row.get("timestamp", "")
                if ref_time[:10] in ts:  # 同一天
                    val = row.get(name, "")
                    try:
                        return int(float(val))
                    except (ValueError, TypeError):
                        pass
            return None
    except Exception:
        return None

def load_fallback():
    """从 CSV 读最后一行作为兜底"""
    if not os.path.exists(CSV_FILE):
        return None
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                return None
            last = rows[-1]
            items = []
            for name, price_str in last.items():
                if name == "timestamp":
                    continue
                try:
                    price = int(float(price_str))
                    if price > 0:
                        items.append({"name": name, "price": price, "source": "fallback"})
                except (ValueError, TypeError):
                    pass
            return items if len(items) >= MIN_ITEMS else None
    except Exception:
        return None

# ============================================
# 存储: CSV 时序
# ============================================
def save_csv(items, timestamp):
    """存储到 CSV 时间序列"""
    # 生成新行
    row = {"timestamp": timestamp}
    for it in items:
        row[it["name"]] = it["price"]

    # 读现有 CSV
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing = list(reader)
    else:
        existing = []

    # 收集所有列名
    all_cols = set()
    if existing:
        all_cols.update(existing[0].keys())
    all_cols.update(row.keys())
    cols = ["timestamp"] + sorted(c for c in all_cols if c != "timestamp")

    # 追加并写回
    existing.append(row)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in existing:
            writer.writerow(r)

    print(f"  CSV 已保存: {len(existing)} 行")

# ============================================
# 分析: 预测引擎
# ============================================
def analyze(items):
    """买入建议分析"""
    buys = []
    for it in items:
        name = it["name"]
        grade = it.get("grade", 3)
        price = it["price"]

        # 周末波动比
        ref = ORZICE_REF.get(name, {})
        swing = ref.get("swing", GRADE_FALLBACK_SWING.get(grade, 1.10))
        swing = max(1.05, min(2.0, swing))

        predicted = int(price * swing)
        profit = round(predicted * FEE.get(grade, 0.85) - price)

        # 历史最低参考
        history_prices = get_history_prices(name)
        week_low = min(history_prices) if history_prices else price
        gain_pct = round((price - week_low) / week_low * 100, 1) if week_low > 0 else 0

        if profit > PROFIT_MIN:
            buys.append({
                "name": name, "grade": grade, "price": price,
                "predicted": predicted, "profit": profit,
                "gain_pct": gain_pct, "week_low": week_low,
            })

    buys.sort(key=lambda x: x["profit"], reverse=True)
    return buys

def get_history_prices(name):
    """从 CSV 取该子弹历史价格"""
    prices = []
    if not os.path.exists(CSV_FILE):
        return prices
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                val = row.get(name, "")
                try:
                    p = int(float(val))
                    if p > 0:
                        prices.append(p)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    return prices[-100:]  # 最近100条

# ============================================
# 推送: Server酱微信
# ============================================
def push_report(buys, total, timestamp):
    """推送微信日报"""
    if not SENDKEY:
        print("[推送] 跳过, 未设置 SENDKEY")
        return

    now = datetime.now(TZ_CN)
    wd = WEEKDAY_CN.get(now.weekday(), "")
    title = f"三角洲子弹导购 {now.strftime('%m/%d')} {wd}"

    if now.weekday() in (0, 1):
        tip = "> 周初低价窗口，以下为周末预期利润 Top 10"
    elif now.weekday() in (2, 3):
        tip = "> 价格已上行，以下为周末预期利润 Top 10"
    elif now.weekday() == 4:
        tip = "> 周五已近峰值，注意区分已涨 vs 预期再涨空间"
    else:
        tip = "> 周末峰值区间，可关注下周初回调机会"

    lines = [
        f"## 三角洲子弹导购日报",
        f"**{now.strftime('%m/%d')} {wd}** | {timestamp}",
        f"常规子弹: {total} 种",
        "", tip, "",
        "| # | 子弹 | 当前 | 周低 | 已涨 | 周末预估 | 预期利润 |",
        "|---|------|------|------|------|----------|----------|",
    ]

    if buys:
        for i, b in enumerate(buys[:10], 1):
            g_cn = GRADE_CN.get(b["grade"], f"{b['grade']}级弹")
            gain = f"+{b['gain_pct']}%" if b['gain_pct'] >= 0 else f"{b['gain_pct']}%"
            lines.append(
                f"| {i} | {g_cn} {b['name']} | {b['price']} | "
                f"{b['week_low']} | {gain} | {b['predicted']} | **+{b['profit']}** |")
    else:
        lines.append("| - | 暂无满足条件的买入信号 | - | - | - | - | - |")

    lines += ["", "> 利润=周末预估×手续费−买入价 | 数据: orzice | v3.0"]

    body = {"title": title, "desp": "\n".join(lines)}
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
    print("Delta Ammo Bot v3.0 (GitHub Actions)")
    print(f"启动时间: {datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1. 采集
    data = fetch_prices()
    if data["total"] == 0:
        print("[失败] 无数据")
        sys.exit(1)

    items = data["items"]
    ts = data["timestamp"]
    print(f"  有效: {len(items)} 种子弹")

    # 2. 存储
    print("[存储] 写入 CSV...")
    save_csv(items, ts)

    # 3. 分析
    print("[分析] 预测引擎...")
    buys = analyze([it for it in items if it.get("grade", 0) in TARGET_GRADES])
    print(f"  买入建议: {len(buys)} 种")

    # 4. 推送
    now = datetime.now(TZ_CN)
    if force_push or now.hour == 22:
        print("[推送] 发送日报...")
        push_report(buys, len(items), ts)
    else:
        print(f"[跳过] 非推送时段 ({now.hour}时)")

    # 摘要
    for b in buys[:5]:
        print(f"  {GRADE_CN.get(b['grade'])} {b['name']}: "
              f"买入{b['price']} 利润+{b['profit']}")

    print("=" * 50)
    print("完成")

if __name__ == "__main__":
    main()
