#!/usr/bin/env python3
"""
新豐風速通知：風速／陣風超過門檻時，透過 ntfy 推播到手機。
預報未來幾小時會下「需要撐傘」的雨時，也會另外推播下雨通知（有自己的冷卻期）。
連續 STATUS_HOURS 小時沒有任何通知時，送一則「現況＋下一小時預報」的定時回報。

資料來源
  1. 中央氣象署自動氣象站（O-A0001-001）即時觀測：平均風速、近一小時最大陣風
  2. Open-Meteo 逐時預報（依你家的經緯度）：未來幾小時的風速、陣風、雨量與降雨機率

只用 Python 標準函式庫，不需要 pip install。
所有設定都從環境變數讀取（見 README.md）。
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime
from zoneinfo import ZoneInfo


# ---------- 設定 ----------

def env(name, default=None):
    """讀環境變數；沒設或空字串就用預設值。"""
    value = os.environ.get(name, "").strip()
    return value if value else default


def env_float(name, default):
    try:
        return float(env(name, default))
    except (TypeError, ValueError):
        print(f"[警告] {name} 不是數字，改用預設值 {default}")
        return float(default)


CWA_API_KEY = env("CWA_API_KEY")
NTFY_TOPIC = env("NTFY_TOPIC")
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATION_NAME = env("CWA_STATION_NAME", "新豐")
LAT = env_float("LAT", 24.8996)        # 預設約為新豐鄉公所，請改成你家
LON = env_float("LON", 120.9838)

WIND_THRESHOLD = env_float("WIND_THRESHOLD", 8.0)    # 平均風速門檻（m/s），8.0 = 5 級
GUST_THRESHOLD = env_float("GUST_THRESHOLD", 13.9)   # 陣風門檻（m/s），13.9 = 7 級
FORECAST_HOURS = int(env_float("FORECAST_HOURS", 3))  # 往後看幾小時的預報
COOLDOWN_HOURS = int(env_float("COOLDOWN_HOURS", 3))  # 通知過後幾小時內不重複通知
STATUS_HOURS = int(env_float("STATUS_HOURS", 2))      # 幾小時沒任何通知就送定時回報（0 = 關閉）

# 下雨通知：預報某小時雨量 ≥ RAIN_THRESHOLD 且降雨機率 ≥ RAIN_PROB_THRESHOLD 才通知
# 1 mm/h 大約是「不撐傘會淋濕」的程度；毛毛雨通常 < 0.5 mm/h
RAIN_THRESHOLD = env_float("RAIN_THRESHOLD", 1.0)            # 每小時雨量門檻（mm），0 = 關閉下雨通知
RAIN_PROB_THRESHOLD = env_float("RAIN_PROB_THRESHOLD", 50)  # 降雨機率門檻（%）
RAIN_COOLDOWN_HOURS = int(env_float("RAIN_COOLDOWN_HOURS", COOLDOWN_HOURS))  # 下雨通知的冷卻期

TEST_NOTIFY = env("TEST_NOTIFY") in ("1", "true", "True")

ALERT_TAG = "wind-alert"
RAIN_TAG = "rain-alert"
STATUS_TAG = "wind-status"
TIMEOUT = 20
SLACK_SECONDS = 5 * 60   # 排程時間會飄，判斷「幾小時內」時多給 5 分鐘緩衝
TZ = ZoneInfo("Asia/Taipei")


# ---------- 工具 ----------

BEAUFORT_UPPER = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]


def beaufort(ms):
    """m/s 轉蒲福風級。"""
    return sum(1 for upper in BEAUFORT_UPPER if ms >= upper)


def fmt(ms):
    return f"{ms:.1f} m/s（{beaufort(ms)} 級）"


def http_get(url, allow_insecure_retry=False):
    req = urllib.request.Request(url, headers={"User-Agent": "wind-alert/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        # 氣象署網站偶爾會有憑證鏈不完整的問題，這時才退而不驗證憑證
        if allow_insecure_retry and isinstance(getattr(e, "reason", None), ssl.SSLError):
            print("[警告] 氣象署 SSL 憑證驗證失敗，改用不驗證憑證的方式重試")
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return resp.read().decode("utf-8")
        raise


def to_number(value):
    """氣象署用 -99 之類的負值表示缺值。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


# ---------- 資料來源 ----------

def get_cwa_observation():
    """回傳 dict：station, time, wind, gust；失敗回傳 None。"""
    if not CWA_API_KEY:
        print("[略過] 沒有設定 CWA_API_KEY，不抓氣象署即時觀測")
        return None

    query = urllib.parse.urlencode({
        "Authorization": CWA_API_KEY,
        "StationName": STATION_NAME,
        "format": "JSON",
    })
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001?{query}"
    try:
        data = json.loads(http_get(url, allow_insecure_retry=True))
    except Exception as e:
        print(f"[錯誤] 氣象署觀測資料抓取失敗：{e}")
        return None

    stations = (data.get("records") or {}).get("Station") or []
    if not stations:
        print(f"[錯誤] 氣象署回傳資料裡找不到測站「{STATION_NAME}」")
        return None

    # 同名測站時，優先選新竹縣的
    station = next(
        (s for s in stations
         if (s.get("GeoInfo") or {}).get("CountyName") in ("新竹縣", "新竹市")),
        stations[0],
    )
    we = station.get("WeatherElement") or {}
    return {
        "station": station.get("StationName", STATION_NAME),
        "time": (station.get("ObsTime") or {}).get("DateTime", ""),
        "wind": to_number(we.get("WindSpeed")),
        "gust": to_number((we.get("GustInfo") or {}).get("PeakGustSpeed")),
    }


def get_forecast():
    """回傳 list of dict：time, wind, gust, rain, rain_prob；失敗回傳 []。"""
    query = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "hourly": "wind_speed_10m,wind_gusts_10m,precipitation,precipitation_probability",
        "wind_speed_unit": "ms",
        "timezone": "Asia/Taipei",
        "forecast_hours": FORECAST_HOURS,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{query}"
    try:
        data = json.loads(http_get(url))
    except Exception as e:
        print(f"[錯誤] Open-Meteo 預報抓取失敗：{e}")
        return []

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    winds = hourly.get("wind_speed_10m") or []
    gusts = hourly.get("wind_gusts_10m") or []
    rains = hourly.get("precipitation") or []
    probs = hourly.get("precipitation_probability") or []
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "time": t,
            "wind": to_number(winds[i] if i < len(winds) else None),
            "gust": to_number(gusts[i] if i < len(gusts) else None),
            "rain": to_number(rains[i] if i < len(rains) else None),
            "rain_prob": to_number(probs[i] if i < len(probs) else None),
        })
    return rows


# ---------- 判斷 ----------

def check_observation(obs):
    lines = []
    if not obs:
        return lines
    t = obs["time"][11:16] if len(obs["time"]) >= 16 else obs["time"]
    if obs["wind"] is not None and obs["wind"] >= WIND_THRESHOLD:
        lines.append(f"現在（{obs['station']}站 {t}）平均風速 {fmt(obs['wind'])}")
    if obs["gust"] is not None and obs["gust"] >= GUST_THRESHOLD:
        lines.append(f"現在（{obs['station']}站 {t}）陣風 {fmt(obs['gust'])}")
    return lines


def check_forecast(rows):
    lines = []
    for r in rows:
        parts = []
        if r["wind"] is not None and r["wind"] >= WIND_THRESHOLD:
            parts.append(f"風速 {fmt(r['wind'])}")
        if r["gust"] is not None and r["gust"] >= GUST_THRESHOLD:
            parts.append(f"陣風 {fmt(r['gust'])}")
        if parts:
            lines.append(f"預報 {r['time'][11:16]}：" + "、".join(parts))
    return lines


def check_rain(rows):
    """預報裡雨量和降雨機率都達門檻的小時。降雨機率沒資料時只看雨量。"""
    lines = []
    if RAIN_THRESHOLD <= 0:
        return lines
    for r in rows:
        if r["rain"] is None or r["rain"] < RAIN_THRESHOLD:
            continue
        if r["rain_prob"] is not None and r["rain_prob"] < RAIN_PROB_THRESHOLD:
            continue
        lines.append(f"預報 {r['time'][11:16]}：{fmt_rain(r)}")
    return lines


def fmt_rain(r):
    rain = f"雨量 {r['rain']:.1f} mm" if r["rain"] is not None else "雨量無資料"
    prob = f"，降雨機率 {r['rain_prob']:.0f}%" if r["rain_prob"] is not None else ""
    return rain + prob


# ---------- ntfy ----------

def get_history():
    """取得 ntfy 主題最近的訊息（只取需要的時間範圍），回傳 list of (time, tags)。"""
    hours = max(COOLDOWN_HOURS, RAIN_COOLDOWN_HOURS, STATUS_HOURS, 1) + 1
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}/json?poll=1&since={hours}h"
    try:
        body = http_get(url)
    except Exception as e:
        print(f"[警告] 無法查詢 ntfy 歷史訊息（{e}），當作沒通知過")
        return []
    history = []
    for line in body.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") == "message":
            history.append((msg.get("time", 0), msg.get("tags") or []))
    return history


def sent_within(history, tags, hours):
    """history 裡 hours 小時內是否有帶 tags 其中之一的訊息。"""
    if hours <= 0:
        return False
    cutoff = time.time() - hours * 3600 + SLACK_SECONDS
    return any(t >= cutoff and any(tag in msg_tags for tag in tags)
               for t, msg_tags in history)


def send_ntfy(title, message, priority=4, tags=None):
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "wind-alert/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        print(f"[通知] 已送出（HTTP {resp.status}）")


# ---------- 主程式 ----------

def main():
    if not NTFY_TOPIC:
        print("[錯誤] 沒有設定 NTFY_TOPIC")
        return 1

    print(f"門檻：平均風速 ≥ {WIND_THRESHOLD} m/s，陣風 ≥ {GUST_THRESHOLD} m/s，"
          f"雨量 ≥ {RAIN_THRESHOLD} mm/h 且降雨機率 ≥ {RAIN_PROB_THRESHOLD:.0f}%；"
          f"預報看未來 {FORECAST_HOURS} 小時")

    obs = get_cwa_observation()
    if obs:
        w = fmt(obs["wind"]) if obs["wind"] is not None else "無資料"
        g = fmt(obs["gust"]) if obs["gust"] is not None else "無資料"
        print(f"即時觀測（{obs['station']} {obs['time']}）：平均 {w}，陣風 {g}")

    forecast = get_forecast()
    for r in forecast:
        w = f"{r['wind']:.1f}" if r["wind"] is not None else "-"
        g = f"{r['gust']:.1f}" if r["gust"] is not None else "-"
        print(f"預報 {r['time']}：平均 {w} m/s，陣風 {g} m/s，{fmt_rain(r)}")

    if TEST_NOTIFY:
        send_ntfy("風速通知測試", "設定成功！\n" + summary_text(obs, forecast),
                  priority=3, tags=["white_check_mark"])
        return 0

    history = get_history()
    wind_reasons = check_observation(obs) + check_forecast(forecast)
    rain_reasons = check_rain(forecast)

    # 風和雨各自判斷、各自冷卻，互不影響
    if wind_reasons:
        if sent_within(history, [ALERT_TAG], COOLDOWN_HOURS):
            print(f"風速超過門檻，但 {COOLDOWN_HOURS} 小時內已通知過，這次不重複通知")
        else:
            message = "\n".join(wind_reasons)
            print("風速超過門檻，送出通知：\n" + message)
            send_ntfy("💨 風大注意", message, priority=4, tags=["dash", ALERT_TAG])

    if rain_reasons:
        if sent_within(history, [RAIN_TAG], RAIN_COOLDOWN_HOURS):
            print(f"預報會下雨，但 {RAIN_COOLDOWN_HOURS} 小時內已通知過，這次不重複通知")
        else:
            message = "\n".join(rain_reasons) + "\n出門記得帶傘 ☂️"
            print("預報會下雨，送出通知：\n" + message)
            send_ntfy("☔ 可能下雨", message, priority=4, tags=["umbrella", RAIN_TAG])

    if wind_reasons or rain_reasons:
        return 0

    print("沒有超過門檻")
    if STATUS_HOURS <= 0:
        return 0
    if sent_within(history, [ALERT_TAG, RAIN_TAG, STATUS_TAG], STATUS_HOURS):
        print(f"{STATUS_HOURS} 小時內已有通知，不送定時回報")
        return 0
    print(f"已 {STATUS_HOURS} 小時沒有通知，送出定時回報")
    send_ntfy("🌬️ 風況回報", summary_text(obs, forecast),
              priority=2, tags=["leaves", STATUS_TAG])
    return 0


def summary_text(obs, forecast):
    """現況觀測＋下一小時預報，平均風速、陣風與雨量都列出。"""
    lines = []
    if obs:
        t = obs["time"][11:16] if len(obs["time"]) >= 16 else obs["time"]
        w = fmt(obs["wind"]) if obs["wind"] is not None else "無資料"
        g = fmt(obs["gust"]) if obs["gust"] is not None else "無資料"
        lines.append(f"現在（{obs['station']}站 {t}）\n  平均 {w}\n  陣風 {g}")
    else:
        lines.append("現在：沒抓到氣象署觀測資料")

    nxt = next_hour_row(forecast)
    if nxt:
        w = fmt(nxt["wind"]) if nxt["wind"] is not None else "無資料"
        g = fmt(nxt["gust"]) if nxt["gust"] is not None else "無資料"
        lines.append(f"預報 {nxt['time'][11:16]}\n  平均 {w}\n  陣風 {g}\n  {fmt_rain(nxt)}")
    else:
        lines.append("預報：沒抓到預報資料")
    return "\n".join(lines)


def next_hour_row(forecast):
    """預報裡第一個時間在現在之後的小時（即下一小時）；找不到就用最後一筆。"""
    now = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M")
    for r in forecast:
        if r["time"] > now:
            return r
    return forecast[-1] if forecast else None


if __name__ == "__main__":
    sys.exit(main())
