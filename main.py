import datetime
import os
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse  # 🆕 引入網頁回應
from PIL import Image
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

AUTH_KEY = "CWA-02744568-A84E-49F7-8496-8E9D0834D8C2"
URL_POP = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&locationName=%E5%8D%97%E6%8A%95%E7%B8%A3"
URL_RAIN = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-002?Authorization={AUTH_KEY}&format=JSON&StationId=C0H960"
URL_WIND = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON&WeatherElement=WindDirection&StationId=CAH010"
URL_RADAR_JSON = f"https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/O-A0058-003?Authorization={AUTH_KEY}&downloadType=WEB&format=JSON"

# 原本固定給 ESP32 的南投狀態
current_cached_status = "OPEN (PoP:0% Rain10m:0.0mm Wind:0deg Humid:0% WSpd:0.0m/s Radar:SAFE) [Initializing]"


def lonlat_to_pixel(lon: float, lat: float):
    """🧠 核心演算法：將經緯度轉換為 3600x3600 雷達圖的像素座標 (X, Y)"""
    # 經度範圍 118.0 ~ 124.0
    px_x = int((lon - 118.0) / (124.0 - 118.0) * 3600)
    # 緯度範圍 20.5 ~ 26.5 (注意：地圖 Y 軸是由北往南算，所以用 26.5 去減)
    px_y = int((26.5 - lat) / (26.5 - 20.5) * 3600)
    return px_x, px_y


def check_radar_pixel(img, px_x: int, px_y: int):
    """傳入圖片與像素座標，檢查周圍 5x5 是否有彩色雨雲"""
    # 防止座標超出 3600 範圍閃退
    if not (0 <= px_x < 3600 and 0 <= px_y < 3600):
        return "OUT_OF_RANGE"

    for dx in range(-2, 3):
        for dy in range(-2, 3):
            # 防止邊緣溢出
            nx, ny = px_x + dx, px_y + dy
            if not (0 <= nx < 3600 and 0 <= ny < 3600):
                continue

            r, g, b = img.getpixel((nx, ny))
            # 變色濾波：非灰、非黑、非白
            if not (abs(r - g) < 20 and abs(g - b) < 20 and abs(r - b) < 20):
                if (r + g + b) > 50 and (r + g + b) < 730:
                    return "DANGER"
    return "SAFE"


def fetch_weather_job():
    """定時排程：固定守護南投埔里家中的衣架"""
    global current_cached_status
    pop, rain_10m, wind_dir, humidity, wind_speed = 0, 0.0, 0.0, 0, 0.0
    radar_status = "SAFE"

    # 1. 降雨機率
    try:
        res_pop = requests.get(URL_POP, timeout=5, verify=False).json()
        pop = int(
            res_pop["records"]["location"][0]["weatherElement"][0]["time"][0][
                "parameter"
            ]["parameterName"]
        )
    except:
        pass

    # 2. 10分鐘雨量
    try:
        res_rain = requests.get(URL_RAIN, timeout=5, verify=False).json()
        rain_10m = float(
            res_rain["records"]["Station"][0]["RainfallElement"]["Past10Min"][
                "Precipitation"
            ]
        )
    except:
        pass

    # 3. 風速風向濕度
    try:
        res_wind = requests.get(URL_WIND, timeout=5, verify=False).json()
        station = res_wind["records"]["Station"][0]
        wind_dir = float(station["WeatherElement"]["WindDirection"])
        wind_speed = float(station["WeatherElement"]["WindSpeed"])
        humidity = int(station["WeatherElement"]["RelativeHumidity"])
    except:
        pass

    # 4. 固定測站雷達解析 (南投埔里經緯度: 121.0, 24.0)
    try:
        res_json = requests.get(URL_RADAR_JSON, timeout=8, verify=False).json()
        img_url = res_json["cwaopendata"]["dataset"]["resource"]["ProductURL"]
        img_res = requests.get(img_url, timeout=10, verify=False)
        img = Image.open(BytesIO(img_res.content)).convert("RGB")

        # 呼叫公式換算南投像素
        home_x, home_y = lonlat_to_pixel(121.0, 24.0)
        radar_status = check_radar_pixel(img, home_x, home_y)
    except Exception as e:
        print(f"排程雷達解析失敗: {e}")

    data_metrics = f"(PoP:{pop}% Rain10m:{rain_10m}mm Wind:{int(wind_dir)}deg Humid:{humidity}% WSpd:{wind_speed}m/s Radar:{radar_status})"

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
    print(f"✅ 定時同步完成！當前狀態: {current_cached_status}")


# =================排程器啟動=================
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_weather_job, "interval", minutes=10)
scheduler.start()
fetch_weather_job()


# ================= 🌐 🆕 新增：手機前端網頁 UI =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    """當你用手機瀏覽器連上網站首頁時，會顯示這個漂亮的按鈕畫面"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>雷達即時定位檢查</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }
            .card { background: white; padding: 30px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; }
            button { background-color: #007bff; color: white; border: none; padding: 15px 25px; font-size: 16px; border-radius: 8px; cursor: pointer; width: 100%; }
            button:hover { background-color: #0056b3; }
            #result { margin-top: 20px; font-weight: bold; font-size: 18px; color: #333; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>衣架守護者 - 手機定位雷達</h2>
            <p>點擊下方按鈕，獲取手機 GPS 並即時分析你上空的雷達迴波狀態：</p>
            <button onclick="getLocation()">🎯 發送手機定位檢查</button>
            <div id="result">等待定位中...</div>
        </div>

        <script>
            function getLocation() {
                var resultDiv = document.getElementById("result");
                if (navigator.geolocation) {
                    resultDiv.innerHTML = "正在獲取 GPS 座標...";
                    navigator.geolocation.getCurrentPosition(sendLocation, function(error) {
                        resultDiv.innerHTML = "❌ 定位失敗: " + error.message;
                    });
                } else {
                    resultDiv.innerHTML = "❌ 您的瀏覽器不支援 GPS 定位功能";
                }
            }

            function sendLocation(position) {
                var lat = position.coords.latitude;
                var lon = position.coords.longitude;
                var resultDiv = document.getElementById("result");
                resultDiv.innerHTML = "🛰️ 座標已取得，正在與氣象署雷達圖同步辨識...";

                // 發送給 FastAPI 後端 API
                fetch('/api/check_location?lat=' + lat + '&lon=' + lon)
                    .then(response => response.json())
                    .then(data => {
                        if (data.status === "DANGER") {
                            resultDiv.innerHTML = "<span style='color: red;'>⚠️ 警告：偵測到您上方 [X:" + data.pixel_x + ", Y:" + data.pixel_y + "] 出現強烈對流雨雲！(DANGER)</span>";
                        } else if (data.status === "SAFE") {
                            resultDiv.innerHTML = "<span style='color: green;'>✅ 安全：您上方上空乾淨無雨雲！(SAFE)</span>";
                        } else {
                            resultDiv.innerHTML = "座標超出台灣雷達涵蓋範圍";
                        }
                    })
                    .catch(err => {
                        resultDiv.innerHTML = "❌ 連線後端失敗";
                    });
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


# ================= 🌐 🆕 新增：接收手機經緯度的即時雷達分析 API =================
@app.get("/api/check_location")
def check_mobile_location(lat: float, lon: float):
    """接收前端傳來的經緯度，動態換算像素並分析雷達圖"""
    try:
        # 1. 換算像素座標
        px_x, px_y = lonlat_to_pixel(lon, lat)

        # 2. 即時下載最新雷達圖並分析
        res_json = requests.get(URL_RADAR_JSON, timeout=5, verify=False).json()
        img_url = res_json["cwaopendata"]["dataset"]["resource"]["ProductURL"]
        img_res = requests.get(img_url, timeout=5, verify=False)
        img = Image.open(BytesIO(img_res.content)).convert("RGB")

        # 3. 檢查像素顏色
        status = check_radar_pixel(img, px_x, px_y)

        return {"pixel_x": px_x, "pixel_y": px_y, "status": status}
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