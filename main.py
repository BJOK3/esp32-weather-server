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
    "display_name": "",  
    "city": "",          
    "town": "",          
    "lon": 0.0,          
    "lat": 0.0,          
}

current_cached_status = "CLOSE (Loc:未設定位置，請先開啟控制台網頁設定區域)"


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

    if not CURRENT_LOCATION["city"] or not CURRENT_LOCATION["town"] or CURRENT_LOCATION["lon"] == 0.0:
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"💤 [{now_str}] 系統提示：目前尚未設定守護位置，排程暫停同步。")
        return

    city = CURRENT_LOCATION["city"]
    town = CURRENT_LOCATION["town"]
    target_lon = CURRENT_LOCATION["lon"]
    target_lat = CURRENT_LOCATION["lat"]

    pop, rain_10m, wind_dir, humidity, wind_speed = 0, 0.0, 0.0, 0, 0.0
    radar_status = "SAFE"

    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\n⏰ [{now_str}] 排程觸發...【目前目標：{CURRENT_LOCATION['display_name']}】")

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
        url_wind = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001?Authorization={AUTH_KEY}"
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
    print(f"✅ 數據同步完成！快取狀態: {current_cached_status}")


scheduler = BackgroundScheduler()
scheduler.add_job(fetch_weather_job, "interval", minutes=10)
scheduler.start()
fetch_weather_job()


# ================= 🌐 網頁前端 UI (全新連動下拉選單版 - 修正 f-string 衝突) =================
@app.get("/", response_class=HTMLResponse)
def get_home_page():
    # 🧠 修正關鍵：把超大的 JavaScript 縣市資料庫寫在外面，避開 python f-string 的大括號衝突
    javascript_database = """
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
    """

    # 這裡我們用兩個大括號 {{ }} 來逃脫 Python 的變數檢查
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>智慧衣架多功能控制台</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; background-color: #f0f4f8; padding: 20px; }}
            .card {{ background: white; padding: 25px; border-radius: 15px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); max-width: 420px; margin: 0 auto; text-align: left; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ font-weight: bold; display: block; margin-bottom: 6px; color: #333; font-size: 14px; }}
            input, select {{ width: 100%; padding: 11px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; background: white; }}
            button {{ color: white; border: none; padding: 12px; font-size: 15px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; margin-top: 5px; margin-bottom: 5px; transition: 0.2s; }}
            .btn-gps {{ background-color: #007bff; }}
            .btn-gps:hover {{ background-color: #0056b3; }}
            .btn-save {{ background-color: #28a745; }}
            .btn-save:hover {{ background-color: #218838; }}
            .status-box {{ background: #e9ecef; padding: 12px; border-radius: 8px; margin-top: 15px; font-family: monospace; font-size: 13px; line-height: 1.4; word-break: break-all; }}
            .hint {{ font-size: 12px; color: #666; margin-top: 3px; display: block; }}
            hr {{ border: 0; border-top: 1px solid #ddd; margin: 20px 0; }}
            .section-title {{ font-size: 14px; color: #007bff; font-weight: bold; margin-bottom: 10px; border-left: 4px solid #007bff; padding-left: 8px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2 style="text-align: center; color: #333; margin-top: 0; font-size: 22px;">衣架守護區域控制台 🛰️</h2>
            
            <div class="section-title">捷徑：手機晶片自動定位</div>
            <button class="btn-gps" onclick="getPhoneGPS()">🎯 抓取手機當前 GPS 守護此處</button>
            <span class="hint" style="margin-bottom: 10px;">點擊後自動抓取 GPS 並自動在下方選單對齊對應縣市！</span>
            
            <hr>

            <div class="section-title">手動設定：自訂位置</div>
            
            <div class="form-group">
                <label>1. 自訂顯示地名 (顯示在衣架螢幕上)</label>
                <input type="text" id="nameInput" value="{CURRENT_LOCATION['display_name']}" placeholder="例如：草屯家、頂樓曬衣場">
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
                <label>4. 精準經緯度座標 (選填：想更精準在打)</label>
                <input type="text" id="latlonInput" value="{f'{CURRENT_LOCATION['lat']},{CURRENT_LOCATION['lon']}' if CURRENT_LOCATION['lat'] != 0.0 else ''}" placeholder="例如：23.978,120.686">
                <span class="hint">💡 提示：留空的話，系統會自動使用上方選單的鄉鎮中心點；想精準到特定門牌，再貼上 Google 地圖複製的「緯度,經度」。</span>
            </div>

            <button class="btn-save" onclick="saveManualSettings()">💾 儲存手動設定並立即同步</button>
            
            <h3 style="margin-top: 20px; margin-bottom: 5px; font-size: 14px; color:#333;">📡 目前衣架同步狀態：</h3>
            <div class="status-box" id="statusBox">載入中...</div>
        </div>

        <script>
            // 💉 注入獨立的縣市資料庫
            {javascript_database}

            // 🚀 初始化：載入縣市下拉選單
            window.onload = function() {{
                const citySelect = document.getElementById("citySelect");
                for (let city in taiwanData) {{
                    let opt = document.createElement("option");
                    opt.value = city;
                    opt.innerHTML = city;
                    citySelect.appendChild(opt);
                }}
                refreshStatus();
            }};

            // 🔄 連動：當縣市改變時，動態更新鄉鎮選單
            function updateTownDropdown(selectedTown = "") {{
                const citySelect = document.getElementById("citySelect");
                const townSelect = document.getElementById("townSelect");
                const selectedCity = citySelect.value;

                townSelect.innerHTML = '<option value="">--請選擇--</option>';

                if (selectedCity && taiwanData[selectedCity]) {{
                    taiwanData[selectedCity].forEach(function(town) {{
                        let opt = document.createElement("option");
                        opt.value = town;
                        opt.innerHTML = town;
                        if (town === selectedTown) opt.selected = true;
                        townSelect.appendChild(opt);
                    }});
                }}
            }}

            function refreshStatus() {{
                fetch('/hanger/status')
                    .then(res => res.text())
                    .then(text => {{
                        document.getElementById("statusBox").innerText = text;
                    }});
            }}
            setInterval(refreshStatus, 4000);

            // 🎯 手機一鍵定位邏輯
            function getPhoneGPS() {{
                if (navigator.geolocation) {{
                    document.getElementById("statusBox").innerText = "⏳ 正在向手機索取 GPS 座標...";
                    navigator.geolocation.getCurrentPosition(function(position) {{
                        var lat = position.coords.latitude;
                        var lon = position.coords.longitude;
                        document.getElementById("statusBox").innerText = `🛰️ 取得 GPS (${{lat.toFixed(4)}}, ${{lon.toFixed(4)}})...`;
                        
                        fetch(`/api/set_by_gps?lat=${{lat}}&lon=${{lon}}`)
                            .then(res => res.json())
                            .then(data => {{
                                alert(`🎉 手機定位同步成功！\\n鎖定區域：${{data.city}}${{data.town}}`);
                                refreshStatus();
                                
                                document.getElementById("nameInput").value = data.name;
                                document.getElementById("citySelect").value = data.city;
                                updateTownDropdown(data.town);
                                document.getElementById("latlonInput").value = `${{lat.toFixed(4)}},${{lon.toFixed(4)}}`;
                            }});
                    }}, function(error) {{
                        alert("❌ 定位失敗: " + error.message);
                        refreshStatus();
                    }});
                }
            }}

            // 💾 儲存手動選單設定
            function saveManualSettings() {{
                var name = document.getElementById("nameInput").value.trim();
                var city = document.getElementById("citySelect").value;
                var town = document.getElementById("townSelect").value;
                var latlon = document.getElementById("latlonInput").value.trim();
                
                if(!name || !city || !town) {{
                    alert("『地名』、『縣市選單』、『鄉鎮選單』均為必填！");
                    return;
                }}

                var lat = "0";
                var lon = "0";
                if(latlon) {{
                    var parts = latlon.split(",");
                    if(parts.length !== 2) {{
                        alert("經緯度格式錯誤！");
                        return;
                    }}
                    lat = parts[0].trim();
                    lon = parts[1].trim();
                }}

                document.getElementById("statusBox").innerText = "⏳ 正在儲存設定並同步刷新氣象大數據...";

                fetch(`/api/set_manual?name=${{encodeURIComponent(name)}}&city=${{encodeURIComponent(city)}}&town=${{encodeURIComponent(town)}}&lat=${{lat}}&lon=${{lon}}`)
                    .then(res => res.json())
                    .then(data => {{
                        alert(`🎉 設定儲存成功！\\n守護目標：${{data.name}}\\n定位方式：${{data.mode}}`);
                        refreshStatus();
                    }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


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
        
        # 修正可能非標準格式的中文名
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

    fetch_weather_job()
    return {"status": "SUCCESS", "name": name, "mode": mode}


@app.get("/hanger/status")
def get_hanger_status():
    return current_cached_status


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)