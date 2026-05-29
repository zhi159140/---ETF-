# -*- coding: utf-8 -*-
"""
ETF动量评分系统 - 东方财富数据源版

1. 24日对数动量回归评分 score = 年化收益率 * R2
2. 59日短期涨幅止盈判断
3. 25%超涨进入21个交易日锁定期
4. 15:30后优先使用当天收盘数据
5. PushPlus 使用 txt 文本格式推送
"""

import json
import math
import os
import requests
from datetime import datetime, timedelta, timezone


ETF_POOL = [
    {"code": "588230", "market": "SH", "name": "科创200ETF"},
    {"code": "159915", "market": "SZ", "name": "创业板ETF"},
    {"code": "513100", "market": "SH", "name": "纳指100ETF"},
    {"code": "563360", "market": "SH", "name": "A500ETF"},
    {"code": "518880", "market": "SH", "name": "黄金ETF"},
]

HIST_DAYS = 24
ANNUAL_DAYS = 252
SHORT_DAYS = 59
TAKE_PROFIT_INCREASE = 0.25
LOCK_DAYS = 21

CHINA_TZ = timezone(timedelta(hours=8))
LOCK_FILE = "etf_lock_state.json"

MX_APIKEY = os.environ.get("MX_APIKEY")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def load_lock_state():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                lock_map = data.get("lock_map", {})
                if isinstance(lock_map, str):
                    try:
                        lock_map = json.loads(lock_map)
                    except Exception:
                        lock_map = {}
                lock_update_date = data.get("lock_update_date")
                return lock_map, lock_update_date
        except Exception as e:
            print(f"加载锁定状态失败: {e}")
    return {}, None


def save_lock_state(lock_map, lock_update_date):
    try:
        data = {
            "lock_map": lock_map,
            "lock_update_date": lock_update_date
        }
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"保存锁定状态失败: {e}")


lock_map, lock_update_date = load_lock_state()


def now_china():
    return datetime.now(CHINA_TZ)


def get_name(etf):
    return etf.get("name", etf.get("code"))


def send_pushplus_text(title, content):
    if not PUSHPLUS_TOKEN:
        print("PushPlus未推送：PUSHPLUS_TOKEN未设置")
        return False

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "txt"
    }
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(PUSHPLUS_URL, headers=headers, json=payload, timeout=20)
        result = response.json()
        if result.get("code") == 200:
            print("PushPlus推送成功")
            return True
        print(f"PushPlus推送失败: {result}")
        return False
    except Exception as e:
        print(f"PushPlus推送异常: {e}")
        return False


def get_etf_history_data(etf, limit=160):
    if not MX_APIKEY:
        print("错误：MX_APIKEY环境变量未设置")
        return []

    base_url = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
    query = f"{etf['code']}{etf.get('name', '')}近{limit}个交易日收盘价"
    headers = {
        "Content-Type": "application/json",
        "apikey": MX_APIKEY
    }
    data = {"toolQuery": query}

    try:
        response = requests.post(base_url, headers=headers, json=data, timeout=30)
        result = response.json()

        if result.get("status") != 0:
            print(f"错误：{etf['code']} API返回状态码 {result.get('status')}")
            return []

        data = result.get("data", {})
        inner_data = data.get("data", {})
        search_result = inner_data.get("searchDataResultDTO", {})
        dto_list = search_result.get("dataTableDTOList", [])

        if not dto_list:
            print(f"错误：{etf['code']} 未获取到K线数据")
            return []

        dto = dto_list[0]
        table = dto.get("table", {})
        dates = table.get("headName", [])
        close_values = None

        for key in table.keys():
            if key != "headName":
                close_values = table[key]
                break

        if close_values is None:
            print(f"错误：{etf['code']} 未找到收盘价数据")
            return []

        rows = []
        for date_str, close_str in zip(dates, close_values):
            date = date_str.replace("(日)", "").replace("(月)", "").replace("(年)", "")
            close = float(close_str.replace("元", "").replace(",", ""))
            rows.append({
                "date": date,
                "close": close,
            })

        rows.sort(key=lambda x: x["date"])
        return rows[-limit:]
    except Exception as e:
        print(f"获取 {etf['code']} {get_name(etf)} 数据失败: {e}")
        return []


def get_all_trade_dates(all_data):
    return sorted(set(
        row["date"]
        for rows in all_data.values()
        for row in rows
    ))


def get_today_and_asof_date(all_data):
    today = now_china().date()
    current_time = now_china().time()
    all_dates = get_all_trade_dates(all_data)

    if not all_dates:
        return today.isoformat(), None

    latest_date = all_dates[-1]
    today_str = today.isoformat()

    if latest_date == today_str and current_time >= datetime.strptime("15:30", "%H:%M").time():
        return today_str, today_str

    if latest_date == today_str and len(all_dates) >= 2:
        return today_str, all_dates[-2]

    return today_str, latest_date


def get_close_prices(rows, end_date, count):
    recent = [r for r in rows if r["date"] <= end_date]
    if len(recent) < count:
        return None
    recent = recent[-count:]
    return [r["close"] for r in recent]


def mean(values):
    return sum(values) / len(values)


def polyfit_slope_intercept(x, y):
    x_mean = mean(x)
    y_mean = mean(y)
    numerator = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(len(x)))
    denominator = sum((x[i] - x_mean) ** 2 for i in range(len(x)))

    if denominator == 0:
        return 0, y_mean

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean
    return slope, intercept


def calc_momentum_score_from_prices(prices):
    y = [math.log(p) for p in prices]
    x = list(range(len(y)))
    slope, intercept = polyfit_slope_intercept(x, y)
    annualized_returns = math.exp(slope * ANNUAL_DAYS) - 1
    y_mean = mean(y)
    y_pred = [slope * i + intercept for i in x]
    ss_res = sum((y[i] - y_pred[i]) ** 2 for i in range(len(y)))
    ss_tot = sum((v - y_mean) ** 2 for v in y)
    r2 = 0 if ss_tot == 0 else 1 - ss_res / ss_tot
    return annualized_returns * r2


def get_score(rows, asof_date):
    closes = get_close_prices(rows, asof_date, HIST_DAYS)
    if closes is None or len(closes) < HIST_DAYS:
        return None
    prices = closes + [closes[-1]]
    return calc_momentum_score_from_prices(prices)


def get_short_increase(rows, asof_date):
    closes = get_close_prices(rows, asof_date, SHORT_DAYS)
    if closes is None or len(closes) < SHORT_DAYS:
        return None
    return closes[-1] / closes[0] - 1


def count_elapsed_trade_days(trade_dates, last_date, current_date):
    if not last_date:
        return 0
    return sum(1 for d in trade_dates if last_date < d <= current_date)


def update_lock_days_once(asof_date, trade_dates):
    global lock_update_date

    if lock_update_date == asof_date:
        return

    if lock_update_date is None:
        lock_update_date = asof_date
        save_lock_state(lock_map, lock_update_date)
        return

    elapsed_days = count_elapsed_trade_days(trade_dates, lock_update_date, asof_date)
    if elapsed_days <= 0:
        return

    for code in list(lock_map.keys()):
        lock_map[code] -= elapsed_days
        if lock_map[code] <= 0:
            print(f"锁定解除: {code}")
            del lock_map[code]

    lock_update_date = asof_date
    save_lock_state(lock_map, lock_update_date)


def print_table(rows):
    print("{:<8} {:<14} {:>14} {:>12} {:>12} {:>8}".format(
        "代码", "名称", "score", "短期涨幅%", "锁定日", "目标"
    ))
    for r in rows:
        score_text = "" if r["score"] is None else "{:.6f}".format(r["score"])
        increase_text = "" if r["短期涨幅%"] is None else "{:.2f}%".format(r["短期涨幅%"])
        target_text = "是" if r["是否目标"] else ""
        print("{:<8} {:<14} {:>14} {:>12} {:>12} {:>8}".format(
            r["代码"], r["名称"], score_text, increase_text, r["剩余锁定交易日"], target_text
        ))


def build_text_report(rows, target_etf, today=None, asof_date=None):
    if today is None:
        today = now_china().strftime("%Y-%m-%d")

    lines = [
        f"ETF动量评分 - {today}"
    ]
    if asof_date:
        lines.append(f"数据截至：{asof_date}")

    lines.append("")
    lines.append("{:<8} {:<10} {:>12} {:>10} {:>8} {:>4}".format(
        "代码", "名称", "score", "短期涨幅", "锁定日", "目标"
    ))

    for r in rows:
        score_text = "" if r["score"] is None else "{:.6f}".format(r["score"])
        increase_text = "" if r["短期涨幅%"] is None else "{:.2f}%".format(r["短期涨幅%"])
        target_text = "是" if r["是否目标"] else ""
        lines.append("{:<8} {:<10} {:>12} {:>10} {:>8} {:>4}".format(
            r["代码"], r["名称"], score_text, increase_text, r["剩余锁定交易日"], target_text
        ))

    lines.append("")
    if target_etf is None:
        lines.append("结论：空仓，无可买ETF。")
    else:
        target = next(r for r in rows if r["代码"] == target_etf)
        lines.append(f"结论：目标ETF为 {target_etf} {target['名称']}。")

    return "\n".join(lines)


def build_markdown_report(rows, target_etf, today=None, asof_date=None):
    if today is None:
        today = now_china().strftime("%Y-%m-%d")

    md = f"# ETF动量评分 - {today}\n\n"
    if asof_date:
        md += f"数据截至：{asof_date}\n\n"

    md += "| 代码 | 名称 | score | 短期涨幅 | 剩余锁定交易日 | 是否目标 |\n"
    md += "|---|---|---:|---:|---:|---|\n"

    for r in rows:
        score_text = "" if r["score"] is None else "{:.6f}".format(r["score"])
        increase_text = "" if r["短期涨幅%"] is None else "{:.2f}%".format(r["短期涨幅%"])
        target_text = "是" if r["是否目标"] else ""
        md += "| {} | {} | {} | {} | {} | {} |\n".format(
            r["代码"], r["名称"], score_text, increase_text, r["剩余锁定交易日"], target_text
        )

    if target_etf is None:
        md += "\n结论：空仓，无可买ETF。\n"
    else:
        target = next(r for r in rows if r["代码"] == target_etf)
        md += f"\n结论：目标ETF为 {target_etf} {target['名称']}。\n"

    return md


def score_one_day():
    all_data = {}
    for etf in ETF_POOL:
        rows = get_etf_history_data(etf)
        if rows:
            all_data[etf["code"]] = rows

    today, asof_date = get_today_and_asof_date(all_data)
    if asof_date is None:
        print("无法取得交易日数据")
        return None, None, today, asof_date

    trade_dates = get_all_trade_dates(all_data)
    update_lock_days_once(asof_date, trade_dates)

    result_rows = []
    score_map = {}
    etf_map = {etf["code"]: etf for etf in ETF_POOL}

    for etf in ETF_POOL:
        code = etf["code"]
        rows = all_data.get(code, [])
        score = get_score(rows, asof_date)
        short_increase = get_short_increase(rows, asof_date)

        if score is not None:
            score_map[code] = score

        if short_increase is not None and short_increase >= TAKE_PROFIT_INCREASE:
            if lock_map.get(code, 0) == 0:
                lock_map[code] = LOCK_DAYS
                print(f"{code} {get_name(etf)} 短期涨幅 {short_increase * 100:.2f}% >= 25.00%，进入 {LOCK_DAYS} 个交易日锁定期")
                save_lock_state(lock_map, lock_update_date)

        result_rows.append({
            "代码": code,
            "名称": get_name(etf),
            "score": score,
            "短期涨幅%": short_increase * 100 if short_increase is not None else None,
            "剩余锁定交易日": lock_map.get(code, 0),
        })

    result_rows.sort(
        key=lambda r: r["score"] if r["score"] is not None else -999999,
        reverse=True
    )

    valid_rank = [
        r["代码"] for r in result_rows
        if score_map.get(r["代码"], -999) > 0 and lock_map.get(r["代码"], 0) == 0
    ]
    target_etf = valid_rank[0] if valid_rank else None

    for r in result_rows:
        r["是否目标"] = r["代码"] == target_etf

    print(f"\n=== 每日实时评分 | 评分日 {today} | 数据截至 {asof_date} ===")
    print_table(result_rows)

    if target_etf is None:
        print("\n无可买ETF：全部锁定或评分不大于0")
    else:
        print(f"\n目标ETF: {target_etf} {get_name(etf_map[target_etf])}")

    return result_rows, target_etf, today, asof_date


if __name__ == "__main__":
    print("=" * 60)
    print("ETF动量评分系统 - 聚宽风格 24日对数回归版")
    print("=" * 60)
    print()

    rows, target_etf, today, asof_date = score_one_day()

    if rows:
        today_str = now_china().strftime("%Y%m%d")
        md_content = build_markdown_report(rows, target_etf, today=today, asof_date=asof_date)
        output_file = f"etf_momentum_{today_str}.md"

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(md_content)

        print(f"\n报告已保存: {output_file}")

        push_title = f"ETF动量评分 {today}"
        push_content = build_text_report(rows, target_etf, today=today, asof_date=asof_date)
        send_pushplus_text(push_title, push_content)
