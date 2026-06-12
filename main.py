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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

AUTH_KEY = "CWA-02744568-A84E-49F7-8496-8E9D0834D8C2"
TW_TZ = ZoneInfo("Asia/Taipei")

# ================= 🗺️ 全域變數設定 =================
CURRENT_LOCATION = {
    "display_name": "",  
    "city": "",          
    "town": "",          
    "lon": 0.0,          
    "lat": 0.0,          
}

current_cached_status = "CLOSE (Loc:未設定位置，請先開啟控制台網頁設定區域)"

# 📱 全新修改：移除了實體按鈕，改由雲端全權紀錄狀態
SYSTEM_MODE = "AUTO"      # 系統模式："AUTO" (自動) 或 "MANUAL" (手動)
REMOTE_COMMAND = "STOP"   # 手動模式指令："STOP", "CLOSE", "OPEN"


# ================= 🌤️ 氣象偵測核心函式 (修復 NameError) =================
def fetch_weather_job():
    """ 這個就是原本噴出 NameError 的函式，確保它被定義在 API 呼叫之前 """
    global current_cached_status, CURRENT_LOCATION
    
    # 如果還沒有設定位置，就不執行
    if not CURRENT_LOCATION["city"] or not CURRENT_LOCATION["town"]:
        current_cached_status = "CLOSE (Loc:未設定位置，請先開啟控制台網頁設定區域)"
        return

# ================= 🌤️ 氣象偵測核心函式 (修復縮排錯誤) =================
def fetch_weather_job():
    """ 
    定時或手動觸發的天氣檢查大腦。
    自動串接氣象署 API 與雷達圖分析，並將結果封裝，供 ESP32 下載。
    """
    global current_cached_status, CURRENT_LOCATION
    
    # 檢查是否設定位置
    if not CURRENT_LOCATION["city"] or not CURRENT_LOCATION["town"]:
        current_cached_status = "CLOSE (Loc:未設定位置，請先開啟控制台網頁設定區域)"
        return

    # 預設天氣初始值 (防止 API 局部出錯時程式崩潰)
    pop = 0
    rain_10m = 0.0
    wind_dir = 0.0
    wind_speed = 0.0
    humidity = 50
    radar_verdict = "SAFE"

    city_name = CURRENT_LOCATION["city"]
    town_name = CURRENT_LOCATION["town"]
    display_name = CURRENT_LOCATION["display_name"]

    try:
        # 🟢 這裡開始的所有程式碼，前面都必須有 8 個空格（相對於最左邊）
        # 也就是相對於 try: 必須往右縮排 4 個空格！
        
        # -----------------------------------------------------------------
        # 1. 抓取中央氣象署 (CWA) 鄉鎮天氣觀測資料
        # -----------------------------------------------------------------
        cwa_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}&format=JSON"
        res = requests.get(cwa_url, timeout=8, verify=False)
        
        if res.status_code == 200:
            data = res.json()
            stations = data.get("records", {}).get("Station", [])
            target_station = None

            for s in stations:
                geo = s.get("GeoInfo", {})
                if geo.get("CountyName") == city_name and geo.get("TownName") == town_name:
                    target_station = s
                    break
            
            if not target_station:
                for s in stations:
                    if s.get("GeoInfo", {}).get("CountyName") == city_name:
                        target_station = s
                        break

            if target_station:
                obs = target_station.get("WeatherElement", {})
                humidity = int(obs.get("RelativeHumidity", 50))
                rain_10m = float(obs.get("Now", {}).get("Precipitation10Min", 0.0))
                if rain_10m < 0: rain_10m = 0.0
                wind_speed = float(obs.get("WindSpeed", 0.0))
                wind_dir = float(obs.get("WindDirection", 0.0))
                print(f"[CWA 測站成功] 鎖定觀測站: {target_station.get('StationName')}")

        # -----------------------------------------------------------------
        # 2. 抓取預報資料庫 (取得未來幾小時內的降雨機率 PoP)
        # -----------------------------------------------------------------
        forecast_mapping = {
            "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009",
            "苗栗縣": "F-D0047-013", "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021",
            "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029", "屏東縣": "F-D0047-033",
            "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
            "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057",
            "臺北市": "F-D0047-061", "高雄市": "F-D0047-065", "新北市": "F-D0047-069",
            "臺中市": "F-D0047-073", "臺南市": "F-D0047-077", "金門縣": "F-D0047-081",
            "連江縣": "F-D0047-085"
        }
        
        fid = forecast_mapping.get(city_name, "F-D0047-089")
        pop_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{fid}?Authorization={AUTH_KEY}&elementName=PoP6h&format=JSON"
        pop_res = requests.get(pop_url, timeout=8, verify=False)
        
        if pop_res.status_code == 200:
            pop_data = pop_res.json()
            locations = pop_data.get("records", {}).get("locations", [{}])[0].get("location", [])
            for loc in locations:
                if loc.get("locationName") == town_name:
                    elems = loc.get("weatherElement", [])
                    if elems:
                        val = elems[0].get("time", [{}])[0].get("elementValue", [{}])[0].get("value", "0")
                        pop = int(val) if val and val != " " else 0
                    break

        # -----------------------------------------------------------------
        # 3. 即時降雨雷達圖切片 (雷達回波像素級掃描技術)
        # -----------------------------------------------------------------
        radar_api_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0058-001?Authorization={AUTH_KEY}&format=JSON"
        radar_res = requests.get(radar_api_url, timeout=8, verify=False)
        
        if radar_res.status_code == 200:
            radar_img_url = radar_res.json().get("records", {}).get("RadarImage", [{}])[0].get("ImageUrl", "")
            if radar_img_url:
                img_data = requests.get(radar_img_url, timeout=8, verify=False).content
                img = Image.open(BytesIO(img_data)).convert("RGB")
                
                lat_val = CURRENT_LOCATION["lat"]
                lon_val = CURRENT_LOCATION["lon"]
                
                if lat_val > 0 and lon_val > 0:
                    pixel_x = int((lon_val - 117.5) / (123.5 - 117.5) * 1024)
                    pixel_y = int((26.5 - lat_val) / (26.5 - 20.0) * 1024)
                    
                    danger_pixels = 0
                    for dx in range(-5, 6):
                        for dy in range(-5, 6):
                            tx = pixel_x + dx
                            ty = pixel_y + dy
                            if 0 <= tx < 1024 and 0 <= ty < 1024:
                                r, g, b = img.getpixel((tx, ty))
                                if r > 35 or g > 35 or b > 35:
                                    danger_pixels += 1
                    
                    if danger_pixels >= 8:
                        radar_verdict = "DANGER"
                        print(f"⚠️ [雷達警告] 偵測到周圍有強烈雨雲進逼！(危險點數: {danger_pixels})")

        # -----------------------------------------------------------------
        # 4. 打包數據更新快取
        # -----------------------------------------------------------------
        now_str = datetime.datetime.now(TW_TZ).strftime("%H:%M:%S")
        action_advice = "OPEN"
        if pop >= 70 or rain_10m > 0.0 or radar_verdict == "DANGER" or humidity > 85 or wind_speed > 8.0:
            action_advice = "CLOSE"
            
        current_cached_status = (
            f"{action_advice} (Loc:{display_name} 於 {now_str} 更新) | "
            f"PoP:{pop}% | Rain10m:{rain_10m}mm | "
            f"Wind:{wind_dir}deg | WSpd:{wind_speed}m/s | "
            f"Humid:{humidity}% | Radar:{radar_verdict}"
        )
        print(f"📡 [排程大腦成功] 目前最新狀態：{current_cached_status}")

    except Exception as e:
        # except 必須跟 try 站在同一個縮排水平線上
        current_cached_status = f"CLOSE (Error:氣象站聯動異常 {str(e)})"
        print(f"❌ [排程大腦失敗] 發生錯誤: {str(e)}")


# ================= ⏰ 自動定時排程 =================
scheduler = BackgroundScheduler()
# 每 10 分鐘自動執行一次氣象檢查
scheduler.add_job(fetch_weather_job, 'interval', minutes=10)
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
            
            <div class="section-title">⚙️ 核心運作模式設定</div>
            <div style="display: flex; gap: 10px;">
                <button class="btn-ctrl btn-mode-auto" onclick="setSystemMode('AUTO')">🤖 切換：自動AI守護</button>
                <button class="btn-ctrl btn-mode-manual" onclick="setSystemMode('MANUAL')">📱 切換：純手動遙控</button>
            </div>
            
            <div id="manualPanel" style="margin-top: 15px; display: none;">
                <div class="section-title" style="color: #fd7e14; border-left-color: #fd7e14;">🕹️ 手動即時遙控面板</div>
                <div style="display: flex; gap: 10px;">
                    <button class="btn-ctrl btn-close-hang" 
                            onmousedown="startHold('CLOSE')" onmouseup="stopHold()" onmouseleave="stopHold()"
                            ontouchstart="startHold('CLOSE')" ontouchend="stopHold()">🔼 遠端收衣</button>
                    <button class="btn-ctrl btn-open-hang" 
                            onmousedown="startHold('OPEN')" onmouseup="stopHold()" onmouseleave="stopHold()"
                            ontouchstart="startHold('OPEN')" ontouchend="stopHold()">🔽 遠端展開</button>
                    <button class="btn-ctrl btn-stop-hang" onclick="sendControl('STOP')">⏹️ 停止馬達</button>
                </div>
                <button class="btn-ctrl btn-stop-hang" onclick="sendControl('STOP')">⏹️ 停止馬達</button>
            </div>
            
            <hr>
            
            <div class="section-title">捷徑：手機晶片自動定位</div>
            <button class="btn-gps" onclick="getPhoneGPS()">🎯 抓取手機當前 GPS 守護此處</button>
            <span class="hint" style="margin-bottom: 10px;">點擊後自動抓取 GPS 並自動在下方選單對齊對應縣市！</span>
            
            <hr>

            <div class="section-title">手動設定：自訂位置</div>
            
            <div class="form-group">
                <label>1. 自訂顯示地名</label>
                <input type="text" id="nameInput" value="__DISPLAY_NAME__">
            </div>

            <div style="display: flex; gap: 10px;">
                <div class="form-group" style="flex: 1;">
                    <label>2. 縣市選單</label>
                    <select id="citySelect" onchange="updateTownDropdown()">
                        <option value="">--請選擇--</option>
                    </select>
                </div>
                <div class="form-group" style="flex: 1;">
                    <label>3. 鄉鎮市區</label>
                    <select id="townSelect">
                        <option value="">--請選擇--</option>
                    </select>
                </div>
            </div>

            <div class="form-group">
                <label>4. 精準經緯度座標 (選填)</label>
                <input type="text" id="latlonInput" value="__LAT_LON_VALUE__">
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

            window.onload = function() {
                const citySelect = document.getElementById("citySelect");
                for (let city in taiwanData) {
                    let opt = document.createElement("option");
                    opt.value = city; opt.innerHTML = city;
                    citySelect.appendChild(opt);
                }
                checkModeOnLoad();
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

            function setSystemMode(mode) {
                fetch(`/api/set_mode?mode=${mode}`)
                    .then(res => res.json())
                    .then(data => {
                        var panel = document.getElementById("manualPanel");
                        if (data.mode === "MANUAL") {
                            panel.style.display = "block";
                            alert("已切換為【純手動遙控模式】，此時 AI 守護暫停。");
                        } else {
                            panel.style.display = "none";
                            alert("已開啟【🤖 自動 AI 守護模式】。");
                        }
                        refreshStatus();
                    });
            }

            function checkModeOnLoad() {
                fetch('/api/get_current_mode')
                    .then(res => res.json())
                    .then(data => {
                        if(data.mode === "MANUAL") {
                            document.getElementById("manualPanel").style.display = "block";
                        }
                    });
            }

            function sendControl(cmd) {
                fetch(`/api/remote_control?cmd=${cmd}`)
                    .then(res => res.json())
                    .then(data => { refreshStatus(); });
            }

            function getPhoneGPS() {
                if (navigator.geolocation) {
                    document.getElementById("statusBox").innerText = "⏳ 正在向手機索取 GPS 座標...";
                    navigator.geolocation.getCurrentPosition(function(position) {
                        var lat = position.coords.latitude; var lon = position.coords.longitude;
                        fetch(`/api/set_by_gps?lat=${lat}&lon=${lon}`)
                            .then(res => res.json()).then(data => {
                                alert(`🎉 手機定位同步成功！\\n鎖定區域：${data.city}${data.town}`);
                                refreshStatus();
                                document.getElementById("nameInput").value = data.name;
                                document.getElementById("citySelect").value = data.city;
                                updateTownDropdown(data.town);
                            });
                    });
                }
            }

            function saveManualSettings() {
                var name = document.getElementById("nameInput").value.trim();
                var city = document.getElementById("citySelect").value;
                var town = document.getElementById("townSelect").value;
                if(!name || !city || !town) { alert("請填寫地名與選擇縣市鄉鎮！"); return; }
                fetch(`/api/set_manual?name=${encodeURIComponent(name)}&city=${encodeURIComponent(city)}&town=${encodeURIComponent(town)}&lat=0&lon=0`)
                    .then(res => res.json()).then(data => { alert("設定儲存成功！"); refreshStatus(); });
            }
        </script>
    </body>
    </html>
    """
    latlon_str = f"{CURRENT_LOCATION['lat']},{CURRENT_LOCATION['lon']}" if CURRENT_LOCATION['lat'] != 0.0 else ""
    final_html = html_template.replace("__DISPLAY_NAME__", CURRENT_LOCATION["display_name"])
    final_html = final_html.replace("__LAT_LON_VALUE__", latlon_str)
    return HTMLResponse(content=final_html, status_code=200)


# ================= 📱 API：模式切換 =================
@app.get("/api/set_mode")
def set_mode(mode: str):
    global SYSTEM_MODE, REMOTE_COMMAND
    if mode in ["AUTO", "MANUAL"]:
        SYSTEM_MODE = mode
        if mode == "AUTO":
            REMOTE_COMMAND = "STOP" 
    return {"status": "SUCCESS", "mode": SYSTEM_MODE}

@app.get("/api/get_current_mode")
def get_current_mode():
    return {"mode": SYSTEM_MODE}

@app.get("/api/remote_control")
def remote_control(cmd: str):
    global REMOTE_COMMAND
    if cmd in ["CLOSE", "OPEN", "STOP"]:
        REMOTE_COMMAND = cmd
    return {"status": "SUCCESS", "command": REMOTE_COMMAND}


# ================= 🌐 擴充狀態 API (唯一保留的正確版) =================
@app.get("/hanger/status")
def get_hanger_status():
    global current_cached_status, REMOTE_COMMAND, SYSTEM_MODE
    # 輸出格式如: "MODE:MANUAL | CMD:STOP | CLOSE (Loc: ...)"
    return f"MODE:{SYSTEM_MODE} | CMD:{REMOTE_COMMAND} | {current_cached_status}"


# ================= 🌐 後端 API：手機 GPS 定位 =================
@app.get("/api/set_by_gps")
def set_by_gps(lat: float, lon: float):
    global CURRENT_LOCATION
    
    CURRENT_LOCATION["display_name"] = "手機隨行點"
    CURRENT_LOCATION["city"] = "南投縣"  
    CURRENT_LOCATION["town"] = "埔里鎮"
    CURRENT_LOCATION["lat"] = lat
    CURRENT_LOCATION["lon"] = lon

    try:
        headers = {"User-Agent": "SmartHangerApp/4.0"}
        res = requests.get(f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&addressdetails=1", headers=headers, timeout=3).json()
        addr = res.get("address", {})
        city = addr.get("county", "") or addr.get("city", "") or addr.get("state", "")
        town = addr.get("town", "") or addr.get("suburb", "") or addr.get("city_district", "")
        
        if city: 
            if "市" in city: city = city[city.find("市")-2:city.find("市")+1]
            if "縣" in city: city = city[city.find("縣")-2:city.find("縣")+1]
            if city.startswith("台"): city = "臺" + city[1:]
            CURRENT_LOCATION["city"] = city
        if town: 
            CURRENT_LOCATION["town"] = town
        CURRENT_LOCATION["display_name"] = f"GPS({CURRENT_LOCATION['city']}{CURRENT_LOCATION['town']})"
    except:
        pass

    # 🟢 這裡呼叫就不會再 NameError 了，因為它已被定義在上方
    fetch_weather_job()
    return {
        "status": "SUCCESS", 
        "lon": lon, 
        "lat": lat,
        "name": CURRENT_LOCATION["display_name"],
        "city": CURRENT_LOCATION["city"],
        "town": CURRENT_LOCATION["town"]
    }


# ================= 🌐 後端 API：手動選單儲存 =================
@app.get("/api/set_manual")
def set_manual(name: str, city: str, town: str, lat: float, lon: float):
    global CURRENT_LOCATION
    
    CURRENT_LOCATION["display_name"] = name
    CURRENT_LOCATION["city"] = city
    CURRENT_LOCATION["town"] = town

    mode = "選單分區中心點定位"
    if lat != 0.0 and lon != 0.0:
        CURRENT_LOCATION["lat"] = lat
        CURRENT_LOCATION["lon"] = lon
        mode = "Google地圖公分級精準座標"
    else:
        try:
            headers = {"User-Agent": "SmartHangerApp/4.0"}
            res = requests.get(f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(city+town)}&format=json&limit=1", headers=headers, timeout=4).json()
            if res and len(res) > 0:
                CURRENT_LOCATION["lon"] = float(res[0]["lon"])
                CURRENT_LOCATION["lat"] = float(res[0]["lat"])
            else:
                CURRENT_LOCATION["lon"] = 120.68
                CURRENT_LOCATION["lat"] = 23.97
        except:
            CURRENT_LOCATION["lon"] = 120.68
            CURRENT_LOCATION["lat"] = 23.97

    # 🟢 正常呼叫
    fetch_weather_job()
    return {"status": "SUCCESS", "name": name, "mode": mode}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)