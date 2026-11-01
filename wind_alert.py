#!/usr/bin/env python3
"""
新豐風速通知：風速／陣風超過門檻時，透過 ntfy 推播到手機。

資料來源
  1. 中央氣象署自動氣象站（O-A0001-001）即時觀測：平均風速、近一小時最大陣風
  2. Open-Meteo 逐時預報（依你家的經緯度）：未來幾小時的風速與陣風

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

TEST_NOTIFY = env("TEST_NOTIFY") in ("1", "true", "True")

ALERT_TAG = "wind-alert"
TIMEOUT = 20


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
    """回傳 list of dict：time, wind, gust；失敗回傳 []。"""
    query = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "hourly": "wind_speed_10m,wind_gusts_10m",
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
    return [
        {"time": t, "wind": to_number(w), "gust": to_number(g)}
        for t, w, g in zip(times, winds, gusts)
    ]


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


# ---------- ntfy ----------

def recently_alerted():
    """查 ntfy 主題最近 COOLDOWN_HOURS 小時內是否已經發過風速通知。"""
    if COOLDOWN_HOURS <= 0:
        return False
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}/json?poll=1&since={COOLDOWN_HOURS}h"
    try:
        body = http_get(url)
    except Exception as e:
        print(f"[警告] 無法查詢 ntfy 歷史訊息（{e}），當作沒通知過")
        return False
    for line in body.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") == "message" and ALERT_TAG in (msg.get("tags") or []):
            return True
    return False


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

    print(f"門檻：平均風速 ≥ {WIND_THRESHOLD} m/s，陣風 ≥ {GUST_THRESHOLD} m/s；"
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
        print(f"預報 {r['time']}：平均 {w} m/s，陣風 {g} m/s")

    if TEST_NOTIFY:
        summary = []
        if obs and obs["wind"] is not None:
            summary.append(f"目前{obs['station']}站平均風速 {fmt(obs['wind'])}")
        if forecast and forecast[0]["gust"] is not None:
            summary.append(f"下一小時預報陣風 {fmt(forecast[0]['gust'])}")
        send_ntfy("風速通知測試",
                  "設定成功！\n" + ("\n".join(summary) if summary else "（這次沒抓到風速資料）"),
                  priority=3, tags=["white_check_mark"])
        return 0

    reasons = check_observation(obs) + check_forecast(forecast)
    if not reasons:
        print("沒有超過門檻，不通知")
        return 0

    if recently_alerted():
        print(f"超過門檻，但 {COOLDOWN_HOURS} 小時內已通知過，這次不重複通知")
        return 0

    message = "\n".join(reasons)
    print("超過門檻，送出通知：\n" + message)
    send_ntfy("💨 風大注意", message, priority=4, tags=["dash", ALERT_TAG])
    return 0


if __name__ == "__main__":
    sys.exit(main())
