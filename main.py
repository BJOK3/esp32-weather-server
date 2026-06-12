import datetime
import os
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from PIL import Image
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

AUTH_KEY = "CWA-02744568-A84E-49F7-8496-8E9D0834D8C2"

# ================= 🆕 動態位置全域變數設定 =================
# 預設依然為南投縣埔里鎮，但現在可以透過網頁隨時抽換！
CURRENT_LOCATION = {
    "city": "南投縣",
    "town": "埔里鎮",
    "lon": 121.00,  # 用於雷達圖換算
    "lat": 24.00,
}

# 全域變數：用來暫存給 ESP32 讀取的狀態
current_cached_status = "OPEN (PoP:0% Rain10m:0.0mm Wind:0deg Humid:0% WSpd:0.0m/s Radar:SAFE) [Initializing]"


def lonlat_to_pixel(lon: float, lat: float):
    """將經緯度轉換為 3600x3600 雷達圖的像素座標 (X, Y)"""
    px_x = int((lon - 118.0) / (124.0 - 118.0) * 3600)
    px_y = int((26.5 - lat) / (26.5 - 20.5) * 3600)
    return px_x, px_y


def check_radar_pixel(img, px_x: int, px_y: int):
    """檢查指定像素周邊 5x5 是否有彩色雨雲"""
    if not (0 <= px_x < 3600 and 0 <= px_y < 3600):
        return "OUT_OF_RANGE"
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            nx, ny = px_x + dx, px_y + dy
            if not (0 <= nx < 3600 and 0 <= ny < 3600):
                continue
            r, g, b = img.getpixel((nx, ny))
            if not (abs(r - g) < 20 and abs(g - b) < 20 and abs(r - b) < 20):
                if (r + g + b) > 50 and (r + g + b) < 730:
                    return "DANGER"
    return "SAFE"


def fetch_weather_job():
    """定時排程：動態根據當前設定的 CURRENT_LOCATION 抓取對應 API"""
    global current_cached_status, CURRENT_LOCATION

    city = CURRENT_LOCATION["city"]
    town = CURRENT_LOCATION["town"]
    target_lon = CURRENT_LOCATION["lon"]
    target_lat = CURRENT_LOCATION["lat"]

    pop, rain_10m, wind_dir, humidity, wind_speed = 0, 0.0, 0.0, 0, 0.0
    radar_status = "SAFE"

    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(
        f"\n⏰ [{now_str}] 排程觸發：開始同步數據【當前守護目標：{city}{town}】"
    )

    # ---- 1. 動態抓取該縣市的降雨機率 PoP ----
    try:
        url_pop = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&locationName={city}"
        res_pop = requests.get(url_pop, timeout=5, verify=False).json()
        location_data = res_pop["records"]["location"][0]
        for elem in location_data["weatherElement"]:
            if elem["elementName"] == "PoP":
                pop = int(elem["time"][0]["parameter"]["parameterName"])
                break
    except Exception as e:
        print(f"[⚠️ 抓取 PoP 失敗]: {e}")

    # ---- 2. 🆕 智慧搜尋：抓取全台雨量觀測，自動找出與該鄉鎮最近的測站 ----
    try:
        url_rain = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-002?Authorization={AUTH_KEY}&format=JSON"
        res_rain = requests.get(url_rain, timeout=6, verify=False).json()
        stations = res_rain["records"].get(
            "Station", res_rain["records"].get("location", [])
        )

        min_dist = 999.0
        best_station = None
        for s in stations:
            # 優先比對地名（例如：測站位於南投縣埔里鎮）
            s_city = s.get("GeoInfo", {}).get("CountyName", "")
            s_town = s.get("GeoInfo", {}).get("TownName", "")
            if city in s_city and town in s_town:
                best_station = s
                break
            # 如果地名沒對上，用經緯度畢氏定理找出最近的測站
            s_lon = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLongitude", 0))
            s_lat = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLatitude", 0))
            dist = ((s_lon - target_lon) ** 2 + (s_lat - target_lat) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_station = s

        if best_station:
            rain_10m = float(
                best_station["RainfallElement"]["Past10Min"]["Precipitation"]
            )
            # 如果讀到負數（測站故障或維護中），校正為 0.0
            if rain_10m < 0:
                rain_10m = 0.0
            print(f"📡 成功鎖定最鄰近雨量觀測站: {best_station.get('StationName', '未知')}")
    except Exception as e:
        print(f"[⚠️ 尋找鄰近雨量站失敗]: {e}")

    # ---- 3. 🆕 智慧搜尋：抓取氣象站資料，自動找出最近的風速與濕度測站 ----
    try:
        url_wind = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON"
        res_wind = requests.get(url_wind, timeout=6, verify=False).json()
        stations = res_wind["records"].get(
            "Station", res_wind["records"].get("location", [])
        )

        min_dist = 999.0
        best_station = None
        for s in stations:
            s_lon = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLongitude", 0))
            s_lat = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLatitude", 0))
            dist = ((s_lon - target_lon) ** 2 + (s_lat - target_lat) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_station = s

        if best_station:
            elem = best_station.get("WeatherElement", {})
            wind_dir = (
                float(elem["WindDirection"])
                if float(elem.get("WindDirection", 0)) >= 0
                else 0.0
            )
            wind_speed = (
                float(elem["WindSpeed"])
                if float(elem.get("WindSpeed", 0)) >= 0
                else 0.0
            )
            humidity = (
                int(elem["RelativeHumidity"])
                if int(elem.get("RelativeHumidity", 0)) >= 0
                else 0
            )
            print(f"📡 成功鎖定最鄰近氣象觀測站: {best_station.get('StationName', '未知')}")
    except Exception as e:
        print(f"[⚠️ 尋找鄰近氣象站失敗]: {e}")

    # ---- 4. 動態雷達圖解析 ----
    try:
        url_radar_json = f"https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/O-A0058-003?Authorization={AUTH_KEY}&downloadType=WEB&format=JSON"
        res_json = requests.get(url_radar_json, timeout=8, verify=False).json()
        img_url = res_json["cwaopendata"]["dataset"]["resource"]["ProductURL"]
        img_res = requests.get(img_url, timeout=10, verify=False)
        img = Image.open(BytesIO(img_res.content)).convert("RGB")

        # 根據動態設定的經緯度換算像素
        px_x, px_y = lonlat_to_pixel(target_lon, target_lat)
        radar_status = check_radar_pixel(img, px_x, px_y)
    except Exception as e:
        print(f"[⚠️ 雷達圖片視覺解析失敗]: {e}")

    # ---- 🧠 智慧決策鏈 ----
    data_metrics = f"(Loc:{city}{town} PoP:{pop}% Rain10m:{rain_10m}mm Wind:{int(wind_dir)}deg Humid:{humidity}% WSpd:{wind_speed}m/s Radar:{radar_status})"

    if (
        rain_10m > 0.0
        or radar_status == "DANGER"
        or pop >= 70
        or (270 <= wind_dir <= 360)
        or wind_speed > 8.0
        or humidity > 85
    ):
        current_cached_status = f"CLOSE {data_metrics}"
    else:
        current_cached_status = f"OPEN {data_metrics}"
    print(f"✅ 數據同步完成！伺服器最新狀態: {current_cached_status}")


# 排程器設定
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_weather_job, "interval", minutes=10)
scheduler.start()
fetch_weather_job()


# ================= 🌐 網頁前端 UI (支援手動變更縣市鄉鎮與經緯度) =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>智慧衣架地區設定控制台</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }}
            .card {{ background: white; padding: 25px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; text-align: left; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ font-weight: bold; display: block; margin-bottom: 5px; color: #333; }}
            input, select {{ width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; }}
            button {{ background-color: #28a745; color: white; border: none; padding: 12px; font-size: 16px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; margin-top: 10px; }}
            button:hover {{ background-color: #218838; }}
            .status-box {{ background: #e9ecef; padding: 12px; border-radius: 8px; margin-top: 15px; font-family: monospace; font-size: 13px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2 style="text-align: center; color: #007bff; margin-top: 0;">衣架守護區域設定 🌍</h2>
            <p style="font-size: 14px; color: #666; text-align: center;">請輸入您智慧衣架實體所在的縣市與鄉鎮，伺服器將自動切換定位 API 進行防雨守護。</p>
            
            <div class="form-group">
                <label>1. 輸入縣市 (例如：臺中市、臺北市、南投縣)</label>
                <input type="text" id="cityInput" value="{CURRENT_LOCATION['city']}" placeholder="請輸入完整縣市名稱">
            </div>
            
            <div class="form-group">
                <label>2. 輸入鄉鎮市區 (例如：西屯區、大安區、埔里鎮)</label>
                <input type="text" id="townInput" value="{CURRENT_LOCATION['town']}" placeholder="請輸入鄉鎮市區名稱">
            </div>

            <div style="display: flex; gap: 10px;">
                <div class="form-group" style="flex: 1;">
                    <label>中心點經度 (Lon)</label>
                    <input type="number" step="0.01" id="lonInput" value="{CURRENT_LOCATION['lon']}">
                </div>
                <div class="form-group" style="flex: 1;">
                    <label>中心點緯度 (Lat)</label>
                    <input type="number" step="0.01" id="latInput" value="{CURRENT_LOCATION['lat']}">
                </div>
            </div>

            <button onclick="updateLocation()">💾 儲存並立即更新測站 API</button>
            
            <h3 style="margin-top: 20px; margin-bottom: 5px; font-size: 15px;">📡 目前衣架同步狀態：</h3>
            <div class="status-box" id="statusBox">載入中...</div>
        </div>

        <script>
            // 定時更新狀態盒
            function refreshStatus() {{
                fetch('/hanger/status')
                    .then(res => res.text())
                    .then(text => {{
                        document.getElementById("statusBox").innerText = text;
                    }});
            }}
            setInterval(refreshStatus, 3000);
            refreshStatus();

            function updateLocation() {{
                var city = document.getElementById("cityInput").value.trim();
                var town = document.getElementById("townInput").value.trim();
                var lon = document.getElementById("lonInput").value.trim();
                var lat = document.getElementById("latInput").value.trim();
                
                if(!city || !town || !lon || !lat) {{
                    alert("所有欄位皆為必填！");
                    return;
                }}

                document.getElementById("statusBox").innerText = "⏳ 地區變更中，正在重新向氣象署拉取最新觀測站數據...";

                fetch(`/api/set_location?city=${{encodeURIComponent(city)}}&town=${{encodeURIComponent(town)}}&lon=${{lon}}&lat=${{lat}}`)
                    .then(res => res.json())
                    .then(data => {{
                        if(data.result === "SUCCESS") {{
                            alert(`🎉 區域已成功切換至：${{data.updated_to}}！\n排程 API 與雷達像素已同步完成調整。`);
                            refreshStatus();
                        }}
                    }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


# ================= 🌐 🆕 新增：設定位置的 API =================
@app.get("/api/set_location")
def set_location(city: str, town: str, lon: float, lat: float):
    global CURRENT_LOCATION
    # 確保使用者輸入的縣市包含常見的台/臺字相容處理
    if city.startswith("台"):
        city = "臺" + city[1:]

    CURRENT_LOCATION["city"] = city
    CURRENT_LOCATION["town"] = town
    CURRENT_LOCATION["lon"] = lon
    CURRENT_LOCATION["lat"] = lat

    # 修改完後，立刻手動觸發一次排程工作，讓全新的地區數據立刻生效，不用等10分鐘！
    fetch_weather_job()

    return {
        "result": "SUCCESS",
        "updated_to": f"{city}{town} (Lon:{lon}, Lat:{lat})",
    }


# ================= 🌐 原本開放給 ESP32 的 API 路徑 =================
@app.get("/hanger/status")
def get_hanger_status():
    return current_cached_status


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)