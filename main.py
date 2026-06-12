import datetime
import os
from io import BytesIO
import urllib.parse
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
    "address": "542南投縣草屯鎮富寮里中正路568號",  # 支援超精準門牌
    "city": "南投縣",
    "town": "草屯鎮",
    "lon": 120.686,  # 草屯富寮里中心點經緯度預設值
    "lat": 23.978,
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
    print(
        f"\n⏰ [{now_str}] 排程觸發：數據同步中...【目前守護目標：{CURRENT_LOCATION['address']}】"
    )

    # 1. 抓取該縣市降雨機率 PoP
    try:
        url_pop = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&locationName={city}"
        res_pop = requests.get(url_pop, timeout=5, verify=False).json()
        location_data = res_pop["records"]["location"][0]
        for elem in location_data["weatherElement"]:
            if elem["elementName"] == "PoP":
                pop = int(elem["time"][0]["parameter"]["parameterName"])
                break
    except Exception as e:
        print(f"抓取 PoP 失敗: {e}")

    # 2. 智慧搜尋：找出最鄰近的雨量測站
    try:
        url_rain = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-002?Authorization={AUTH_KEY}&format=JSON"
        res_rain = requests.get(url_rain, timeout=6, verify=False).json()
        stations = res_rain["records"].get(
            "Station", res_rain["records"].get("location", [])
        )

        min_dist = 999.0
        best_station = None
        for s in stations:
            s_city = s.get("GeoInfo", {}).get("CountyName", "")
            s_town = s.get("GeoInfo", {}).get("TownName", "")
            # 精準匹配
            if city in s_city and town in s_town:
                best_station = s
                break
            # 距離匹配
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
            if rain_10m < 0:
                rain_10m = 0.0
            print(f"📡 已綁定最近雨量站: {best_station.get('StationName')}")
    except Exception as e:
        print(f"抓取雨量站失敗: {e}")

    # 3. 智慧搜尋：找出最近的氣象觀測站（風速、濕度）
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
            print(f"📡 已綁定最近氣象站: {best_station.get('StationName')}")
    except Exception as e:
        print(f"抓取氣象站失敗: {e}")

    # 4. 雷達圖分析
    try:
        url_radar_json = f"https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/O-A0058-003?Authorization={AUTH_KEY}&downloadType=WEB&format=JSON"
        res_json = requests.get(url_radar_json, timeout=8, verify=False).json()
        img_url = res_json["cwaopendata"]["dataset"]["resource"]["ProductURL"]
        img_res = requests.get(img_url, timeout=10, verify=False)
        img = Image.open(BytesIO(img_res.content)).convert("RGB")

        px_x, px_y = lonlat_to_pixel(target_lon, target_lat)
        radar_status = check_radar_pixel(img, px_x, px_y)
    except Exception as e:
        print(f"雷達視覺辨識失敗: {e}")

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
    print(f"✅ 數據更新完成！當前快取狀態: {current_cached_status}")


scheduler = BackgroundScheduler()
scheduler.add_job(fetch_weather_job, "interval", minutes=10)
scheduler.start()
fetch_weather_job()


# ================= 🌐 網頁前端 UI (極簡化：只留地址欄位) =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>智慧衣架門牌定位控制台</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }}
            .card {{ background: white; padding: 25px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; text-align: left; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ font-weight: bold; display: block; margin-bottom: 8px; color: #333; }}
            input {{ width: 100%; padding: 12px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 15px; }}
            button {{ background-color: #007bff; color: white; border: none; padding: 12px; font-size: 16px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; margin-top: 5px; }}
            button:hover {{ background-color: #0056b3; }}
            .status-box {{ background: #e9ecef; padding: 12px; border-radius: 8px; margin-top: 15px; font-family: monospace; font-size: 13px; line-height: 1.4; word-break: break-all; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2 style="text-align: center; color: #007bff; margin-top: 0;">衣架精準守護區域 🌍</h2>
            <p style="font-size: 14px; color: #666; text-align: center;">輸入完整精準門牌地址，系統會利用地理編碼自動鎖定該棟建築的雷達圖像素座標與最鄰近測站。</p>
            
            <div class="form-group">
                <label>📍 請輸入完整安裝地址</label>
                <input type="text" id="addressInput" value="{CURRENT_LOCATION['address']}" placeholder="例如：542南投縣草屯鎮富寮里中正路568號">
            </div>

            <button onclick="updateAddress()">💾 儲存門牌並立即同步</button>
            
            <h3 style="margin-top: 20px; margin-bottom: 5px; font-size: 15px;">📡 目前衣架同步狀態：</h3>
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

            function updateAddress() {{
                var addr = document.getElementById("addressInput").value.trim();
                if(!addr) {{
                    alert("地址不能為空！");
                    return;
                }}
                document.getElementById("statusBox").innerText = "⏳ 正在分析精準門牌之 GPS 座標...";

                fetch(`/api/set_address?address=${{encodeURIComponent(addr)}}`)
                    .then(res => res.json())
                    .then(data => {{
                        if(data.status === "SUCCESS") {{
                            alert(`🎉 門牌解碼成功！\\n鎖定區域：${{data.city}}${{data.town}}\\n座標：(${{data.lon}}, ${{data.lat}})`);
                            refreshStatus();
                        }} else {{
                            alert("❌ 無法辨識此地址，請確認縣市與道路名稱是否正確。");
                            refreshStatus();
                        }}
                    }})
                    .catch(err => {{
                        alert("連線後端伺服器失敗");
                    }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


# ================= 🌐 🆕 智能防錯升級：結構化地理編碼 API =================
@app.get("/api/set_address")
def set_address(address: str):
    global CURRENT_LOCATION
    try:
        headers = {"User-Agent": "SmartHangerApp/2.5 (contact: test@example.com)"}
        
        # 1. 先用原本使用者輸入的精準地址去搜尋
        geocode_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(address)}&format=json&addressdetails=1&limit=1"
        res = requests.get(geocode_url, headers=headers, timeout=5).json()

        # 🧠 2. 智能防錯機制：如果因為「號碼太精準」導致 OpenStreetMap 找不到...
        if not res or len(res) == 0:
            print(f"🔍 精準門牌找不到，啟動自動降級模糊搜尋...")
            
            # 削掉「XX號」或「XX號XX樓」再搜一次
            fallback_address = address
            for keyword in ["號", "樓"]:
                if keyword in fallback_address:
                    # 找到最後一個數字開頭到號結尾的位置切掉
                    idx = fallback_address.find(keyword)
                    while idx > 0 and fallback_address[idx-1].isdigit():
                        idx -= 1
                    fallback_address = fallback_address[:idx]
                    break
            
            # 如果連郵遞區號（3或5碼數字）也造成干擾，把最前面的數字拔掉
            if fallback_address and fallback_address[0].isdigit():
                fallback_address = "".join([c for c in fallback_address if not c.isdigit()])

            print(f"👉 嘗試使用模糊地址搜尋: {fallback_address}")
            geocode_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(fallback_address)}&format=json&addressdetails=1&limit=1"
            res = requests.get(geocode_url, headers=headers, timeout=5).json()

        # 3. 解析最終找到的結果
        if res and len(res) > 0:
            lon = float(res[0]["lon"])
            lat = float(res[0]["lat"])
            addr_details = res[0].get("address", {})

            # 提取縣市
            city = addr_details.get("county", "") or addr_details.get("city", "") or addr_details.get("state", "")
            # 提取鄉鎮區
            town = addr_details.get("town", "") or addr_details.get("suburb", "") or addr_details.get("city_district", "")

            # 🛠️ 萬一地圖回傳非結構化資料，使用中文保底機制
            if not city or ("縣" not in city and "市" not in city):
                for k in ["南投縣", "臺中市", "彰化縣", "臺北市", "新北市", "高雄市"]:
                    if k[:2] in address:
                        city = k
                        break
                if not city: city = address[:3]
                
            if not town:
                for kw in ["鎮", "鄉", "區", "市"]:
                    if kw in address:
                        start = address.find(city) + len(city) if city in address else 0
                        idx = address.find(kw, start)
                        town = address[max(0, idx - 2) : idx + 1]
                        break

            if city.startswith("台"):
                city = "臺" + city[1:]

            # 成功更新全域變數
            CURRENT_LOCATION["address"] = address
            CURRENT_LOCATION["city"] = city
            CURRENT_LOCATION["town"] = town
            CURRENT_LOCATION["lon"] = lon
            CURRENT_LOCATION["lat"] = lat

            # 讓背景天氣排程立刻刷新
            fetch_weather_job()

            return {
                "status": "SUCCESS",
                "city": city,
                "town": town,
                "lon": lon,
                "lat": lat
            }
        else:
            return {"status": "FAILED", "message": "即使模糊搜尋也找不到此區域，請檢查地名拼字"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


# ================= 原本開放給 ESP32 的 API 路徑 =================
@app.get("/hanger/status")
def get_hanger_status():
    return current_cached_status


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)