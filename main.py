import requests

import paho.mqtt.client as mqtt
import math

from PIL import Image
from io import BytesIO
import time
import datetime
import os
from io import BytesIO
import urllib.parse
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from PIL import Image
import requests
import urllib3
from collections import deque
event_queue = deque()

MQTT_BROKER = "658761669c52448a9eb3d3576ab49e33.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_TOPIC = "hanger/command"

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set("ESP32_Hanger", "a88888888A")
mqtt_client.tls_set() # HiveMQ Cloud 需要 TLS 加密
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()


last_action = "NONE"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

AUTH_KEY = "CWA-02744568-A84E-49F7-8496-8E9D0834D8C2"
TW_TZ = ZoneInfo("Asia/Taipei")

# ================= 🗺️ 修改全域變數預設值 =================
CURRENT_LOCATION = {
    "display_name": "尚未設定位置",  
    "city": "",         
    "town": "",         
    "lon": 0.0,            
    "lat": 0.0,             
}

current_cached_status = "CLOSE 請先開啟控制台網頁，透過 GPS 定位或手動選擇地區以開始監測。"

# 📱 全新修改：移除了實體按鈕，改由雲端全權紀錄狀態
SYSTEM_MODE = "AUTO"      # 系統模式："AUTO" (自動) 或 "MANUAL" (手動)
REMOTE_COMMAND = "STOP"   # 手動模式指令："STOP", "CLOSE", "OPEN"

def parse_taiwan_address(addr):
    # 列印出 Nominatim 實際回傳的內容，讓我們在 Render 的 logs 看到
    print(f"DEBUG: Nominatim raw address: {addr}")
    
    city = addr.get("county") or addr.get("city") or addr.get("state") or ""
    town = addr.get("town") or addr.get("city_district") or addr.get("district") or addr.get("suburb") or ""
    
    # 針對台灣地址的校正
    if "臺北市" in city or "台北市" in city: city = "臺北市"
    if "新北市" in city: city = "新北市"
    
    return city, town

def get_distance(lat1, lon1, lat2, lon2):
    # 計算兩點距離 (公里)
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def find_nearest_station(stations, target_lat, target_lon):
    nearest = None
    min_dist = float('inf')
    for s in stations:
        try:
            # 檢查經緯度是否存在 (不同 API 的結構可能稍有差異)
            geo = s.get("GeoInfo", {})
            lat = float(geo.get("Coordinates", [{}])[0].get("StationLatitude", 0))
            lon = float(geo.get("Coordinates", [{}])[0].get("StationLongitude", 0))
            
            # 忽略無效座標 (例如 0,0)
            if lat == 0 or lon == 0: continue
            
            dist = get_distance(target_lat, target_lon, lat, lon)
            if dist < min_dist:
                min_dist = dist
                nearest = s
        except (ValueError, KeyError, IndexError):
            continue
    return nearest

def fetch_weather_job():
    global current_cached_status, CURRENT_LOCATION, last_action, REMOTE_COMMAND
    
    # 檢查是否已設定有效位置 (緯度不為 0)
    if CURRENT_LOCATION["lat"] == 0.0 or CURRENT_LOCATION["lon"] == 0.0:
        current_cached_status = "等待設定 (請點擊網頁進行定位)"
        return

    if not CURRENT_LOCATION["city"] or not CURRENT_LOCATION["town"]:
        current_cached_status = "CLOSE (Loc:未設定位置，請先開啟控制台網頁設定區域)"
        return

    def safe_float(val, default=0.0):
        try:
            f = float(val)
            return f if f >= 0 else default
        except (ValueError, TypeError):
            return default

    city_name = CURRENT_LOCATION["city"]
    town_name = CURRENT_LOCATION["town"]
    pop, rain_10m, rain_1hr = 0, 0.0, 0.0
    wind_speed, wind_dir, humidity = 0.0, 0.0, 50
    risk_score = 0
    action_advice = "SAFE"


    try:
        # 1. 抓取雨量 (O-A0002-001)
        rain_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-001?Authorization={AUTH_KEY}&format=JSON"
        res_rain = requests.get(rain_url, timeout=8, verify=False)
        if res_rain.status_code == 200:
            stations = res_rain.json().get("records", {}).get("Station", [])
            # 1. 嘗試使用經緯度搜尋
            target = find_nearest_station(stations, CURRENT_LOCATION["lat"], CURRENT_LOCATION["lon"])

            # 2. 如果沒找到 (例如經緯度無效)，才嘗試縣市比對
            if not target:
                target = next((s for s in stations if s["GeoInfo"]["TownName"] == town_name), None)

            # 3. 再沒找到，才嘗試縣市搜尋 (降級為最後手段)
            if not target:
                target = next((s for s in stations if s["GeoInfo"]["CountyName"] == city_name), None)
            if target:
                rain_el = target.get("RainfallElement", {})
                rain_10m = safe_float(rain_el.get("Past10Min", {}).get("Precipitation"))
                rain_1hr = safe_float(rain_el.get("Past1hr", {}).get("Precipitation"))
                print(f"[雨量] 站點: {target['StationName']}, 10分雨量: {rain_10m}mm, 1小時雨量: {rain_1hr}mm")

        # 2. 抓取環境參數 (O-A0003-001)
        env_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON"
        res_env = requests.get(env_url, timeout=8, verify=False)
        if res_env.status_code == 200:
            stations = res_env.json().get("records", {}).get("Station", [])
            
            # 優先嘗試找最近的站
            target = find_nearest_station(stations, CURRENT_LOCATION["lat"], CURRENT_LOCATION["lon"])
            
            # 如果找不到，退回鄉鎮搜尋
            if not target:
                target = next((s for s in stations if s["GeoInfo"]["TownName"] == town_name), None)
            
            # 再找不到，退回縣市搜尋
            if not target:
                target = next((s for s in stations if s["GeoInfo"]["CountyName"] == city_name), None)
            
            if not target:
                print(f"[WARN] 找不到 {city_name}{town_name} 的環境氣象站資料")
            
            if target:
                obs = target.get("WeatherElement", {})
                # 解析數據
                humidity = int(safe_float(obs.get("RelativeHumidity", 50)))
                wind_speed = safe_float(obs.get("WindSpeed", 0.0))
                wind_dir = safe_float(obs.get("WindDirection", 0.0))
                print(f"[環境] 站點: {target['StationName']}, 濕度: {humidity}%, 風速: {wind_speed}m/s")

        # 3. 抓取預報 (F-C0032-001) - 只請求 PoP (降雨機率)
        forecast_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={AUTH_KEY}&elementName=PoP&format=JSON"
        pop_res = requests.get(forecast_url, timeout=8, verify=False)
        
        if pop_res.status_code == 200:
            locations = pop_res.json().get("records", {}).get("location", [])
            # 根據 city_name 查找該縣市
            city_data = next((loc for loc in locations if loc.get("locationName") == city_name), None)
            
            if city_data:
                elements = city_data.get("weatherElement", [])
                
                # 直接解析降雨機率 (PoP)
                pop_elem = next((el for el in elements if el.get("elementName") == "PoP"), None)
                if pop_elem and "time" in pop_elem:
                    val = pop_elem["time"][0].get("parameter", {}).get("parameterName", "0")
                    pop = int(val) if val.isdigit() else 0
                
                print(f"[CWA 預報] {city_name} 降雨機率: {pop}%")

# 4. 雷達圖分析 (O-A0058-003)
        radar_api_url = f"https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/O-A0058-003?Authorization={AUTH_KEY}&downloadType=WEB&format=JSON"
        radar_score = 0 # 新增：將雷達風險量化為 0-10 的數字
        try:
            radar_res = requests.get(radar_api_url, timeout=12, verify=False)
            if radar_res.status_code == 200:
                img_url = radar_res.json().get("cwaopendata", {}).get("dataset", {}).get("resource", {}).get("ProductURL")
                if img_url:
                    img_res = requests.get(img_url, timeout=15, verify=False)
                    img = Image.open(BytesIO(img_res.content)).convert("RGB")
                    lat_val = CURRENT_LOCATION["lat"]
                    lon_val = CURRENT_LOCATION["lon"]
                    
                    if lat_val > 0 and lon_val > 0:
                        pixel_x = int((lon_val - 118.0) / (124.0 - 118.0) * 3600)
                        pixel_y = int((26.5 - lat_val) / (26.5 - 20.5) * 3600)
                        danger_pixels = 0
                        # 檢查 11x11 的範圍
                        for dx in range(-5, 6):
                            for dy in range(-5, 6):
                                tx, ty = pixel_x + dx, pixel_y + dy
                                if 0 <= tx < 3600 and 0 <= ty < 3600:
                                    r, g, b = img.getpixel((tx, ty))
                                    # 根據實際像素顏色判斷降雨強度
                                    if r > 50 or g > 50 or b > 50:
                                        danger_pixels += 1
                        
                        # 【改進】將雷達像素數轉換為 0-10 分數
                        # 假設檢測範圍共 121 點，danger_pixels 佔比越高，風險分數越高
                        radar_score = min(10, int((danger_pixels / 121) * 20)) 
                        print(f"[雷達] 危險點數: {danger_pixels}, 轉換分數: {radar_score}/10")
        except Exception as e:
            print(f"❌ [雷達分析失敗] {e}")
            radar_score = 0

# ==================== 修改位置：fetch_weather_job() 內的決策邏輯 ====================
        # 5. 決策邏輯
        rain_trend_score = 0
        if rain_10m > 1.0:
            rain_trend_score += 3
        elif rain_10m > 0.3:
            rain_trend_score += 2
        elif rain_10m > 0:
            rain_trend_score += 1

        # 新增：Past1hr 雨量判斷（更能看出持續降雨）
        if rain_1hr > 5.0:
            rain_trend_score += 4
        elif rain_1hr > 2.0:
            rain_trend_score += 3
        elif rain_1hr > 0.5:
            rain_trend_score += 2
        elif rain_1hr > 0:
            rain_trend_score += 1

        # 決策邏輯部分
        # 之前是 if radar_verdict == "DANGER":
        # 現在改為：
        if radar_score >= 7:
            risk_score += 4 # 極高風險
        elif radar_score >= 4:
            risk_score += 2 # 中等風險
            
            
        if pop >= 80:
            rain_trend_score += 2
        elif pop >= 60:
            rain_trend_score += 1

        # 根據 rain_trend_score 決定趨勢狀態
        if rain_trend_score >= 6:
            trend_state = "RISING_FAST"
        elif rain_trend_score >= 4:
            trend_state = "RISING"
        elif rain_trend_score >= 2:
            trend_state = "STABLE"
        else:
            trend_state = "CLEARING"

        # 風險分數計算
        risk_score = 0

        # 趨勢狀態基礎分
        if trend_state == "RISING_FAST":
            risk_score += 3
        elif trend_state == "RISING":
            risk_score += 2
        elif trend_state == "STABLE":
            risk_score += 1

        # 額外加分項

        if rain_10m > 0.3 or rain_1hr > 1.0:
            risk_score += 1
        if pop >= 80:
            risk_score += 1
        if wind_speed > 5.0:
            risk_score += 1
        if rain_1hr > 3.0:
            risk_score += 1

        action_advice = "CLOSE" if risk_score >= 4 else "OPEN"

        # 6. 更新狀態
        now_str = datetime.datetime.now(TW_TZ).strftime("%H:%M:%S")
        REMOTE_COMMAND = action_advice
        if action_advice != last_action:
            event_queue.append(f"{now_str} - 動作變更為: {action_advice} (風險分:{risk_score})")
            last_action = action_advice

        current_cached_status = (
            f"(Loc:{city_name}{town_name} 於 {now_str} 更新) | "
            f"PoP:{pop}% | Rain10m:{rain_10m}mm | Rain1hr:{rain_1hr}mm | "
            f"Radar:{radar_score}/10 | Wind:{wind_dir}deg | WSpd:{wind_speed}m/s | "
            f"Humid:{humidity}% | Risk:{risk_score}"
        )
        print(f"📡 [排程成功] {current_cached_status}")

    except Exception as e:
        print(f"❌ [排程失敗] {str(e)}")
        current_cached_status = f"CLOSE (Error:聯動異常 {str(e)})"

# ================= ⏰ 自動定時排程 =================
scheduler = BackgroundScheduler()
# 每 10 分鐘自動執行一次氣象檢查
scheduler.add_job(fetch_weather_job, 'interval', minutes=3)
scheduler.start()


# ================= 🌐 網頁前端 UI =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>智慧衣架無線控制台</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            button {
            /* 防止選取文字 */
            -webkit-user-select: none; 
            user-select: none;
            /* 防止長按出現系統選單 */
            -webkit-touch-callout: none;
            touch-action: manipulation;}
            body { font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }
            .card { background: white; padding: 25px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 420px; margin: 0 auto; text-align: left; }
            .form-group { margin-bottom: 15px; }
            label { font-weight: bold; display: block; margin-bottom: 6px; color: #333; font-size: 14px; }
            input, select { width: 100%; padding: 11px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; background: white; }
            button { color: white; border: none; padding: 12px; font-size: 15px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; margin-top: 5px; margin-bottom: 5px; transition: 0.2s; }
            .btn-gps { background-color: #007bff; }
            .btn-gps:hover { background-color: #0056b3; }
            .btn-save { background-color: #28a745; }
            .btn-save:hover { background-color: #218838; }
            .btn-ctrl { font-size: 16px; margin: 5px 0; }
            .btn-mode-auto { background-color: #6f42c1; } 
            .btn-mode-manual { background-color: #fd7e14; } 
            .btn-close-hang { background-color: #dc3545; } 
            .btn-open-hang { background-color: #17a2b8; }  
            .btn-stop-hang { background-color: #6c757d; }  
            .status-box { background: #e9ecef; padding: 12px; border-radius: 8px; margin-top: 15px; font-family: monospace; font-size: 13px; line-height: 1.4; word-break: break-all; }
            .hint { font-size: 12px; color: #666; margin-top: 3px; display: block; }
            hr { border: 0; border-top: 1px solid #ddd; margin: 20px 0; }
            .section-title { font-size: 14px; color: #007bff; font-weight: bold; margin-bottom: 10px; border-left: 4px solid #007bff; padding-left: 8px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2 style="text-align: center; color: #333; margin-top: 0; font-size: 22px;">衣架守護區域控制台 🛰️</h2>
            
            <hr>
            
            <div class="section-title">捷徑：手機晶片自動定位</div>
            <button class="btn-gps" onclick="getPhoneGPS()">🎯 抓取手機當前 GPS 守護此處</button>
            <span class="hint" style="margin-bottom: 10px;">點擊後自動抓取 GPS 並自動在下方選單對齊對應縣市！</span>
            
            <hr>

            <div style="display: flex; gap: 10px;">
                <div class="form-group" style="flex: 1;">
                    <label>1. 縣市選單</label>
                    <select id="citySelect" onchange="updateTownDropdown(); checkLocationConsistency();">
                        <option value="">--請選擇--</option>
                    </select>

                    <select id="townSelect" onchange="checkLocationConsistency();">
                        <option value="">--請選擇--</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label>3. 精準經緯度座標 (選填)</label>
                <input type="text" id="latlonInput" value="__LAT_LON_VALUE__" placeholder="緯度, 經度">
            </div>

            <button class="btn-save" onclick="saveManualSettings()">💾 儲存手動設定並立即同步</button>
            
            <h3 style="margin-top: 20px; margin-bottom: 5px; font-size: 14px; color:#333;">📡 目前衣架同步狀態：</h3>
            <div class="status-box" id="statusBox">載入中...</div>
        </div>

        <script>
            // 🗺️ 台灣縣市與鄉鎮區完整連動資料庫
            const taiwanData = {
                "基隆市": ["仁愛區", "信義區", "中正區", "中山區", "安樂區", "暖暖區", "七堵區"],
                "臺北市": ["中正區", "大同區", "中山區", "鬆山區", "大安區", "萬華區", "信義區", "士林區", "北投區", "內湖區", "南港區", "文山區"],
                "新北市": ["板橋區", "三重區", "中和區", "永和區", "新莊區", "新店區", "樹林區", "鶯歌區", "三峽區", "淡水區", "汐止區", "瑞芳區", "土城區", "蘆洲區", "五股區", "泰山區", "林口區", "深坑區", "石碇區", "坪林區", "三芝區", "石門區", "八里區", "平溪區", "雙溪區", "貢寮區", "金山區", "萬里區", "烏來區"],
                "桃園市": ["桃園區", "中壢區", "大溪區", "楊梅區", "蘆竹區", "大園區", "龜山區", "八德區", "龍潭區", "平鎮區", "新屋區", "觀音區", "復興區"],
                "新竹市": ["東區", "北區", "香山區"],
                "新竹縣": ["竹北市", "竹東鎮", "新埔鎮", "關西鎮", "湖口鄉", "新豐鄉", "芎林鄉", "橫山鄉", "北埔鄉", "寶山鄉", "俄眉鄉", "尖石鄉", "五峰鄉"],
                "苗栗縣": ["苗栗市", "頭份市", "竹南鎮", "後龍鎮", "通霄鎮", "苑裡鎮", "卓蘭鎮", "造橋鄉", "西湖鄉", "頭屋鄉", "公館鄉", "銅鑼鄉", "三義鄉", "大湖鄉", "獅潭鄉", "三灣鄉", "南庄鄉", "泰安鄉"],
                "臺中市": ["中區", "東區", "南區", "西區", "北區", "北屯區", "西屯區", "南屯區", "太平區", "大里區", "霧峰區", "烏日區", "豐原區", "後里區", "石岡區", "東勢區", "和平區", "新社區", "潭子區", "大雅區", "神岡區", "大肚區", "沙鹿區", "龍井區", "梧棲區", "清水區", "大甲區", "外埔區", "大安區"],
                "彰化縣": ["彰化市", "員林市", "鹿港鎮", "和美鎮", "北斗鎮", "溪湖鎮", "田中鎮", "二林鎮", "線西鄉", "伸港鄉", "福興鄉", "秀水鄉", "花壇鄉", "芬園鄉", "大村鄉", "埔鹽鄉", "埔心鄉", "永靖鄉", "社頭鄉", "二水鄉", "田尾鄉", "埤頭鄉", "芳苑鄉", "大城鄉", "竹塘鄉", "溪州鄉"],
                "南投縣": ["南投市", "埔里鎮", "草屯鎮", "竹山鎮", "集集鎮", "名間鄉", "鹿谷鄉", "中寮鄉", "魚池鄉", "國姓鄉", "水里鄉", "信義鄉", "仁愛鄉"],
                "雲林縣": ["斗六市", "斗南鎮", "虎尾鎮", "西螺鎮", "土庫鎮", "北港鎮", "古坑鄉", "大埤鄉", "莿桐鄉", "林內鄉", "二崙鄉", "崙背鄉", "麥寮鄉", "東勢鄉", "褒忠鄉", "臺西鄉", "元長鄉", "四湖鄉", "口湖鄉", "水林鄉"],
                "嘉義市": ["東區", "西區"],
                "嘉義縣": ["太保市", "朴子市", "布袋鎮", "大林鎮", "民雄鄉", "溪口鄉", "新港鄉", "六腳鄉", "東石鄉", "義竹鄉", "鹿草鄉", "水上鄉", "中埔鄉", "竹崎鄉", "梅山鄉", "番路鄉", "大埔鄉", "阿里山鄉"],
                "臺南市": ["中西區", "東區", "南區", "西區", "北區", "安平區", "安南區", "永康區", "歸仁區", "新化區", "左鎮區", "玉井區", "楠西區", "南化區", "仁德區", "關廟區", "龍崎區", "官田區", "麻豆區", "佳里區", "西港區", "七股區", "將軍區", "學甲區", "北門區", "新營區", "後壁區", "白河區", "東山區", "六甲區", "下營區", "柳營區", "鹽水區", "善化區", "大內區", "山上區", "新市區"],
                "高雄市": ["新興區", "前金區", "苓雅區", "鹽埕區", "鼓山區", "旗津區", "前鎮區", "三民區", "楠梓區", "小港區", "左營區", "仁武區", "大社區", "岡山區", "路竹區", "阿蓮區", "田寮區", "燕巢區", "橋頭區", "梓官區", "彌陀區", "永安區", "湖內區", "鳳山區", "大寮區", "林園區", "鳥松區", "大樹區", "旗山區", "美濃區", "六龜區", "內門區", "杉林區", "甲仙區", "桃源區", "那瑪夏區", "茂林區", "茄萣區"],
                "屏東縣": ["屏東市", "潮州鎮", "東港鎮", "恆春鎮", "萬丹鄉", "長治鄉", "麟洛鄉", "九如鄉", "里港鄉", "鹽埔鄉", "高樹鄉", "萬巒鄉", "內埔鄉", "竹田鄉", "新埤鄉", "枋寮鄉", "新園鄉", "崁頂鄉", "林邊鄉", "南州鄉", "佳冬鄉", "琉球鄉", "車城鄉", "滿州鄉", "枋山鄉", "三地門鄉", "霧臺鄉", "瑪家鄉", "泰武鄉", "來義鄉", "春日鄉", "獅子鄉", "牡丹鄉"],
                "宜蘭縣": ["宜蘭市", "羅東鎮", "蘇澳鎮", "頭城鎮", "礁溪鄉", "壯圍鄉", "員山鄉", "冬山鄉", "五結鄉", "三星鄉", "大同鄉", "南澳鄉"],
                "花蓮縣": ["花蓮市", "鳳林鎮", "玉里鎮", "新城鄉", "吉安鄉", "壽豐鄉", "光復鄉", "豐濱鄉", "瑞穗鄉", "富里鄉", "秀林鄉", "萬榮鄉", "卓溪鄉"],
                "臺東縣": ["臺東市", "成功鎮", "關山鎮", "卑南鄉", "大武鄉", "太麻里鄉", "東河鄉", "長濱鄉", "鹿野鄉", "池上鄉", "綠島鄉", "延平鄉", "海端鄉", "達仁鄉", "金峰鄉", "蘭嶼鄉"],
                "澎湖縣": ["馬公市", "湖西鄉", "白沙鄉", "西嶼鄉", "望安鄉", "七美鄉"],
                "金門縣": ["金城鎮", "金湖鎮", "金沙鎮", "金寧鄉", "烈嶼鄉", "烏坵鄉"],
                "連江縣": ["南竿鄉", "北竿鄉", "莒光鄉", "東引鄉"]
            };
            
            function calculateDistance(lat1, lon1, lat2, lon2) {
                const R = 6371; // 地球半徑 (km)
                const dLat = (lat2 - lat1) * Math.PI / 180;
                const dLon = (lon2 - lon1) * Math.PI / 180;
                const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                          Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                          Math.sin(dLon/2) * Math.sin(dLon/2);
                return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            }
            
            function checkLocationConsistency() {
                const latlonInput = document.getElementById("latlonInput").value;
                const city = document.getElementById("citySelect").value;
                
                if (latlonInput && latlonInput.includes(",")) {
                    const [lat, lon] = latlonInput.split(",").map(Number);
                    
                    // 這裡設定一個「中心點」的概念
                    // 為了簡單處理，如果使用者選了縣市，我們對比該縣市的大致座標
                    // 這裡使用 Nominatim API 的反查概念更精確
                    fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json&accept-language=zh-TW`)
                        .then(res => res.json())
                        .then(data => {
                            if (data.address) {
                                const realCity = data.address.county || data.address.city || "";
                                // 如果選單選擇的城市與 GPS 反查出的城市不符
                                if (city && !realCity.includes(city)) {
                                    alert("⚠️ 偵測到您選擇的地區與經緯度位置不符！將為您清空經緯度。");
                                    document.getElementById("latlonInput").value = "";
                                }
                            }
                        })
                        .catch(err => console.error("位置驗證失敗", err));
                }
            }

            window.onload = function() {
                const citySelect = document.getElementById("citySelect");
                for (let city in taiwanData) {
                    let opt = document.createElement("option");
                    opt.value = city; opt.innerHTML = city;
                    citySelect.appendChild(opt);
                }
                refreshStatus();
            };

            function updateTownDropdown(selectedTown = "") {
                const citySelect = document.getElementById("citySelect");
                const townSelect = document.getElementById("townSelect");
                const selectedCity = citySelect.value;
                townSelect.innerHTML = '<option value="">--請選擇--</option>';
                if (selectedCity && taiwanData[selectedCity]) {
                    taiwanData[selectedCity].forEach(function(town) {
                        let opt = document.createElement("option");
                        opt.value = town; opt.innerHTML = town;
                        if (town === selectedTown) opt.selected = true;
                        townSelect.appendChild(opt);
                    });
                }
            }

            function refreshStatus() {
                fetch('/hanger/status')
                    .then(res => res.text())
                    .then(text => {
                        document.getElementById("statusBox").innerText = text;
                    });
            }
            setInterval(refreshStatus, 4000);

            
            function sendControl(cmd) {
                fetch(`/api/remote_control?cmd=${cmd}`)
                    .then(res => res.json())
                    .then(data => { refreshStatus(); });
            }

            // 修改後的 getPhoneGPS 函式
            function getPhoneGPS() {
                if (!navigator.geolocation) {
                    alert("您的瀏覽器不支援定位功能。");
                    return;
                }

                document.getElementById("statusBox").innerText = "⏳ 正在取得 GPS 定位...";
                
                // 設定超時時間，避免卡死
                navigator.geolocation.getCurrentPosition(
                    function(position) {
                        var lat = position.coords.latitude.toFixed(6);
                        var lon = position.coords.longitude.toFixed(6);
                        
                        document.getElementById("statusBox").innerText = "⏳ 定位成功，正在轉換為地址...";
                        
                        fetch(`/api/set_by_gps?lat=${lat}&lon=${lon}`)
                            .then(res => res.json())
                            .then(data => {
                                if (data.status === "SUCCESS") {
                                    alert("🎉 定位成功！\\n已鎖定經緯度：" + data.lat + "," + data.lon);
                                    // 原本可能有的：updateDropdown(data.city, data.town); 
                                    // -> 直接註解掉或刪除這行，避免它去嘗試匹配不存在的選項
                                    refreshStatus();
                                }
                            })
                            .catch(err => alert("伺服器連線失敗"));
                    },
                    function(error) {
                        var msg = "";
                        switch(error.code) {
                            case error.PERMISSION_DENIED: msg = "請在手機設定中允許瀏覽器存取位置權限。"; break;
                            case error.POSITION_UNAVAILABLE: msg = "無法取得位置資訊。"; break;
                            case error.TIMEOUT: msg = "定位請求超時，請稍後再試。"; break;
                        }
                        alert("定位失敗：" + msg);
                        document.getElementById("statusBox").innerText = "❌ 定位失敗";
                    },
                    { enableHighAccuracy: true, timeout: 10000 }
                );
            }

            // 更新：單純發送指令的函式
            function sendControl(cmd) {
                fetch(`/api/remote_control?cmd=${cmd}`)
                    .then(res => res.json())
                    .then(data => { 
                        console.log("指令已發送:", cmd);
                        refreshStatus(); // 發送後立即更新介面狀態
                    });
            }

            

            function saveManualSettings() {
                var btn = event.target;
                var city = document.getElementById("citySelect").value;
                var town = document.getElementById("townSelect").value;
                var latlonInput = document.getElementById("latlonInput").value.trim();
                
                if ((!city || !town) && !latlonInput) { 
                    alert("請選擇縣市鄉鎮，或直接輸入經緯度座標！"); 
                    return; 
                }

                // 1. 視覺上先行刷新：讓使用者立刻感覺到網頁已反應
                document.getElementById("statusBox").innerText = "⏳ 正在儲存設定並準備同步...";
                btn.disabled = true;

                var lat = 0, lon = 0;
                var displayName = (city && town) ? (city + town) : "自訂座標";
                if (latlonInput) {
                    var parts = latlonInput.split(",");
                    if (parts.length === 2) {
                        lat = parseFloat(parts[0].trim());
                        lon = parseFloat(parts[1].trim());
                    }
                }
                
                // 2. 發送儲存請求
                fetch(`/api/set_manual?name=${encodeURIComponent(displayName)}&city=${encodeURIComponent(city)}&town=${encodeURIComponent(town)}&lat=${lat}&lon=${lon}`)
                    .then(res => res.json())
                    .then(data => { 
                        // 3. 儲存成功後，觸發後端同步 (force_refresh)
                        return fetch('/api/force_refresh');
                    })
                    .then(() => {
                        // 4. 最後執行一次刷新，確保顯示最新的同步狀態
                        refreshStatus();
                        alert("同步完成！");
                    })
                    .catch(err => {
                        alert("同步失敗，請檢查網路。");
                        refreshStatus();
                    })
                    .finally(() => {
                        btn.disabled = false;
                    });
            }
        </script>
    </body>
    </html>
    """
    # 邏輯判斷：如果經緯度是 0.0，則顯示為空字串，避免輸入框出現 0.0
    latlon_str = f"{CURRENT_LOCATION['lat']},{CURRENT_LOCATION['lon']}" if CURRENT_LOCATION['lat'] != 0.0 else ""
    
    # 取出顯示名稱 (若沒設定則為空)
    display_name = CURRENT_LOCATION["display_name"] or ""
    
    # 進行字串替換
    final_html = html_template.replace("__DISPLAY_NAME__", display_name)
    final_html = final_html.replace("__LAT_LON_VALUE__", latlon_str)
    
    return HTMLResponse(content=final_html, status_code=200)



# 修改您的 API 路由
@app.get("/api/force_refresh")
def force_refresh():
    # 立即執行一次天氣檢查任務
    fetch_weather_job()
    # 發送 REFRESH 指令
    mqtt_client.publish(MQTT_TOPIC, "REFRESH")
    return {"status": "SUCCESS", "message": "已通知 ESP32 更新"}

# ================= 🌐 擴充狀態 API (唯一保留的正確版) =================
@app.get("/hanger/status")
def get_hanger_status():
    global current_cached_status, REMOTE_COMMAND, SYSTEM_MODE
    # 輸出格式如: "MODE:MANUAL | CMD:STOP | CLOSE (Loc: ...)"
    return f"MODE:{SYSTEM_MODE} | CMD:{REMOTE_COMMAND} | {current_cached_status}"



# 建議定義一個輔助函數，避免兩個 API 重複寫解析邏輯
def parse_address(addr):
    # 增加更多欄位映射：county(縣/市), city(市), state(省/州)
    city = addr.get("county") or addr.get("city") or addr.get("state") or ""
    # 增加更多欄位映射：town(鎮/市), city_district(區), suburb(村里), village(村), hamlet(聚落)
    town = addr.get("town") or addr.get("city_district") or addr.get("district") or addr.get("suburb") or addr.get("village") or addr.get("hamlet") or ""
    return city, town

@app.get("/api/set_manual")
def set_manual(name: str, city: str, town: str, lat: float, lon: float):
    global CURRENT_LOCATION
    
    if (not city or not town) and (lat != 0.0 and lon != 0.0):
        try:
            headers = {"User-Agent": "SmartHangerApp/4.0"}
            # 加入 accept-language=zh-TW
            url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language=zh-TW"
            res = requests.get(url, headers=headers, timeout=5).json()
            addr = res.get("address", {})
            city, town = parse_address(addr)
            name = f"座標定位({city}{town})"
        except Exception as e:
            print(f"❌ Reverse Geocoding Error: {e}")

    elif (city and town) and (lat == 0.0 or lon == 0.0):
        try:
            headers = {"User-Agent": "SmartHangerApp/4.0"}
            query = f"{city}{town}"
            url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1&accept-language=zh-TW"
            res = requests.get(url, headers=headers, timeout=5).json()
            if res:
                lat, lon = float(res[0]["lat"]), float(res[0]["lon"])
        except Exception as e:
            print(f"❌ Geocoding Error: {e}")

    # 【保護措施】如果解析後還是空的，給予預設值，確保程式不會因為沒地名而停止
    final_city = city if city else "未設定縣市"
    final_town = town if town else "未設定鄉鎮"

    CURRENT_LOCATION.update({
        "display_name": name if name else f"{final_city}{final_town}",
        "city": final_city,
        "town": final_town,
        "lat": lat,
        "lon": lon
    })
    
    print(f"DEBUG: 更新後的區域為 {final_city}{final_town}")
    fetch_weather_job()
    return {"status": "SUCCESS", "city": final_city, "town": final_town, "lat": lat, "lon": lon}


@app.get("/api/set_by_gps")
def set_by_gps(lat: float, lon: float):
    global CURRENT_LOCATION

    city = ""
    town = ""
    name = f"座標定位({lat},{lon})"

    try:
        headers = {"User-Agent": "SmartHangerApp/4.0"}
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
        res = requests.get(url, headers=headers, timeout=5).json()
        addr = res.get("address", {})
        city = addr.get("county", "") or addr.get("city", "")
        town = addr.get("town", "") or addr.get("city_district", "") or addr.get("suburb", "")
        name = f"座標定位({city}{town})"
    except:
        pass

    CURRENT_LOCATION.update({
        "display_name": name,
        "city": city,
        "town": town,
        "lat": lat,
        "lon": lon
    })

    fetch_weather_job()
    return {"status": "SUCCESS", "city": city, "town": town, "lat": lat, "lon": lon}


@app.get("/api/event")
def get_event():
    import time

    if event_queue:
        return {"action": event_queue.popleft()}

    start = time.time()
    while time.time() - start < 25:
        if event_queue:
            return {"action": event_queue.popleft()}
        time.sleep(0.5)

    return {"action": "NONE"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

