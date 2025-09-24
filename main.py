# smart_irrigation_app.py
import logging
import sqlite3
from datetime import datetime, date
import requests
from flask import Flask, request, jsonify, render_template_string, redirect, url_for

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
DB_PATH = 'plant_watering.db'

# ------------------- DB Connection -------------------
def connect_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

# ------------------ Initialize DB --------------------
def init_db():
    conn = connect_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sensors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soil_moisture REAL,
                    temperature REAL,
                    humidity REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision TEXT,
                    water_amount INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    crop TEXT,
                    sow_date DATE,
                    area REAL DEFAULT 10.0,
                    soil_depth REAL DEFAULT 0.2
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tank_levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level_percent REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    conn.commit()
    conn.close()

init_db()

# ------------------- Static Data -------------------
# Crops and Translations skipped here for brevity
# Make sure you keep all previous CROP_PROFILES and TRANSLATIONS blocks
# Helper functions like `calculate_dynamic_kc_for_crop`, `get_et0_from_openmeteo`, etc. also unchanged
# So do NOT delete them from your script

# ------------------------- Watering Decision Route -------------------------
@app.route('/api/watering_decision', methods=['GET'])
def watering_decision():
    lang = request.args.get('lang', 'en')
    sensor = get_sensor_data()
    if sensor['timestamp'] is None:
        return jsonify({'error': 'No sensor data available'}), 404

    soil = float(sensor['soil_moisture'])
    humidity = float(sensor['humidity'])

    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT crop, sow_date, area, soil_depth FROM fields LIMIT 1")
    row = c.fetchone()
    conn.close()

    if row:
        crop = row['crop']
        sow_date = datetime.strptime(row['sow_date'], '%Y-%m-%d').date() if isinstance(row['sow_date'], str) else row['sow_date']
        area = float(row['area'])
        soil_depth = float(row['soil_depth'])
    else:
        crop = 'banana'
        sow_date = date.today()
        area = 10
        soil_depth = 0.2

    latitude = float(request.args.get('lat', 27.2))
    longitude = float(request.args.get('lon', 88.03))
    daily_forecast = get_daily_weather_forecast(latitude, longitude)
    et0 = get_et0_from_openmeteo(latitude, longitude) or 3.0
    kc, stage_name, days_elapsed = calculate_dynamic_kc_for_crop(crop, sow_date)

    predicted_rain = 0.0
    if daily_forecast and len(daily_forecast.get('precipitation', [])) >= 2:
        predicted_rain = float(daily_forecast['precipitation'][1] or 0.0)

    moisture_threshold = 30.0
    critical_moisture = 10.0
    decision_text = ""
    if soil >= moisture_threshold:
        decision_text = "No watering needed; soil moisture sufficient."
    else:
        today_prec = daily_forecast['precipitation'][0] if daily_forecast and daily_forecast.get('precipitation') else 0.0
        tomorrow_prec = predicted_rain
        if today_prec > 0.3:
            decision_text = "No watering needed; rain expected today."
        elif tomorrow_prec > 0.3 and soil >= critical_moisture:
            decision_text = "No watering needed; rain expected tomorrow and soil not critically low."
        elif soil < critical_moisture and tomorrow_prec > 0.3:
            decision_text = "Minimal watering recommended; soil critically low but rain tomorrow."
        else:
            if humidity < 30:
                decision_text = "Watering needed; low humidity and no rain expected."
            else:
                pass  # proceed to compute water amount

    water_amount = calculate_water_amount(soil, area, soil_depth, predicted_rain, et0, kc, humidity)

    if "No watering needed" in decision_text and water_amount > 0:
        decision_text = f"Warning: water deficit detected, {water_amount} liters needed."

    conn = connect_db()
    c = conn.cursor()
    c.execute("INSERT INTO decisions (decision, water_amount) VALUES (?, ?)", (decision_text, int(water_amount)))
    conn.commit()
    conn.close()

    tank = get_latest_tank_level()
    tank_level = tank['level_percent'] if tank['level_percent'] is not None else 0

    condition = "Good"
    if soil < critical_moisture:
        condition = "Poor - critically low moisture"
    elif soil < moisture_threshold:
        condition = "Fair - moisture below threshold"

    temp_warn = None
    if daily_forecast and len(daily_forecast.get('max_temperatures', [])) >= 2:
        t0 = daily_forecast['max_temperatures'][0]
        t1 = daily_forecast['max_temperatures'][1]
        if t1 > t0 + 4:
            temp_warn = "High temperature spike expected tomorrow."

    result = {
        'crop': crop,
        'crop_display': CROP_PROFILES.get(crop, {}).get('display', crop),
        'sow_date': str(sow_date),
        'days_elapsed': days_elapsed,
        'stage': stage_name,
        'kc': kc,
        'et0_mm_per_day': et0,
        'soil_moisture': soil,
        'humidity': humidity,
        'predicted_rain_tomorrow_mm': predicted_rain,
        'watering_decision': decision_text,
        'water_amount_liters': int(water_amount),
        'tank_level_percent': tank_level,
        'field_condition': condition,
        'temperature_warning': temp_warn
    }

    result_localized = {
        'title': t('title', lang),
        'watering_decision_label': t('watering_decision', lang),
        'water_amount_label': t('water_amount', lang),
        'field_condition_label': t('field_condition', lang),
        'latest_sensor_label': t('latest_sensor', lang),
        'sensor': sensor,
        'tank': tank,
        'result': result
    }

    return jsonify(result_localized), 200

# ------------------- UI Dashboard (Homepage) -------------------
INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <title>{{ t('title') }}</title>
    <script>
      function changeLang() {
        const lang = document.getElementById('lang').value;
        window.location.href = '/?lang=' + lang;
      }
    </script>
  </head>
  <body>
    <h1>{{ t('title') }}</h1>
    <form method="post" action="{{ url_for('set_field') }}">
      <label>{{ t('choose_crop') }}:
        <select name="crop">
          {% for key, profile in crops.items() %}
            <option value="{{ key }}" {% if current_crop == key %}selected{% endif %}>{{ profile.display }}</option>
          {% endfor %}
        </select>
      </label><br/>
      <label>{{ t('sow_date') }}:
        <input type="date" name="sow_date" value="{{ sow_date }}">
      </label><br/>
      <label>Area (m²):
        <input name="area" type="number" value="{{ area }}" step="0.1">
      </label><br/>
      <label>Soil depth (m):
        <input name="soil_depth" type="number" value="{{ soil_depth }}" step="0.01">
      </label><br/>
      <label>{{ t('lang') }}:
        <select id="lang" onchange="changeLang()">
          <option value="en" {% if lang == 'en' %}selected{% endif %}>English</option>
          <option value="hi" {% if lang == 'hi' %}selected{% endif %}>हिन्दी</option>
          <option value="si" {% if lang == 'si' %}selected{% endif %}>Sikkimese</option>
          <option value="ta" {% if lang == 'ta' %}selected{% endif %}>தமிழ்</option>
        </select>
      </label><br/>
      <button type="submit">{{ t('submit') }}</button>
    </form>
  </body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def set_field():
    lang = request.args.get('lang', 'en')
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT crop, sow_date, area, soil_depth FROM fields LIMIT 1")
    row = c.fetchone()
    conn.close()

    crop = row['crop'] if row else 'banana'
    sow_date = row['sow_date'] if row else str(date.today())
    area = row['area'] if row else 10
    soil_depth = row['soil_depth'] if row else 0.2

    if request.method == 'POST':
        crop = request.form.get('crop', crop)
        sow_date = request.form.get('sow_date', sow_date)
        area = float(request.form.get('area', area))
        soil_depth = float(request.form.get('soil_depth', soil_depth))

        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT id FROM fields LIMIT 1")
        exists = c.fetchone()
        if exists:
            c.execute("UPDATE fields SET crop=?, sow_date=?, area=?, soil_depth=? WHERE id=?",
                      (crop, sow_date, area, soil_depth, exists['id']))
        else:
            c.execute("INSERT INTO fields (crop, sow_date, area, soil_depth) VALUES (?, ?, ?, ?)",
                      (crop, sow_date, area, soil_depth))
        conn.commit()
        conn.close()
        return redirect(url_for('set_field', lang=lang))

    return render_template_string(INDEX_HTML,
                                  t=lambda key: t(key, lang),
                                  crops=CROP_PROFILES,
                                  current_crop=crop,
                                  sow_date=sow_date,
                                  area=area,
                                  soil_depth=soil_depth,
                                  lang=lang)

# ------------------- Run Flask (only local) -------------------
if __name__ == '__main__':
    app.run(debug=True)
