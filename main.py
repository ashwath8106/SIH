import logging
import sqlite3
from datetime import datetime, date
import requests
from flask import Flask, request, jsonify, render_template_string, redirect, url_for

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
DB_PATH = 'plant_watering.db'

# --------------------------- CROP PROFILES ---------------------------
CROP_PROFILES = {
    'banana': {
        'display': 'Banana',
        'stages': [
            {'name': 'Initial', 'days': 30, 'kc': 0.6},
            {'name': 'Development', 'days': 90, 'kc': 0.9},
            {'name': 'Mid-Season', 'days': 150, 'kc': 1.05},
            {'name': 'Late-Season', 'days': 60, 'kc': 0.8},
        ]
    },
    'paddy': {
        'display': 'Paddy (Rice)',
        'stages': [
            {'name': 'Initial', 'days': 25, 'kc': 0.7},
            {'name': 'Development', 'days': 35, 'kc': 1.05},
            {'name': 'Mid-Season', 'days': 50, 'kc': 1.2},
            {'name': 'Late-Season', 'days': 30, 'kc': 0.9},
        ]
    },
    'maize': {
        'display': 'Maize',
        'stages': [
            {'name': 'Initial', 'days': 20, 'kc': 0.45},
            {'name': 'Development', 'days': 40, 'kc': 0.85},
            {'name': 'Mid-Season', 'days': 45, 'kc': 1.15},
            {'name': 'Late-Season', 'days': 25, 'kc': 0.8},
        ]
    },
    'potato': {
        'display': 'Potato',
        'stages': [
            {'name': 'Initial', 'days': 15, 'kc': 0.7},
            {'name': 'Development', 'days': 45, 'kc': 1.05},
            {'name': 'Mid-Season', 'days': 30, 'kc': 1.0},
            {'name': 'Late-Season', 'days': 20, 'kc': 0.8},
        ]
    },
    'cardamom': {
        'display': 'Cardamom',
        'stages': [
            {'name': 'Initial', 'days': 30, 'kc': 0.6},
            {'name': 'Development', 'days': 60, 'kc': 0.85},
            {'name': 'Mid-Season', 'days': 120, 'kc': 1.0},
            {'name': 'Late-Season', 'days': 60, 'kc': 0.85},
        ]
    }
}

# --------------------------- MULTILINGUAL ---------------------------
TRANSLATIONS = {
    'en': {'title': 'Smart Irrigation Dashboard', 'choose_crop': 'Choose Crop', 'sow_date': 'Sowing Date',
           'submit': 'Submit', 'lang': 'Language'},
    'hi': {'title': 'स्मार्ट सिंचाई डैशबोर्ड', 'choose_crop': 'फसल चुनें', 'sow_date': 'बुवाई की तारीख',
           'submit': 'जमा करें', 'lang': 'भाषा'},
    'si': {'title': 'Smart Irrigation Dashboard (Sikkimese)', 'choose_crop': 'Crop चुन्नुहोस्',
           'sow_date': 'रोपाइ मिति', 'submit': 'पेश गर्नुहोस्', 'lang': 'भाषा'},
    'ta': {'title': 'அறிவார்ந்த நீர்ப்பாசன கட்டுப்பாடு', 'choose_crop': 'பயிர் தேர்ந்தெடுக்கவும்',
           'sow_date': 'விதைத்த தேதி', 'submit': 'சமர்ப்பி', 'lang': 'மொழி'}
}

def t(key, lang='en'):
    return TRANSLATIONS.get(lang, TRANSLATIONS['en']).get(key, key)

# --------------------------- DB SETUP ---------------------------
def connect_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

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

# ------------------------ HELPER FUNCTIONS ------------------------
def calculate_dynamic_kc_for_crop(crop_key, sow_date, current_date=None):
    if crop_key not in CROP_PROFILES:
        crop_key = 'maize'
    if current_date is None:
        current_date = datetime.now().date()
    days_elapsed = (current_date - sow_date).days
    if days_elapsed < 0:
        days_elapsed = 0
    profile = CROP_PROFILES[crop_key]
    cumulative = 0
    for stage in profile['stages']:
        cumulative += stage['days']
        if days_elapsed <= cumulative:
            return stage['kc'], stage['name'], days_elapsed
    last = profile['stages'][-1]
    return last['kc'], last['name'], days_elapsed

def get_et0_from_openmeteo(latitude, longitude):
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&hourly=et0_fao_evapotranspiration&timezone=auto"
        response = requests.get(url, timeout=5)
        data = response.json()
        return float(data['hourly']['et0_fao_evapotranspiration'][0])
    except Exception:
        return 3.0

def get_daily_weather_forecast(latitude, longitude):
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&daily=precipitation_sum,temperature_2m_max&timezone=auto"
        response = requests.get(url, timeout=5)
        return response.json().get('daily', {})
    except Exception:
        return {}

def get_sensor_data():
    conn = connect_db()
    row = conn.execute('SELECT soil_moisture, temperature, humidity, timestamp FROM sensors ORDER BY timestamp DESC LIMIT 1').fetchone()
    conn.close()
    return dict(row) if row else {'soil_moisture': 0, 'temperature': 0, 'humidity': 0, 'timestamp': None}

def get_latest_tank_level():
    conn = connect_db()
    row = conn.execute('SELECT level_percent, timestamp FROM tank_levels ORDER BY timestamp DESC LIMIT 1').fetchone()
    conn.close()
    return dict(row) if row else {'level_percent': None, 'timestamp': None}

def calculate_water_amount(soil_moisture, area, soil_depth, predicted_rainfall, et0, kc, humidity, field_capacity=30):
    etc = et0 * kc
    soil_depth_mm = soil_depth * 1000
    current_moisture_mm = (soil_moisture / 100.0) * soil_depth_mm
    water_deficit_mm = max(0.0, etc - current_moisture_mm / area - predicted_rainfall)
    if humidity < 30:
        water_deficit_mm *= 1.2
    elif humidity > 80:
        water_deficit_mm *= 0.9
    return max(0, int(round(water_deficit_mm * area)))

# ------------------------- ROUTES -------------------------
@app.route('/api/watering_decision', methods=['GET'])
def watering_decision():
    lang = request.args.get('lang', 'en')
    sensor = get_sensor_data()
    soil = sensor['soil_moisture']
    humidity = sensor['humidity']

    conn = connect_db()
    row = conn.execute("SELECT crop, sow_date, area, soil_depth FROM fields LIMIT 1").fetchone()
    conn.close()

    crop = row['crop'] if row else 'banana'
    sow_date = row['sow_date'] if row else str(date.today())
    sow_date = datetime.strptime(sow_date, '%Y-%m-%d').date() if isinstance(sow_date, str) else sow_date
    area = row['area'] if row else 10
    soil_depth = row['soil_depth'] if row else 0.2

    lat, lon = 27.2, 88.03
    weather = get_daily_weather_forecast(lat, lon)
    et0 = get_et0_from_openmeteo(lat, lon)
    kc, stage_name, days_elapsed = calculate_dynamic_kc_for_crop(crop, sow_date)

    predicted_rain = float(weather.get('precipitation_sum', [0, 0])[1])
    water_amount = calculate_water_amount(soil, area, soil_depth, predicted_rain, et0, kc, humidity)

    result = {
        'crop': crop,
        'stage': stage_name,
        'kc': kc,
        'et0': et0,
        'soil_moisture': soil,
        'humidity': humidity,
        'rain_forecast': predicted_rain,
        'water_amount_liters': water_amount
    }

    return jsonify(result)

INDEX_HTML = """
<!doctype html>
<html>
  <head><title>{{ t('title') }}</title></head>
  <body>
    <h1>{{ t('title') }}</h1>
    <form method="post">
      <label>{{ t('choose_crop') }}:
        <select name="crop">
          {% for key, profile in crops.items() %}
            <option value="{{ key }}" {% if current_crop == key %}selected{% endif %}>{{ profile.display }}</option>
          {% endfor %}
        </select></label><br/>
      <label>{{ t('sow_date') }}: <input type="date" name="sow_date" value="{{ sow_date }}"></label><br/>
      <label>Area (m²): <input name="area" type="number" step="0.1" value="{{ area }}"></label><br/>
      <label>Soil Depth (m): <input name="soil_depth" type="number" step="0.01" value="{{ soil_depth }}"></label><br/>
      <label>{{ t('lang') }}:
        <select name="lang">
          {% for code in ['en','hi','si','ta'] %}
            <option value="{{code}}" {% if lang==code %}selected{% endif %}>{{ code }}</option>
          {% endfor %}
        </select></label><br/>
      <button type="submit">{{ t('submit') }}</button>
    </form>
  </body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def set_field():
    lang = request.form.get('lang', request.args.get('lang', 'en'))
    conn = connect_db()
    row = conn.execute("SELECT crop, sow_date, area, soil_depth FROM fields LIMIT 1").fetchone()
    crop = row['crop'] if row else 'banana'
    sow_date = row['sow_date'] if row else str(date.today())
    area = row['area'] if row else 10
    soil_depth = row['soil_depth'] if row else 0.2

    if request.method == 'POST':
        crop = request.form['crop']
        sow_date = request.form['sow_date']
        area = float(request.form['area'])
        soil_depth = float(request.form['soil_depth'])

        conn = connect_db()
        exists = conn.e
