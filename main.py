import datetime
import requests
from fastapi import FastAPI
import urllib3
# 🆕 引入排程套件
from apscheduler.schedulers.background import BackgroundScheduler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

# 氣象署授權碼與設定
AUTH_KEY = "CWA-02744568-A84E-49F7-8496-8E9D0834D8C2"
URL_POP = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&locationName=%E5%8D%97%E6%8A%95%E7%B8%A3"
URL_RAIN = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-001?Authorization={AUTH_KEY}&format=JSON&RainfallElement=Past1hr&StationId=C0H960"
URL_WIND = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON&WeatherElement=WindDirection&StationId=CAH010"

# 🆕 全域變數：用來暫存最新的天氣狀態
current_cached_status = "OPEN (PoP:0% Rain:0.0mm Wind:0deg) [Initializing]"

def fetch_weather_job():
    """🆕 這個函式會由排程器在指定的時間點精準執行"""
    global current_cached_status
    pop = 0
    rain = 0.0
    wind_dir = 0.0
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"⏰ [{now_str}] 觸發定時任務：開始向氣象署同步最新數據...")

    # ---- 1. 抓降雨機率 PoP ----
    try:
        res_pop = requests.get(URL_POP, timeout=5, verify=False).json()
        location_data = res_pop["records"]["location"][0]
        for elem in location_data["weatherElement"]:
            if elem["elementName"] == "PoP":
                pop = int(elem["time"][0]["parameter"]["parameterName"])
                break
    except Exception as e:
        print(f"[⚠️ 定時抓取 PoP 失敗]: {e}")

    # ---- 2. 抓過去 1 小時雨量 Rain ----
    try:
        res_rain = requests.get(URL_RAIN, timeout=5, verify=False).json()
        stations = res_rain["records"].get("Station", res_rain["records"].get("location", []))
        if stations:
            station = stations[0]
            if "RainfallElement" in station and "Past1hr" in station["RainfallElement"]:
                rain = float(station["RainfallElement"]["Past1hr"]["Precipitation"])
            elif "weatherElement" in station:
                for elem in station["weatherElement"]:
                    if "Past1hr" in elem.get("elementName", "") or "rain" in elem.get("elementName", "").lower():
                        rain = float(elem["elementValue"]["Precipitation"])
                        break
    except Exception as e:
        print(f"[⚠️ 定時抓取 雨量 失敗]: {e}")

    # ---- 3. 抓風向 Wind ----
    try:
        res_wind = requests.get(URL_WIND, timeout=5, verify=False).json()
        stations = res_wind["records"].get("Station", res_wind["records"].get("location", []))
        if stations:
            station = stations[0]
            if "WeatherElement" in station and "WindDirection" in station["WeatherElement"]:
                wind_str = station["WeatherElement"]["WindDirection"]
                wind_dir = float(wind_str) if float(wind_str) >= 0 else 0.0
            elif "weatherElement" in station:
                for elem in station["weatherElement"]:
                    if elem.get("elementName") == "WindDirection":
                        wind_str = elem["elementValue"].get("value", "0")
                        wind_dir = float(wind_str) if float(wind_str) >= 0 else 0.0
                        break
    except Exception as e:
        print(f"[⚠️ 定時抓取 風向 失敗]: {e}")

    # ---- 🧠 智慧判斷與更新快取 ----
    data_metrics = f"(PoP:{pop}% Rain:{rain}mm Wind:{int(wind_dir)}deg)"
    
    if rain > 0.0:
        current_cached_status = f"CLOSE {data_metrics}"
    elif pop >= 70:
        current_cached_status = f"CLOSE {data_metrics}"
    elif 270 <= wind_dir <= 360:
        current_cached_status = f"CLOSE {data_metrics}"
    else:
        current_cached_status = f"OPEN {data_metrics}"
        
    print(f"✅ 數據同步完成！目前最新狀態: {current_cached_status}\n")

# ================= 🆕 啟動背景定時排程器 =================
scheduler = BackgroundScheduler()

# 💡 設定定時規則：在每小時的 0分, 15分, 30分, 45分 自動執行 fetch_weather_job
scheduler.add_job(fetch_weather_job, 'interval', minutes=10)

# 提示：如果你想改成「每隔 10 分鐘抓一次」，可以把上面這行換成底下這行：
# scheduler.add_job(fetch_weather_job, 'interval', minutes=10)

scheduler.start()

# 網站剛啟動時，先手動執行一次，確保快取裡面立刻有正確的資料
fetch_weather_job()


# ================= 🌐 開放給 ESP32 的 API 路徑 =================
@app.get("/hanger/status")
def get_hanger_status():
    # 當 ESP32 連進來，直接秒回記憶體裡的暫存資料，不再當場去連氣象署
    return current_cached_status
# 🆕 在 main.py 最底部加上這段
if __name__ == "__main__":
    import uvicorn
    import os
    # 讀取雲端平台分配的 Port，如果沒有（本機跑）就預設用 8080
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)