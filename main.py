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

# ================= 🗺️ 全域變數設定 =================
CURRENT_LOCATION = {
    "display_name": "南投縣草屯鎮中正路",  # 網頁與 ESP32 畫面上顯示的文字
    "city": "南投縣",                    # 氣象署 PoP 用的縣市名稱（務必填臺或南投等完整名稱）
    "town": "草屯鎮",                    # 氣象署過濾鄰近測站用的鄉鎮區名稱
    "lon": 120.686,                      # 最重要的精準雷達圖經度
    "lat": 23.978,                       # 最重要的精準雷達圖緯度
}

current_cached_status = "OPEN (PoP:0% Rain10m:0.0mm Wind:0deg Humid:0% WSpd:0.0m/s Radar:SAFE) [Initializing]"


def lonlat_to_pixel(lon: float, lat: float):
    """將經緯度轉換為 3600x3600 雷達圖的像素座標"""
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
    """定時排程：動態根據當前設定的縣市與經緯度抓取觀測站數據"""
    global current_cached_status, CURRENT_LOCATION

    city = CURRENT_LOCATION["city"]
    town = CURRENT_LOCATION["town"]
    target_lon = CURRENT_LOCATION["lon"]
    target_lat = CURRENT_LOCATION["lat"]

    pop, rain_10m, wind_dir, humidity, wind_speed = 0, 0.0, 0.0, 0, 0.0
    radar_status = "SAFE"

    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\n⏰ [{now_str}] 排程觸發：數據同步中...【目前目標：{CURRENT_LOCATION['display_name']}】")

    # 1. 抓取該縣市降雨機率 PoP
    try:
        url_pop = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&locationName={city}"
        res_pop = requests.get(url_pop, timeout=5, verify=False).json()
        location_data = res_pop["records"]["location"][0]
        for elem in location_data["weatherElement"]:
            if elem["elementName"] == "PoP":
                pop = int(elem["time"][0]["parameter"]["parameterName"])
                break
    except:
        pass

    # 2. 智慧搜尋：找出最鄰近的雨量測站
    try:
        url_rain = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-002?Authorization={AUTH_KEY}&format=JSON"
        res_rain = requests.get(url_rain, timeout=6, verify=False).json()
        stations = res_rain["records"].get("Station", res_rain["records"].get("location", []))

        min_dist = 999.0
        best_station = None
        for s in stations:
            s_city = s.get("GeoInfo", {}).get("CountyName", "")
            s_town = s.get("GeoInfo", {}).get("TownName", "")
            if city in s_city and town in s_town:
                best_station = s
                break
            s_lon = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLongitude", 0))
            s_lat = float(s.get("GeoInfo", {}).get("Coordinates", [{}])[0].get("StationLatitude", 0))
            dist = ((s_lon - target_lon) ** 2 + (s_lat - target_lat) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_station = s

        if best_station:
            rain_10m = float(best_station["RainfallElement"]["Past10Min"]["Precipitation"])
            if rain_10m < 0:
                rain_10m = 0.0
    except:
        pass

    # 3. 智慧搜尋：找出最近的氣象觀測站（風速、濕度）
    try:
        url_wind = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON"
        res_wind = requests.get(url_wind, timeout=6, verify=False).json()
        stations = res_wind["records"].get("Station", res_wind["records"].get("location", []))

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
            wind_dir = float(elem["WindDirection"]) if float(elem.get("WindDirection", 0)) >= 0 else 0.0
            wind_speed = float(elem["WindSpeed"]) if float(elem.get("WindSpeed", 0)) >= 0 else 0.0
            humidity = int(elem["RelativeHumidity"]) if int(elem.get("RelativeHumidity", 0)) >= 0 else 0
    except:
        pass

    # 4. 雷達圖分析
    try:
        url_radar_json = f"https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/O-A0058-003?Authorization={AUTH_KEY}&downloadType=WEB&format=JSON"
        res_json = requests.get(url_radar_json, timeout=8, verify=False).json()
        img_url = res_json["cwaopendata"]["dataset"]["resource"]["ProductURL"]
        img_res = requests.get(img_url, timeout=10, verify=False)
        img = Image.open(BytesIO(img_res.content)).convert("RGB")

        px_x, px_y = lonlat_to_pixel(target_lon, target_lat)
        radar_status = check_radar_pixel(img, px_x, px_y)
    except:
        pass

    # ---- 🧠 智慧決策鏈 ----
    # 顯示你自訂的地名，讓 ESP32 讀取時知道目前守護哪裡
    data_metrics = f"(Loc:{CURRENT_LOCATION['display_name']} PoP:{pop}% Rain10m:{rain_10m}mm Wind:{int(wind_dir)}deg Humid:{humidity}% WSpd:{wind_speed}m/s Radar:{radar_status})"

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
    print(f"✅ 數據更新完成！快取狀態: {current_cached_status}")


scheduler = BackgroundScheduler()
scheduler.add_job(fetch_weather_job, "interval", minutes=10)
scheduler.start()
fetch_weather_job()


# ================= 🌐 網頁前端 UI (經緯度直接貼上版) =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>智慧衣架座標設定控制台</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }}
            .card {{ background: white; padding: 25px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; text-align: left; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ font-weight: bold; display: block; margin-bottom: 6px; color: #333; font-size: 14px; }}
            input, select {{ width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; }}
            button {{ background-color: #28a745; color: white; border: none; padding: 12px; font-size: 16px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; margin-top: 5px; }}
            button:hover {{ background-color: #218838; }}
            .status-box {{ background: #e9ecef; padding: 12px; border-radius: 8px; margin-top: 15px; font-family: monospace; font-size: 13px; line-height: 1.4; word-break: break-all; }}
            .hint {{ font-size: 12px; color: #666; margin-top: 3px; display: block; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2 style="text-align: center; color: #007bff; margin-top: 0;">智慧衣架精準設定 🌍</h2>
            <p style="font-size: 13px; color: #666; text-align: center; margin-bottom: 20px;">免除地名解析錯誤！直接填入氣象分區與 Google 地圖經緯度，達到 100% 準確率。</p>
            
            <div class="form-group">
                <label>1. 衣架顯示地名 (自由填寫，顯示在 ESP32 畫面上)</label>
                <input type="text" id="nameInput" value="{CURRENT_LOCATION['display_name']}" placeholder="例如：草屯富寮里家">
            </div>

            <div style="display: flex; gap: 10px;">
                <div class="form-group" style="flex: 1;">
                    <label>2. 縣市(PoP用)</label>
                    <input type="text" id="cityInput" value="{CURRENT_LOCATION['city']}" placeholder="例如：南投縣">
                </div>
                <div class="form-group" style="flex: 1;">
                    <label>3. 鄉鎮(測站用)</label>
                    <input type="text" id="townInput" value="{CURRENT_LOCATION['town']}" placeholder="例如：草屯鎮">
                </div>
            </div>

            <div class="form-group">
                <label>4. Google 地圖經緯度座標</label>
                <input type="text" id="latlonInput" value="{CURRENT_LOCATION['lat']},{CURRENT_LOCATION['lon']}" placeholder="例如：23.978,120.686">
                <span class="hint">💡 提示：在手機 Google 地圖上長按你家建築物，複製最上方彈出的那一串「緯度,經度」數字，直接貼在這一格即可！</span>
            </div>

            <button onclick="saveSettings()">💾 儲存設定並立即同步</button>
            
            <h3 style="margin-top: 20px; margin-bottom: 5px; font-size: 14px;">📡 目前衣架同步狀態：</h3>
            <div class="status-box" id="statusBox">載入中...</div>
        </div>

        <script>
            function refreshStatus() {{
                fetch('/hanger/status')
                    .then(res => res.text())
                    .then(text => {{
                        document.getElementById("statusBox").innerText = text;
                    }});
            }}
            setInterval(refreshStatus, 4000);
            refreshStatus();

            function saveSettings() {{
                var name = document.getElementById("nameInput").value.trim();
                var city = document.getElementById("cityInput").value.trim();
                var town = document.getElementById("townInput").value.trim();
                var latlon = document.getElementById("latlonInput").value.trim();
                
                if(!name || !city || !town || !latlon) {{
                    alert("所有欄位均為必填！");
                    return;
                }}

                // 拆分經緯度
                var parts = latlon.split(",");
                if(parts.length !== 2) {{
                    alert("經緯度格式錯誤！請確保格式為：緯度,經度 (例如: 23.978,120.686)");
                    return;
                }}
                var lat = parts[0].trim();
                var lon = parts[1].trim();

                document.getElementById("statusBox").innerText = "⏳ 正在同步更新氣象測站與雷達像素...";

                fetch(`/api/set_precise?name=${{encodeURIComponent(name)}}&city=${{encodeURIComponent(city)}}&town=${{encodeURIComponent(town)}}&lat=${{lat}}&lon=${{lon}}`)
                    .then(res => res.json())
                    .then(data => {{
                        if(data.status === "SUCCESS") {{
                            alert(`🎉 設定儲存成功！\\n目前守護區域：${{data.name}}\\n經緯度：(${{data.lon}}, ${{data.lat}})`);
                            refreshStatus();
                        }}
                    }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


# ================= 🌐 🆕 新增：直接寫入精準座標的 API =================
@app.get("/api/set_precise")
def set_precise(name: str, city: str, town: str, lat: float, lon: float):
    global CURRENT_LOCATION
    
    if city.startswith("台"):
        city = "臺" + city[1:]

    CURRENT_LOCATION["display_name"] = name
    CURRENT_LOCATION["city"] = city
    CURRENT_LOCATION["town"] = town
    CURRENT_LOCATION["lat"] = lat
    CURRENT_LOCATION["lon"] = lon

    # 強制手動刷新背景天氣 job
    fetch_weather_job()

    return {
        "status": "SUCCESS",
        "name": name,
        "lon": lon,
        "lat": lat
    }


# ================= 原本開放給 ESP32 的 API 路徑 =================
@app.get("/hanger/status")
def get_hanger_status():
    return current_cached_status


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)