from flask import Flask, render_template, request, redirect, session, jsonify, send_file
import sqlite3
import datetime
import io
import os

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors

# ── Arduino (graceful fallback) ─────────────────────────────────────────────
try:
    import serial
    import time
    arduino = serial.Serial('COM7', 9600, timeout=1)
    time.sleep(2)
    ARDUINO_CONNECTED = True
except Exception:
    arduino = None
    ARDUINO_CONNECTED = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gracehealth-secret-2024-xK9#mP")

ADMIN_EMAIL    = "admin@gracehealth.com"
ADMIN_PASSWORD = "Admin@1234"


# ════════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur  = conn.cursor()

    # Users — female only, no sex field needed
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL,
            age      INTEGER NOT NULL,
            weight   REAL    NOT NULL,
            height   REAL    NOT NULL,
            email    TEXT    NOT NULL UNIQUE,
            phone    TEXT    DEFAULT '',
            selected_avatar TEXT DEFAULT '',
            password TEXT    NOT NULL
        )
    """)

    # BMR + sensor history
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT,
            bpm               TEXT DEFAULT '—',
            spo2              TEXT DEFAULT '—',
            temperature       TEXT DEFAULT '—',
            bmr               REAL,
            time              TEXT,
            report_downloaded INTEGER DEFAULT 0
        )
    """)

    # One-time signup period health assessment (10 questions)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS period_health_assessment (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email    TEXT NOT NULL UNIQUE,
            q1       INTEGER, q2  INTEGER, q3  INTEGER, q4  INTEGER, q5  INTEGER,
            q6       INTEGER, q7  INTEGER, q8  INTEGER, q9  INTEGER, q10 INTEGER,
            score    INTEGER,
            analysis TEXT,
            date     TEXT
        )
    """)

    # Period cycles tracking
    cur.execute("""
        CREATE TABLE IF NOT EXISTS period_cycles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT,
            start_date  TEXT,
            end_date    TEXT,
            avg_score   REAL,
            worst_day   INTEGER,
            worst_score INTEGER,
            best_day    INTEGER,
            best_score  INTEGER,
            overall_label TEXT
        )
    """)

    # Daily symptom logs (5 questions per day during period)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_symptoms (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT,
            cycle_id   INTEGER,
            date       TEXT,
            day_number INTEGER,
            q1         INTEGER, q2 INTEGER, q3 INTEGER, q4 INTEGER, q5 INTEGER,
            score      INTEGER,
            analysis   TEXT,
            tip        TEXT,
            notes      TEXT,
            pain_relief INTEGER DEFAULT 0,
            flow        TEXT    DEFAULT 'medium',
            iron_taken  INTEGER DEFAULT 0,
            water       INTEGER DEFAULT 0,
            sleep_q     TEXT    DEFAULT 'fair',
            exercise    TEXT    DEFAULT 'none'
        )
    """)

    # Doctor visit tracker
    cur.execute("""
        CREATE TABLE IF NOT EXISTS doctor_visits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT,
            visit_date  TEXT,
            diagnosis   TEXT,
            medication  TEXT,
            next_appt   TEXT,
            notes       TEXT
        )
    """)

    # Medication tracker
    cur.execute("""
        CREATE TABLE IF NOT EXISTS medication_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT,
            cycle_id     INTEGER,
            date         TEXT,
            pain_relief  INTEGER DEFAULT 0,
            iron_supp    INTEGER DEFAULT 0,
            vitamin_d    INTEGER DEFAULT 0,
            other        TEXT
        )
    """)

    # PMS log (pre-period tracking)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pms_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            email     TEXT,
            date      TEXT,
            mood      INTEGER,
            bloating  INTEGER,
            headache  INTEGER,
            notes     TEXT
        )
    """)

    # Safe column migrations for existing DBs
    migrations = [
        ("history",   "report_downloaded", "INTEGER DEFAULT 0"),
        ("history",   "bpm",               "TEXT DEFAULT '—'"),
        ("history",   "spo2",              "TEXT DEFAULT '—'"),
        ("history",   "temperature",       "TEXT DEFAULT '—'"),
        ("users",     "age",               "INTEGER"),
        ("users",     "phone",             "TEXT DEFAULT ''"),
        ("users",     "selected_avatar",   "TEXT DEFAULT ''"),
        ("daily_symptoms", "flow",         "TEXT DEFAULT 'medium'"),
        ("daily_symptoms", "iron_taken",   "INTEGER DEFAULT 0"),
        ("daily_symptoms", "water",        "INTEGER DEFAULT 0"),
        ("daily_symptoms", "sleep_q",      "TEXT DEFAULT 'fair'"),
        ("daily_symptoms", "exercise",     "TEXT DEFAULT 'none'"),
    ]
    for table, col, coltype in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            conn.commit()
        except Exception:
            pass

    # Remove sex column gracefully — handled by always setting 'female' in code
    conn.commit()
    conn.close()


init_db()


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════
def parse_sensor(raw):
    """Parse Arduino sensor line -> dict.

    Arduino sends CSV: bpm,spo2,temperature
    with 'NULL' for invalid values, e.g. 'NULL,NULL,36.5' or '75.3,98.1,36.6'
    """
    if not raw:
        return {'bpm': '—', 'spo2': '—', 'temperature': '—', 'valid': False, 'raw': raw}

    def safe_float(s):
        s = s.strip()
        if not s or s.upper() == 'NULL' or s == '—':
            return None
        try:
            return round(float(s), 1)
        except (ValueError, TypeError):
            return None

    # Strategy 1: CSV — e.g. "75.3,98.1,36.6" or "NULL,NULL,36.5"
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) >= 3:
        bpm  = safe_float(parts[0])
        spo2 = safe_float(parts[1])
        temp = safe_float(parts[2])
        if bpm is not None or spo2 is not None or temp is not None:
            return {
                'bpm':         str(bpm)  if bpm  is not None else '—',
                'spo2':        str(spo2) if spo2 is not None else '—',
                'temperature': str(temp) if temp is not None else '—',
                'valid':       True,
                'raw':         raw
            }

    return {'bpm': '—', 'spo2': '—', 'temperature': '—', 'valid': False, 'raw': raw}



def read_arduino_line(retries=3):
    """Read one valid CSV line from Arduino.

    Does NOT flush the buffer — reads whatever is already waiting first,
    which is usually a fresh line the Arduino sent in the last second.
    """
    if not ARDUINO_CONNECTED or not arduino:
        return {'bpm': '—', 'spo2': '—', 'temperature': '—', 'valid': False, 'raw': ''}
    try:
        last_raw = ''
        for i in range(retries):
            raw = arduino.readline().decode(errors='ignore').strip()
            if raw:
                last_raw = raw
                result = parse_sensor(raw)
                if result['valid']:
                    print(f"  [ARDUINO] ✅ {raw!r}  → BPM={result['bpm']} SpO2={result['spo2']} Temp={result['temperature']}")
                    return result
                print(f"  [ARDUINO] 📡 {raw!r}  (not valid CSV)")
        return parse_sensor(last_raw) if last_raw else {'bpm': '—', 'spo2': '—', 'temperature': '—', 'valid': False, 'raw': ''}
    except Exception as e:
        print(f"  [ARDUINO] ❌ Error: {e}")
        return {'bpm': '—', 'spo2': '—', 'temperature': '—', 'valid': False, 'raw': str(e)}


def vitals_status(bpm, spo2, temp):
    """Return overall health status string based on vitals."""
    try:
        b = float(bpm)
        s = float(spo2)
        t = float(temp)
        if s < 94 or b > 110 or b < 50 or t > 38.0:
            return 'warning'
        if s >= 97 and 60 <= b <= 100 and 36.1 <= t <= 37.5:
            return 'good'
        return 'normal'
    except Exception:
        return 'unknown'


def calculate_bmr(weight, height, age):
    """Female-only Mifflin-St Jeor formula."""
    return 10 * weight + 6.25 * height - 5 * age - 161


def get_bmr_status(bmr):
    if bmr < 1200:  return "low"
    if bmr > 2500:  return "high"
    return "normal"


AVATAR_OPTIONS = [
    "https://cdn-icons-png.flaticon.com/512/6997/6997662.png",  # child / young girl
    "https://cdn-icons-png.flaticon.com/512/6997/6997665.png",  # teenage girl
    "https://cdn-icons-png.flaticon.com/512/4140/4140047.png",  # young woman
    "https://cdn-icons-png.flaticon.com/512/4140/4140060.png",  # adult woman
    "https://cdn-icons-png.flaticon.com/512/4140/4140061.png",  # middle-aged woman
    "https://cdn-icons-png.flaticon.com/512/4140/4140063.png",  # senior woman
]


def get_avatar(age, selected=None):
    """Return avatar URL — respects user's chosen avatar if set."""
    if selected and selected.strip():
        return selected
    base = "https://cdn-icons-png.flaticon.com/512"
    if age <= 14:
        return f"{base}/6997/6997662.png"   # young girl / child
    elif age <= 17:
        return f"{base}/6997/6997665.png"   # teenage girl
    elif age <= 25:
        return f"{base}/4140/4140047.png"   # young adult woman
    elif age <= 35:
        return f"{base}/4140/4140060.png"   # adult woman
    elif age <= 50:
        return f"{base}/4140/4140061.png"   # middle-aged woman
    else:
        return f"{base}/4140/4140063.png"   # senior woman


def get_assessment_analysis(score):
    if score <= 8:
        return "Normal menstrual symptoms. Maintain a healthy diet and rest well."
    elif score <= 16:
        return "Moderate PMS symptoms detected. Monitor your cycle and maintain good nutrition."
    elif score <= 24:
        return "Possible severe PMS or hormonal imbalance. Consider consulting a healthcare professional."
    else:
        return "Significant menstrual discomfort detected. We recommend a medical consultation."


def get_daily_analysis(score):
    if score <= 4:
        return ("Mild", "Your symptoms are mild today. Great day for light exercise and staying hydrated. 🧘")
    elif score <= 9:
        return ("Moderate", "Take it easy today. Rest well and try a warm compress for cramps. 🌿")
    elif score <= 12:
        return ("Severe", "Difficult day. Rest, eat iron-rich foods and stay well hydrated. 🥦")
    else:
        return ("Critical", "Severe symptoms today. Please consider seeing a doctor. 🏥")


def get_active_cycle(email):
    """Returns the current open cycle if one exists (no end_date)."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT * FROM period_cycles
        WHERE email = ? AND (end_date IS NULL OR end_date = '')
        ORDER BY id DESC LIMIT 1
    """, (email,))
    cycle = cur.fetchone()
    conn.close()
    return cycle


def get_cycle_day(start_date_str):
    """Returns how many days into the current cycle."""
    try:
        start = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
        today = datetime.date.today()
        return (today - start).days + 1
    except Exception:
        return 1


def predict_next_period(email):
    """Predict next period based on past cycle lengths."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT start_date FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL AND end_date != ''
        ORDER BY id DESC LIMIT 6
    """, (email,))
    cycles = cur.fetchall()
    conn.close()

    if len(cycles) < 2:
        return None, None

    starts = []
    for c in cycles:
        try:
            starts.append(datetime.datetime.strptime(c['start_date'], "%Y-%m-%d").date())
        except Exception:
            pass

    if len(starts) < 2:
        return None, None

    lengths = [(starts[i] - starts[i+1]).days for i in range(len(starts)-1)]
    avg_length = round(sum(lengths) / len(lengths))
    next_period = starts[0] + datetime.timedelta(days=avg_length)
    return next_period.strftime("%B %d, %Y"), avg_length


def check_symptom_pattern_warning(email):
    """Check if user consistently has severe symptoms on Day 2-3 across cycles."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT day_number, score FROM daily_symptoms
        WHERE email = ? AND day_number IN (2, 3)
        ORDER BY id DESC LIMIT 10
    """, (email,))
    rows = cur.fetchall()
    conn.close()

    if len(rows) < 4:
        return None

    severe = [r for r in rows if r['score'] >= 10]
    if len(severe) >= 4:
        return "⚠️ You consistently experience severe symptoms on Day 2–3 of your cycle. Consider consulting a healthcare professional."
    return None


def has_logged_today(email, cycle_id):
    """Check if user already logged symptoms today."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    conn  = get_db()
    cur   = conn.cursor()
    cur.execute("""
        SELECT id FROM daily_symptoms
        WHERE email = ? AND cycle_id = ? AND date = ?
    """, (email, cycle_id, today))
    result = cur.fetchone()
    conn.close()
    return result is not None


def get_calendar_data(email):
    """Returns period days and predicted window for calendar rendering."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        SELECT start_date, end_date FROM period_cycles
        WHERE email = ? ORDER BY id DESC LIMIT 6
    """, (email,))
    cycles = cur.fetchall()

    cur.execute("""
        SELECT date FROM daily_symptoms WHERE email = ?
    """, (email,))
    logged_dates = [r['date'] for r in cur.fetchall()]

    conn.close()

    period_days = []
    for c in cycles:
        if c['start_date']:
            try:
                start = datetime.datetime.strptime(c['start_date'], "%Y-%m-%d").date()
                end   = datetime.datetime.strptime(c['end_date'], "%Y-%m-%d").date() \
                        if c['end_date'] else datetime.date.today()
                d = start
                while d <= end:
                    period_days.append(d.strftime("%Y-%m-%d"))
                    d += datetime.timedelta(days=1)
            except Exception:
                pass

    return period_days, logged_dates


def get_cycle_history(email):
    """Returns past completed cycles with summary."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT * FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL AND end_date != ''
        ORDER BY id DESC LIMIT 6
    """, (email,))
    cycles = cur.fetchall()
    conn.close()
    return cycles


def get_health_score_progress(email):
    """Compare signup assessment score vs latest cycle avg."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT score FROM period_health_assessment WHERE email = ?", (email,))
    assessment = cur.fetchone()

    cur.execute("""
        SELECT avg_score FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """, (email,))
    latest = cur.fetchone()

    conn.close()

    if not assessment or not latest:
        return None

    signup_score = assessment['score']
    latest_score = latest['avg_score']
    diff = latest_score - signup_score

    if diff < -2:
        return {"trend": "improving", "signup": signup_score, "latest": round(latest_score, 1),
                "message": "You're improving! Your symptoms are getting better. 📈"}
    elif diff > 2:
        return {"trend": "worsening", "signup": signup_score, "latest": round(latest_score, 1),
                "message": "Your symptoms have increased since signup. Consider a checkup. 📉"}
    else:
        return {"trend": "stable", "signup": signup_score, "latest": round(latest_score, 1),
                "message": "Your symptoms are stable compared to your initial assessment. ➡️"}



def get_cycle_intelligence(email):
    """Returns advanced cycle length stats."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT start_date FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL
        ORDER BY id DESC LIMIT 12
    """, (email,))
    cycles = cur.fetchall()
    conn.close()

    if len(cycles) < 2:
        return None

    starts = []
    for c in cycles:
        try:
            starts.append(datetime.datetime.strptime(c['start_date'], "%Y-%m-%d").date())
        except Exception:
            pass

    if len(starts) < 2:
        return None

    lengths = [(starts[i] - starts[i+1]).days for i in range(len(starts)-1)]
    avg = round(sum(lengths) / len(lengths))
    shortest = min(lengths)
    longest  = max(lengths)
    variation = longest - shortest
    irregular = variation > 7

    return {
        "avg": avg,
        "shortest": shortest,
        "longest": longest,
        "variation": variation,
        "irregular": irregular,
        "count": len(starts)
    }


def get_current_phase(email):
    """Returns current menstrual phase based on cycle data."""
    active = get_active_cycle(email)
    if active:
        day = get_cycle_day(active['start_date'])
        if day <= 5:
            return {"phase": "Menstruation", "day": day, "color": "#c2185b",
                    "icon": "🔴", "tip": "Your body is shedding. Rest, stay hydrated, eat iron-rich foods.",
                    "energy": "Low"}
        elif day <= 13:
            return {"phase": "Follicular", "day": day, "color": "#f57f17",
                    "icon": "🌱", "tip": "Energy is rising! Great time for light exercise and staying active.",
                    "energy": "Building"}
        elif day <= 16:
            return {"phase": "Ovulation", "day": day, "color": "#2e7d32",
                    "icon": "✨", "tip": "Peak energy and mood! Ideal for physical activity and social plans.",
                    "energy": "Peak"}
        else:
            return {"phase": "Luteal", "day": day, "color": "#6a1b9a",
                    "icon": "🌙", "tip": "PMS window. Watch for mood changes and bloating. Be kind to yourself.",
                    "energy": "Declining"}

    # Not in period — figure out phase from last cycle
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT end_date FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """, (email,))
    last = cur.fetchone()
    conn.close()

    if not last:
        return None

    try:
        end  = datetime.datetime.strptime(last['end_date'], "%Y-%m-%d").date()
        today = datetime.date.today()
        days_since = (today - end).days
        if days_since <= 8:
            return {"phase": "Follicular", "day": days_since, "color": "#f57f17",
                    "icon": "🌱", "tip": "Post-period energy boost! Great time for exercise.",
                    "energy": "Building"}
        elif days_since <= 11:
            return {"phase": "Ovulation", "day": days_since, "color": "#2e7d32",
                    "icon": "✨", "tip": "Peak energy window. Make the most of it!",
                    "energy": "Peak"}
        else:
            return {"phase": "Luteal", "day": days_since, "color": "#6a1b9a",
                    "icon": "🌙", "tip": "PMS window approaching. Rest more, limit caffeine.",
                    "energy": "Declining"}
    except Exception:
        return None


def get_smart_notifications(email):
    """Returns contextual smart notifications for the dashboard."""
    notes = []
    today = datetime.date.today()

    # Prediction-based warning
    next_p, avg_len = predict_next_period(email)
    if next_p:
        try:
            next_date = datetime.datetime.strptime(next_p, "%B %d, %Y").date()
            days_away = (next_date - today).days
            if days_away == 3:
                notes.append({"type": "warning", "icon": "🌸",
                    "msg": "Your period is expected in 3 days. Start iron-rich foods and keep a heating pad ready."})
            elif days_away == 1:
                notes.append({"type": "warning", "icon": "⚠️",
                    "msg": "Your period is expected tomorrow. Rest well tonight and prepare your essentials."})
            elif 0 <= days_away <= 5:
                notes.append({"type": "warning", "icon": "🔔",
                    "msg": f"Your period is expected in {days_away} days. PMS symptoms may begin soon."})
        except Exception:
            pass

    # Cycle count milestone
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM period_cycles WHERE email = ? AND end_date IS NOT NULL", (email,))
    row = cur.fetchone()
    cycle_count = row['cnt'] if row else 0

    if cycle_count == 3:
        notes.append({"type": "success", "icon": "🎉",
            "msg": "You've tracked 3 complete cycles! Your predictions are now more accurate."})
    elif cycle_count == 1:
        notes.append({"type": "info", "icon": "💡",
            "msg": "First cycle tracked! Log 2 more to unlock period predictions."})

    # Pattern-based tip
    cur.execute("""
        SELECT day_number, score FROM daily_symptoms
        WHERE email = ? ORDER BY id DESC LIMIT 15
    """, (email,))
    logs = cur.fetchall()

    if logs:
        day3_scores = [l['score'] for l in logs if l['day_number'] == 3]
        if day3_scores and sum(day3_scores)/len(day3_scores) >= 9:
            notes.append({"type": "tip", "icon": "💊",
                "msg": "Day 3 is usually your hardest day. Keep pain relief ready and plan a lighter schedule."})

    # Progress
    cur.execute("SELECT score FROM period_health_assessment WHERE email = ?", (email,))
    assess = cur.fetchone()
    cur.execute("""
        SELECT avg_score FROM period_cycles WHERE email = ?
        AND end_date IS NOT NULL ORDER BY id DESC LIMIT 1
    """, (email,))
    last_cyc = cur.fetchone()

    if assess and last_cyc:
        diff = last_cyc['avg_score'] - assess['score']
        if diff < -3:
            notes.append({"type": "success", "icon": "📈",
                "msg": "Your symptoms have improved compared to your initial assessment. Great progress!"})
        elif diff > 3:
            notes.append({"type": "warning", "icon": "📉",
                "msg": "Your recent cycle symptoms are higher than your baseline. Consider a health check."})

    conn.close()
    return notes


def get_trends_data(email):
    """Returns chart data for health trends."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        SELECT start_date, avg_score, overall_label FROM period_cycles
        WHERE email = ? AND end_date IS NOT NULL
        ORDER BY id ASC LIMIT 6
    """, (email,))
    cycles = cur.fetchall()

    # Per-symptom averages across all logs
    cur.execute("""
        SELECT AVG(q1) as c, AVG(q2) as f, AVG(q3) as b,
               AVG(q4) as m, AVG(q5) as i
        FROM daily_symptoms WHERE email = ?
    """, (email,))
    sym = cur.fetchone()

    conn.close()

    cycle_labels = [c['start_date'] for c in cycles]
    cycle_scores = [round(c['avg_score'], 1) if c['avg_score'] else 0 for c in cycles]

    symptom_avgs = [0, 0, 0, 0, 0]
    if sym:
        symptom_avgs = [
            round(sym['c'] or 0, 1),
            round(sym['f'] or 0, 1),
            round(sym['b'] or 0, 1),
            round(sym['m'] or 0, 1),
            round(sym['i'] or 0, 1),
        ]

    return {
        "cycle_labels": cycle_labels,
        "cycle_scores": cycle_scores,
        "symptom_avgs": symptom_avgs
    }


# ════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════
@app.route('/')
def home():
    return redirect('/login')


# ── Login ────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == "POST":
        identifier = request.form.get('identifier', '').strip()
        password   = request.form.get('password', '')
        id_lower   = identifier.lower()

        # Admin check
        if id_lower == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session['admin'] = ADMIN_EMAIL
            return redirect('/admin')

        conn = get_db()
        cur  = conn.cursor()

        # Try email first, then phone
        cur.execute("SELECT * FROM users WHERE (email = ? OR phone = ?) AND password = ?",
                    (id_lower, identifier, password))
        user = cur.fetchone()
        conn.close()

        if user:
            session['user'] = user['email']   # always store email as session key
            return redirect('/dashboard')
        else:
            error = "Invalid email / phone number or password. Please try again."

    return render_template("login.html", error=error)


# ── Signup ───────────────────────────────────────────────────────────────────
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    error = None
    if request.method == "POST":
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        phone    = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')

        try:
            age    = int(request.form.get('age', 0))
            weight = float(request.form.get('weight', 0))
            height = float(request.form.get('height', 0))
        except ValueError:
            return render_template("signup.html", error="Please enter valid numbers.")

        if not name or not email or not password:
            error = "All fields are required."
        elif not phone:
            error = "Phone number is required."
        elif not phone.isdigit() or len(phone) != 10:
            error = "Please enter a valid 10-digit phone number."
        elif age < 10 or age > 80:
            error = "Please enter a valid age (10–80)."
        elif weight < 20 or weight > 200:
            error = "Please enter a valid weight (20–200 kg)."
        elif height < 50 or height > 220:
            error = "Please enter a valid height (50–220 cm)."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("SELECT id FROM users WHERE email = ?", (email,))
            if cur.fetchone():
                error = "An account with this email already exists."
                conn.close()
            elif phone:
                cur.execute("SELECT id FROM users WHERE phone = ?", (phone,))
                if cur.fetchone():
                    error = "An account with this phone number already exists."
                    conn.close()
                else:
                    cur.execute("""
                        INSERT INTO users (name, age, weight, height, email, phone, password)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (name, age, weight, height, email, phone, password))
                    conn.commit()
                    conn.close()
                    session['pending_assessment'] = email
                    return redirect('/assessment')
            else:
                cur.execute("""
                    INSERT INTO users (name, age, weight, height, email, phone, password)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (name, age, weight, height, email, '', password))
                conn.commit()
                conn.close()
                session['pending_assessment'] = email
                return redirect('/assessment')

    return render_template("signup.html", error=error)


# ── Period Health Assessment (10 questions — one time after signup) ──────────
@app.route('/assessment', methods=['GET', 'POST'])
def assessment():
    # Must come right after signup or be logged in
    email = session.get('pending_assessment') or session.get('user')
    if not email:
        return redirect('/login')

    if request.method == "POST":
        try:
            answers = [int(request.form.get(f'q{i}', 0)) for i in range(1, 11)]
        except ValueError:
            return render_template("assessment.html",
                                   error="Please answer all questions.")

        if len([a for a in answers if a >= 0]) < 10:
            return render_template("assessment.html",
                                   error="Please answer all 10 questions.")

        score    = sum(answers)
        analysis = get_assessment_analysis(score)
        date     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db()
        cur  = conn.cursor()
        # Upsert — replace if exists
        cur.execute("""
            INSERT OR REPLACE INTO period_health_assessment
            (email, q1, q2, q3, q4, q5, q6, q7, q8, q9, q10, score, analysis, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (email, *answers, score, analysis, date))
        conn.commit()
        conn.close()

        # Move from pending to active session
        if 'pending_assessment' in session:
            session['user'] = email
            session.pop('pending_assessment')

        return redirect('/dashboard')

    # GET — check if already done
    if session.get('user'):
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM period_health_assessment WHERE email = ?",
                    (session['user'],))
        done = cur.fetchone()
        conn.close()
        if done:
            return redirect('/dashboard')

    return render_template("assessment.html", error=None)


# ── Dashboard ────────────────────────────────────────────────────────────────
@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    conn  = get_db()
    cur   = conn.cursor()

    cur.execute("SELECT name, age, weight, height, email, phone FROM users WHERE email = ?", (email,))
    user = cur.fetchone()

    cur.execute("""
        SELECT bpm, spo2, temperature, bmr, time FROM history
        WHERE email = ? ORDER BY id DESC LIMIT 10
    """, (email,))
    history = cur.fetchall()

    sel_av       = user['selected_avatar'] if user and 'selected_avatar' in user.keys() and user['selected_avatar'] else ''
    avatar_url   = get_avatar(user['age'], sel_av) if user else get_avatar(20)
    anaemia_risk = get_anaemia_risk(email, conn)
    cycle_health = get_cycle_health_score(email, conn)
    streak       = get_streak(email, conn)
    missed_alert = get_missed_period_alert(email, conn)

    # Period health assessment result
    cur.execute("SELECT score, analysis FROM period_health_assessment WHERE email = ?", (email,))
    assessment = cur.fetchone()

    conn.close()

    # Active cycle info
    active_cycle   = get_active_cycle(email)
    cycle_day      = get_cycle_day(active_cycle['start_date']) if active_cycle else None
    already_logged = has_logged_today(email, active_cycle['id']) if active_cycle else False

    # Today's log if exists
    today_log = None
    if active_cycle:
        today = datetime.date.today().strftime("%Y-%m-%d")
        conn2 = get_db()
        cur2  = conn2.cursor()
        cur2.execute("""
            SELECT score, analysis, tip FROM daily_symptoms
            WHERE email = ? AND date = ?
        """, (email, today))
        today_log = cur2.fetchone()
        conn2.close()

    # Calendar data
    period_days, logged_dates = get_calendar_data(email)

    # Cycle history timeline
    cycle_history = get_cycle_history(email)

    # Prediction
    next_period, avg_cycle_len = predict_next_period(email)

    # Pattern warning
    pattern_warning = check_symptom_pattern_warning(email)

    # Health score progress
    progress = get_health_score_progress(email)

    # New features data
    cycle_intel       = get_cycle_intelligence(email)
    current_phase     = get_current_phase(email)
    notifications     = get_smart_notifications(email)
    trends            = get_trends_data(email)

    # Doctor visits
    conn3 = get_db()
    cur3  = conn3.cursor()
    cur3.execute("""
        SELECT * FROM doctor_visits WHERE email = ?
        ORDER BY visit_date DESC LIMIT 3
    """, (email,))
    doctor_visits = cur3.fetchall()

    # PMS logs (last 5)
    cur3.execute("""
        SELECT * FROM pms_log WHERE email = ?
        ORDER BY date DESC LIMIT 5
    """, (email,))
    pms_logs = cur3.fetchall()
    conn3.close()

    return render_template("dashboard.html",
        user           = user,
        avatar_url     = avatar_url,
        phone          = user['phone'] if user and user['phone'] else '',
        anaemia_risk   = anaemia_risk,
        cycle_health   = cycle_health,
        streak         = streak,
        missed_alert   = missed_alert,
        history        = history,
        arduino_status = ARDUINO_CONNECTED,
        assessment     = assessment,
        active_cycle   = active_cycle,
        cycle_day      = cycle_day,
        already_logged = already_logged,
        today_log      = today_log,
        period_days    = period_days,
        logged_dates   = logged_dates,
        cycle_history  = cycle_history,
        next_period    = next_period,
        avg_cycle_len  = avg_cycle_len,
        pattern_warning= pattern_warning,
        progress       = progress,
        cycle_intel    = cycle_intel,
        current_phase  = current_phase,
        notifications  = notifications,
        trends         = trends,
        doctor_visits  = doctor_visits,
        pms_logs       = pms_logs
    )


# ── Period Start ─────────────────────────────────────────────────────────────
@app.route('/period/start', methods=['POST'])
def period_start():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    email = session['user']

    # Don't create duplicate open cycle
    existing = get_active_cycle(email)
    if existing:
        return jsonify({"error": "Period already active"}), 400

    today = datetime.date.today().strftime("%Y-%m-%d")
    conn  = get_db()
    cur   = conn.cursor()
    cur.execute("""
        INSERT INTO period_cycles (email, start_date)
        VALUES (?, ?)
    """, (email, today))
    conn.commit()
    cycle_id = cur.lastrowid
    conn.close()

    return jsonify({"success": True, "cycle_id": cycle_id, "start_date": today})


# ── Period End ───────────────────────────────────────────────────────────────
@app.route('/period/end', methods=['POST'])
def period_end():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    email = session['user']
    cycle = get_active_cycle(email)

    if not cycle:
        return jsonify({"error": "No active period found"}), 400

    today = datetime.date.today().strftime("%Y-%m-%d")

    # Calculate cycle summary
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT day_number, score FROM daily_symptoms
        WHERE email = ? AND cycle_id = ?
        ORDER BY day_number
    """, (email, cycle['id']))
    logs = cur.fetchall()

    avg_score   = round(sum(l['score'] for l in logs) / len(logs), 1) if logs else 0
    worst       = max(logs, key=lambda x: x['score']) if logs else None
    best        = min(logs, key=lambda x: x['score']) if logs else None
    worst_day   = worst['day_number'] if worst else None
    worst_score = worst['score']     if worst else None
    best_day    = best['day_number'] if best  else None
    best_score  = best['score']      if best  else None

    _, label = get_daily_analysis(avg_score) if avg_score else (None, "No data")

    cur.execute("""
        UPDATE period_cycles
        SET end_date=?, avg_score=?, worst_day=?, worst_score=?,
            best_day=?, best_score=?, overall_label=?
        WHERE id=?
    """, (today, avg_score, worst_day, worst_score,
          best_day, best_score, label, cycle['id']))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "end_date": today, "avg_score": avg_score})


# ── Daily Symptom Log ────────────────────────────────────────────────────────
@app.route('/daily_log', methods=['GET', 'POST'])
def daily_log():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    cycle = get_active_cycle(email)

    if not cycle:
        return redirect('/dashboard')

    # Block duplicate log
    if has_logged_today(email, cycle['id']):
        return redirect('/dashboard')

    if request.method == 'POST':
        try:
            answers = [int(request.form.get(f'q{i}', 0)) for i in range(1, 6)]
        except ValueError:
            return render_template("daily_log.html", error="Please answer all questions.", cycle=cycle)

        score      = sum(answers)
        label, tip = get_daily_analysis(score)
        today      = datetime.date.today().strftime("%Y-%m-%d")
        day_number = get_cycle_day(cycle['start_date'])
        notes      = request.form.get('notes', '').strip()[:200]
        pain_relief= 1 if request.form.get('pain_relief') == 'yes' else 0
        flow       = request.form.get('flow', 'medium')
        iron_taken = 1 if request.form.get('iron_taken') == 'yes' else 0
        water      = int(request.form.get('water', 0))
        sleep_q    = request.form.get('sleep_q', 'fair')
        exercise   = request.form.get('exercise', 'none')

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO daily_symptoms
            (email, cycle_id, date, day_number, q1, q2, q3, q4, q5,
             score, analysis, tip, notes, pain_relief,
             flow, iron_taken, water, sleep_q, exercise)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (email, cycle['id'], today, day_number,
              *answers, score, label, tip, notes, pain_relief,
              flow, iron_taken, water, sleep_q, exercise))
        conn.commit()
        conn.close()

        return redirect('/dashboard')

    day_number = get_cycle_day(cycle['start_date'])
    return render_template("daily_log.html", error=None, cycle=cycle, day_number=day_number)


# ── Sensor API ───────────────────────────────────────────────────────────────
# ── Health Intelligence Helpers ──────────────────────────────────────────────
def get_anaemia_risk(email, conn):
    """0-100 anaemia risk score from SpO2 + bleeding + fatigue across last cycle."""
    cur = conn.cursor()
    cur.execute("""SELECT q2, q3 FROM daily_symptoms
                   WHERE email=? ORDER BY date DESC LIMIT 7""", (email,))
    logs = cur.fetchall()
    cur.execute("""SELECT spo2 FROM history
                   WHERE email=? AND spo2 != '—' ORDER BY time DESC LIMIT 5""", (email,))
    vitals = cur.fetchall()
    if not logs and not vitals:
        return None

    bleed_avg  = sum(l['q3'] for l in logs) / len(logs) * 33 if logs else 0
    fatigue_avg= sum(l['q2'] for l in logs) / len(logs) * 20 if logs else 0
    spo2_vals  = [float(v['spo2']) for v in vitals if v['spo2'] not in ('—','')]
    spo2_score = 0
    if spo2_vals:
        avg_spo2 = sum(spo2_vals) / len(spo2_vals)
        spo2_score = max(0, (100 - avg_spo2) * 5)  # 95%=25pts, 90%=50pts

    risk = min(100, round(bleed_avg + fatigue_avg + spo2_score))
    return risk


def get_cycle_health_score(email, conn):
    """0-100 overall health score for current/last cycle."""
    cur = conn.cursor()
    cur.execute("""SELECT score FROM daily_symptoms
                   WHERE email=? ORDER BY date DESC LIMIT 7""", (email,))
    logs = cur.fetchall()
    cur.execute("""SELECT bpm, spo2, temperature FROM history
                   WHERE email=? ORDER BY time DESC LIMIT 3""", (email,))
    vitals = cur.fetchall()
    if not logs:
        return None

    sym_avg = sum(l['score'] for l in logs) / len(logs)
    sym_score = max(0, 100 - (sym_avg / 15 * 60))  # 0 symptoms=100, max=40

    vital_score = 70  # default if no vitals
    if vitals:
        bpms  = [float(v['bpm'])  for v in vitals if v['bpm']  not in ('—','')]
        spo2s = [float(v['spo2']) for v in vitals if v['spo2'] not in ('—','')]
        bpm_ok  = all(60 <= b <= 100 for b in bpms)  if bpms  else True
        spo2_ok = all(s >= 94        for s in spo2s) if spo2s else True
        vital_score = 100 if (bpm_ok and spo2_ok) else 55

    return round(sym_score * 0.6 + vital_score * 0.4)


def get_streak(email, conn):
    """Consecutive days logged."""
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT date FROM daily_symptoms
                   WHERE email=? ORDER BY date DESC LIMIT 30""", (email,))
    dates = [r['date'] for r in cur.fetchall()]
    if not dates:
        return 0
    streak = 1
    for i in range(1, len(dates)):
        d1 = datetime.date.fromisoformat(dates[i-1])
        d2 = datetime.date.fromisoformat(dates[i])
        if (d1 - d2).days == 1:
            streak += 1
        else:
            break
    return streak


def get_missed_period_alert(email, conn):
    """Returns days overdue if period is 5+ days late."""
    cur = conn.cursor()
    cur.execute("""SELECT start_date FROM period_cycles
                   WHERE email=? AND end_date IS NOT NULL
                   ORDER BY start_date DESC LIMIT 6""", (email,))
    cycles = [r['start_date'] for r in cur.fetchall()]
    if len(cycles) < 2:
        return None
    cur.execute("""SELECT id FROM period_cycles WHERE email=? AND end_date IS NULL""", (email,))
    if cur.fetchone():
        return None   # period is currently active
    gaps = [(datetime.date.fromisoformat(cycles[i]) -
             datetime.date.fromisoformat(cycles[i+1])).days
            for i in range(len(cycles)-1)]
    avg_gap   = sum(gaps) / len(gaps)
    last_start= datetime.date.fromisoformat(cycles[0])
    expected  = last_start + datetime.timedelta(days=avg_gap)
    today     = datetime.date.today()
    overdue   = (today - expected).days
    return overdue if overdue >= 5 else None


@app.route('/sensor')
def sensor():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    vitals = read_arduino_line(retries=5)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT age, weight, height FROM users WHERE email = ?", (session['user'],))
    u = cur.fetchone()

    if not u:
        conn.close()
        return jsonify({"error": "user not found"}), 404

    bmr        = calculate_bmr(u['weight'], u['height'], u['age'])
    bmr_stat   = get_bmr_status(bmr)
    v_status   = vitals_status(vitals['bpm'], vitals['spo2'], vitals['temperature'])
    now        = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if request.args.get('save') == '1':
        cur.execute("""
            INSERT INTO history (email, bpm, spo2, temperature, bmr, time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session['user'], vitals['bpm'], vitals['spo2'],
              vitals['temperature'], round(bmr, 2), now))
        conn.commit()

    conn.close()
    return jsonify({
        "bpm":         vitals['bpm'],
        "spo2":        vitals['spo2'],
        "temperature": vitals['temperature'],
        "bmr":         round(bmr, 2),
        "bmr_status":  bmr_stat,
        "v_status":    v_status
    })


# ── BMR Report PDF (Personalized) ───────────────────────────────────────────
@app.route('/download_report')
def download_report():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    conn  = get_db()
    cur   = conn.cursor()

    cur.execute("SELECT name, age, weight, height, email, phone FROM users WHERE email = ?", (email,))
    user = cur.fetchone()

    cur.execute("""
        SELECT bpm, spo2, temperature, bmr, time FROM history
        WHERE email = ? ORDER BY id DESC LIMIT 1
    """, (email,))
    data = cur.fetchone()

    cur.execute("SELECT * FROM period_health_assessment WHERE email = ?", (email,))
    assessment = cur.fetchone()

    cur.execute("""
        SELECT * FROM period_cycles WHERE email = ?
        AND end_date IS NOT NULL ORDER BY id DESC LIMIT 1
    """, (email,))
    latest_cycle = cur.fetchone()

    latest_logs = []
    if latest_cycle:
        cur.execute("""
            SELECT * FROM daily_symptoms WHERE cycle_id = ? AND email = ?
            ORDER BY day_number
        """, (latest_cycle['id'], email))
        latest_logs = cur.fetchall()

    conn.close()

    # Use latest reading values, or safe defaults if no reading yet
    bpm_val  = (data['bpm']         if data and data['bpm']         not in ('—','',None) else '0') if data else '0'
    spo2_val = (data['spo2']        if data and data['spo2']        not in ('—','',None) else '0') if data else '0'
    temp_val = (data['temperature'] if data and data['temperature'] not in ('—','',None) else '0') if data else '0'
    bmr      = data['bmr']   if data else calculate_bmr(user['weight'], user['height'], user['age'])
    time_now = data['time']  if data else 'Not yet recorded'
    bmr_stat = get_bmr_status(bmr)

    # ── Helpers ──────────────────────────────────────────────
    def score_bar(score, max_score, bar_len=20):
        filled = int((score / max_score) * bar_len) if max_score else 0
        return "\u2588" * filled + "\u2591" * (bar_len - filled)

    q_labels = [
        "Irregular menstrual cycles",
        "Severe abdominal cramps",
        "Excessive fatigue or weakness",
        "Strong mood swings",
        "Headaches or migraines",
        "Heavy menstrual bleeding",
        "Bloating or stomach discomfort",
        "Acne breakouts before period",
        "Difficulty concentrating",
        "Symptoms affecting daily activities"
    ]
    score_map = {0: "Never", 1: "Sometimes", 2: "Often", 3: "Always"}

    def get_doctor_rec(assessment, bmr_stat):
        if not assessment:
            return None, []
        score = assessment['score']
        flags = []
        if assessment['q2'] and assessment['q2'] >= 2:
            flags.append("Severe cramps (possible Dysmenorrhea)")
        if assessment['q6'] and assessment['q6'] >= 2:
            flags.append("Heavy bleeding (possible Menorrhagia)")
        if assessment['q1'] and assessment['q1'] <= 1:
            flags.append("Irregular cycles (possible PCOS)")
        if assessment['q3'] and assessment['q3'] >= 2 and assessment['q6'] and assessment['q6'] >= 2:
            flags.append("Fatigue with heavy bleeding (possible Anaemia)")
        if assessment['q4'] and assessment['q4'] >= 2 and assessment['q7'] and assessment['q7'] >= 2:
            flags.append("Mood swings with bloating (possible PMDD)")
        if score >= 25 or len(flags) >= 2:
            specialist = "Gynaecologist"
        elif score >= 17:
            specialist = "General Physician or Gynaecologist"
        elif bmr_stat != "normal":
            specialist = "General Physician"
        else:
            specialist = None
        return specialist, flags

    def get_tips(assessment):
        tips = []
        if not assessment:
            return ["Maintain healthy routine — regular exercise, balanced nutrition, adequate sleep."]
        if assessment['q2'] and assessment['q2'] >= 2:
            tips.append("For cramps: Apply a heating pad on lower abdomen. Light yoga helps.")
        if assessment['q3'] and assessment['q3'] >= 2:
            tips.append("For fatigue: Increase iron-rich foods (spinach, lentils). Rest on heavy flow days.")
        if assessment['q6'] and assessment['q6'] >= 2:
            tips.append("For heavy bleeding: Track flow daily. Stay hydrated and avoid prolonged standing.")
        if assessment['q4'] and assessment['q4'] >= 2:
            tips.append("For mood swings: Limit caffeine and sugar. Light exercise releases feel-good hormones.")
        if assessment['q5'] and assessment['q5'] >= 2:
            tips.append("For headaches: Stay hydrated, maintain regular sleep, reduce screen time during period.")
        if assessment['q9'] and assessment['q9'] >= 2:
            tips.append("For concentration: Schedule lighter tasks on heavy flow days. Short rest breaks help.")
        if not tips:
            tips.append("Your symptoms are manageable. Keep up the healthy routine!")
        return tips

    # ── Build PDF ────────────────────────────────────────────
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()

    PINK       = colors.HexColor("#c2185b")
    DARK_PINK  = colors.HexColor("#880e4f")
    LIGHT_PINK = colors.HexColor("#f48fb1")
    PALE_PINK  = colors.HexColor("#fce4ec")
    GREY       = colors.HexColor("#888888")
    BLACK      = colors.HexColor("#2d2d2d")

    title_style = ParagraphStyle('T',  parent=styles['Title'],
                                  fontSize=24, textColor=PINK, spaceAfter=4, alignment=1)
    sub_style   = ParagraphStyle('S',  parent=styles['Normal'],
                                  fontSize=11, textColor=GREY, spaceAfter=2, alignment=1)
    h2_style    = ParagraphStyle('H2', parent=styles['Heading2'],
                                  fontSize=13, textColor=DARK_PINK, spaceAfter=6, spaceBefore=4)
    h3_style    = ParagraphStyle('H3', parent=styles['Normal'],
                                  fontSize=11, textColor=DARK_PINK, fontName="Helvetica-Bold",
                                  spaceAfter=4, spaceBefore=4)
    n_style     = ParagraphStyle('N',  parent=styles['Normal'],
                                  fontSize=10, leading=17, spaceAfter=3, textColor=BLACK)
    small_style = ParagraphStyle('SM', parent=styles['Normal'],
                                  fontSize=9, leading=14, textColor=GREY, spaceAfter=2)
    tip_style   = ParagraphStyle('TIP',parent=styles['Normal'],
                                  fontSize=10, leading=16, leftIndent=12,
                                  textColor=colors.HexColor("#444444"), spaceAfter=4)
    warn_style  = ParagraphStyle('W',  parent=styles['Normal'],
                                  fontSize=10, leading=16,
                                  textColor=colors.HexColor("#b71c1c"), spaceAfter=3)

    def hr(thick=0.5, c=LIGHT_PINK):
        return HRFlowable(width="100%", thickness=thick, color=c, spaceAfter=8, spaceBefore=4)

    story = []

    # Cover header
    story.append(Spacer(1, 10))
    story.append(Paragraph("GraceHealth", title_style))
    story.append(Paragraph("Women's Health Monitoring System", sub_style))
    story.append(Spacer(1, 4))
    story.append(hr(1.5, PINK))
    story.append(Spacer(1, 6))

    # Patient info table
    meta = [
        ["Patient",  user['name'],           "Report Date", datetime.datetime.now().strftime("%B %d, %Y")],
        ["Age",      f"{user['age']} years",  "Email",       user['email']],
        ["Weight",   f"{user['weight']} kg",  "Height",      f"{user['height']} cm"],
    ]
    mt = Table(meta, colWidths=[80, 180, 80, 160])
    mt.setStyle(TableStyle([
        ('FONTNAME',       (0,0),(0,-1), 'Helvetica-Bold'),
        ('FONTNAME',       (2,0),(2,-1), 'Helvetica-Bold'),
        ('FONTSIZE',       (0,0),(-1,-1), 10),
        ('TEXTCOLOR',      (0,0),(0,-1), DARK_PINK),
        ('TEXTCOLOR',      (2,0),(2,-1), DARK_PINK),
        ('ROWBACKGROUNDS', (0,0),(-1,-1), [colors.HexColor("#fff8fb"), colors.white]),
        ('PADDING',        (0,0),(-1,-1), 7),
        ('GRID',           (0,0),(-1,-1), 0.3, LIGHT_PINK),
    ]))
    story.append(mt)
    story.append(Spacer(1, 14))
    story.append(hr())

    # BMR + Vitals section
    story.append(Paragraph("Health Vitals & BMR Analysis", h2_style))
    bmr_label = "Normal" if bmr_stat == "normal" else ("Below Normal" if bmr_stat == "low" else "Above Normal")
    bmr_data = [
        ["Heart Rate (BPM)", f"{bpm_val}",   "Blood Oxygen (SpO2)", f"{spo2_val} %"],
        ["Body Temp (°C)",   f"{temp_val}",  "BMR Value",           f"{round(bmr, 2)} kcal/day"],
        ["Normal BPM",       "60 – 100",     "Normal SpO2",         "95 – 100 %"],
        ["Normal Temp",      "36.1 – 37.5°C","BMR Status",          bmr_label],
    ]
    bt = Table(bmr_data, colWidths=[100, 160, 100, 140])
    bt.setStyle(TableStyle([
        ('FONTNAME',       (0,0),(0,-1), 'Helvetica-Bold'),
        ('FONTNAME',       (2,0),(2,-1), 'Helvetica-Bold'),
        ('FONTSIZE',       (0,0),(-1,-1), 10),
        ('TEXTCOLOR',      (0,0),(0,-1), DARK_PINK),
        ('TEXTCOLOR',      (2,0),(2,-1), DARK_PINK),
        ('ROWBACKGROUNDS', (0,0),(-1,-1), [colors.HexColor("#fff8fb"), colors.white]),
        ('PADDING',        (0,0),(-1,-1), 7),
        ('GRID',           (0,0),(-1,-1), 0.3, LIGHT_PINK),
    ]))
    story.append(bt)
    if bmr_stat != "normal":
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"\u26a0  Your BMR of {round(bmr,2)} kcal/day is {bmr_label.lower()}. "
            "This may indicate nutritional or metabolic concerns. "
            "Please consult a healthcare professional.",
            warn_style))
    story.append(Spacer(1, 12))
    story.append(hr())

    # Period Health Assessment section
    if assessment:
        story.append(Paragraph("Period Health Assessment", h2_style))
        score = assessment['score']
        bar   = score_bar(score, 30)
        story.append(Paragraph(f"<b>Total Score: {score} / 30</b>", n_style))
        story.append(Paragraph(
            f"<font name='Courier' size='9'>{bar}</font>  {score}/30",
            ParagraphStyle('BAR', parent=styles['Normal'],
                           fontSize=9, textColor=PINK, spaceAfter=4)))
        story.append(Paragraph(f"<b>Result:</b> {assessment['analysis']}", n_style))
        story.append(Spacer(1, 8))

        # Frequently reported symptoms (Often/Always only)
        frequent = []
        for i in range(1, 11):
            val = assessment[f'q{i}']
            if val is not None and val >= 2:
                frequent.append((q_labels[i-1], score_map[val]))

        if frequent:
            story.append(Paragraph("Frequently Reported Symptoms", h3_style))
            story.append(Paragraph(
                "Symptoms reported as <b>Often</b> or <b>Always</b>:", small_style))
            story.append(Spacer(1, 4))
            sym_data = [["Symptom", "Frequency"]] + [[s, f] for s, f in frequent]
            sym_t = Table(sym_data, colWidths=[340, 100])
            sym_t.setStyle(TableStyle([
                ('BACKGROUND',     (0,0),(-1,0), PALE_PINK),
                ('TEXTCOLOR',      (0,0),(-1,0), DARK_PINK),
                ('FONTNAME',       (0,0),(-1,0), 'Helvetica-Bold'),
                ('FONTSIZE',       (0,0),(-1,-1), 9),
                ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor("#fff5f8")]),
                ('GRID',           (0,0),(-1,-1), 0.3, LIGHT_PINK),
                ('PADDING',        (0,0),(-1,-1), 6),
                ('TEXTCOLOR',      (1,1),(1,-1), colors.HexColor("#c62828")),
            ]))
            story.append(sym_t)
            story.append(Spacer(1, 8))

        # Full answers table
        story.append(Paragraph("Complete Assessment Answers", h3_style))
        all_data = [["#", "Question", "Answer", "Score"]]
        for i in range(1, 11):
            val = assessment[f'q{i}']
            all_data.append([str(i), q_labels[i-1], score_map.get(val, "—"),
                             str(val) if val is not None else "—"])
        all_t = Table(all_data, colWidths=[25, 300, 90, 40])
        all_t.setStyle(TableStyle([
            ('BACKGROUND',     (0,0),(-1,0), PALE_PINK),
            ('TEXTCOLOR',      (0,0),(-1,0), DARK_PINK),
            ('FONTNAME',       (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',       (0,0),(-1,-1), 9),
            ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor("#fff5f8")]),
            ('GRID',           (0,0),(-1,-1), 0.3, LIGHT_PINK),
            ('PADDING',        (0,0),(-1,-1), 5),
            ('VALIGN',         (0,0),(-1,-1), 'MIDDLE'),
        ]))
        story.append(all_t)
        story.append(Spacer(1, 12))
        story.append(hr())

        # Doctor recommendation
        specialist, flags = get_doctor_rec(assessment, bmr_stat)
        if specialist:
            story.append(Paragraph("Medical Recommendation", h2_style))
            story.append(Paragraph(
                f"Based on your symptoms and health data, we recommend consulting a "
                f"<b>{specialist}</b>.", n_style))
            if flags:
                story.append(Spacer(1, 4))
                story.append(Paragraph("Key indicators noted:", small_style))
                for flag in flags:
                    story.append(Paragraph(f"  \u2022  {flag}", n_style))
            story.append(Spacer(1, 8))
            story.append(hr())

        # Personalised tips
        tips = get_tips(assessment)
        story.append(Paragraph("Personalised Health Tips", h2_style))
        for tip in tips:
            story.append(Paragraph(f"\u2756  {tip}", tip_style))
        story.append(Spacer(1, 8))
        story.append(hr())

    # Latest cycle brief (if exists)
    if latest_cycle and latest_logs:
        story.append(Paragraph("Latest Cycle Summary", h2_style))
        dur = "—"
        try:
            s   = datetime.datetime.strptime(latest_cycle['start_date'], "%Y-%m-%d")
            e   = datetime.datetime.strptime(latest_cycle['end_date'],   "%Y-%m-%d")
            dur = f"{(e - s).days + 1} days"
        except Exception:
            pass
        story.append(Paragraph(
            f"<b>Period:</b> {latest_cycle['start_date']} to {latest_cycle['end_date']}  "
            f"({dur})  |  <b>Avg Score:</b> {latest_cycle['avg_score']}  |  "
            f"<b>Overall:</b> {latest_cycle['overall_label'] or '—'}",
            n_style))
        story.append(Spacer(1, 6))
        cyc_data = [["Day", "Score", "Status", "Pain Relief"]]
        for lg in latest_logs:
            cyc_data.append([
                f"Day {lg['day_number']}",
                f"{lg['score']} / 15",
                lg['analysis'] or "—",
                "Yes" if lg['pain_relief'] else "No",
            ])
        ct = Table(cyc_data, colWidths=[60, 70, 120, 80])
        ct.setStyle(TableStyle([
            ('BACKGROUND',     (0,0),(-1,0), PALE_PINK),
            ('TEXTCOLOR',      (0,0),(-1,0), DARK_PINK),
            ('FONTNAME',       (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',       (0,0),(-1,-1), 9),
            ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor("#fff5f8")]),
            ('GRID',           (0,0),(-1,-1), 0.3, LIGHT_PINK),
            ('PADDING',        (0,0),(-1,-1), 6),
        ]))
        story.append(ct)
        story.append(Spacer(1, 12))
        story.append(hr())


    # ── Nearby Doctors Section ───────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(hr(1.5, PINK))
    story.append(Paragraph("👩‍⚕️ Nearby Gynaecologists — Dumka, Jharkhand", h2_style))
    story.append(Paragraph(
        "If you require medical attention, the following specialists are available near you.",
        n_style))
    story.append(Spacer(1, 8))

    doc_data = [
        [
            Paragraph("<b>Doctor / Hospital</b>", ParagraphStyle('DH', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Contact</b>", ParagraphStyle('DH2', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Location</b>", ParagraphStyle('DH3', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Speciality</b>", ParagraphStyle('DH4', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
        ],
        [
            Paragraph("<b>Dr. Ankita Singh</b><br/>Gynaecologist &amp; Obstetrician",
                ParagraphStyle('DC', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("93342 39788<br/>89861 94091<br/><i>Online consults available</i>",
                ParagraphStyle('DC2', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Hope Diagnostics &amp; Healthcare,<br/>Napit Para, Doctor Lane,<br/>Near Durgasthan Road, Dumka",
                ParagraphStyle('DC3', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Pregnancy Management,<br/>High-Risk Cases,<br/>Irregular Periods",
                ParagraphStyle('DC4', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
        [
            Paragraph("<b>Dr. Rukhsana Yasmin</b><br/>Gynaecologist &amp; Obstetrician",
                ParagraphStyle('DC5', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Available via Justdial<br/><i>justdial.com</i><br/>10:00 AM – 7:00 PM",
                ParagraphStyle('DC6', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Hindustan Medical,<br/>Jail Road, Durga Manda Road, Dumka",
                ParagraphStyle('DC7', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("General Gynaecology,<br/>Obstetric Care",
                ParagraphStyle('DC8', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
        [
            Paragraph("<b>Bharati Hospital</b><br/>Multi-Speciality Maternity Centre",
                ParagraphStyle('DC9', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("020-48562555<br/><i>Ambulance on-site</i>",
                ParagraphStyle('DC10', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Gidhni Pahari Road,<br/>Near Shiv Pahar Chowk,<br/>Banderjori, Dumka",
                ParagraphStyle('DC11', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Maternity Services,<br/>Emergency Care,<br/>Ambulance Available",
                ParagraphStyle('DC12', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
    ]

    doc_table = Table(doc_data, colWidths=[130, 105, 145, 125])
    doc_table.setStyle(TableStyle([
        ('BACKGROUND',     (0,0), (-1,0),  PINK),
        ('TEXTCOLOR',      (0,0), (-1,0),  colors.white),
        ('FONTNAME',       (0,0), (-1,0),  'Helvetica-Bold'),
        ('FONTSIZE',       (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor("#fff8fb"), colors.white]),
        ('GRID',           (0,0), (-1,-1), 0.3, LIGHT_PINK),
        ('VALIGN',         (0,0), (-1,-1), 'TOP'),
        ('PADDING',        (0,0), (-1,-1), 7),
        ('BOTTOMPADDING',  (0,1), (-1,-1), 10),
    ]))
    story.append(doc_table)
    story.append(Spacer(1, 10))
    story.append(hr())

    # Footer
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"<i>Generated by GraceHealth on "
        f"{datetime.datetime.now().strftime('%B %d, %Y at %H:%M')}. "
        "This report is for personal health tracking and is not a substitute "
        "for professional medical advice.</i>",
        small_style))

    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=55, leftMargin=55,
                            topMargin=50, bottomMargin=50)
    doc.build(story)
    buffer.seek(0)

    safe_name   = user['name'].replace(' ', '_')
    report_date = datetime.datetime.now().strftime("%Y-%m-%d")
    filename    = f"GraceHealth_{safe_name}_{report_date}.pdf"

    conn2 = get_db()
    cur2  = conn2.cursor()
    cur2.execute("""
        UPDATE history SET report_downloaded = 1
        WHERE id = (SELECT id FROM history WHERE email = ?
                    ORDER BY id DESC LIMIT 1)
    """, (email,))
    conn2.commit()
    conn2.close()

    return send_file(buffer, as_attachment=True,
                     download_name=filename, mimetype='application/pdf')



# ── Cycle Summary PDF (Full Detail) ──────────────────────────────────────────
@app.route('/download_cycle_report/<int:cycle_id>')
def download_cycle_report(cycle_id):
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    conn  = get_db()
    cur   = conn.cursor()

    cur.execute("SELECT name, age, weight, height, email FROM users WHERE email = ?", (email,))
    user = cur.fetchone()

    cur.execute("SELECT * FROM period_cycles WHERE id = ? AND email = ?", (cycle_id, email))
    cycle = cur.fetchone()

    cur.execute("""
        SELECT * FROM daily_symptoms WHERE cycle_id = ? AND email = ?
        ORDER BY day_number
    """, (cycle_id, email))
    logs = cur.fetchall()

    conn.close()

    if not cycle:
        return "Cycle not found.", 404

    # ── Setup styles ─────────────────────────────────────────
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()

    PINK       = colors.HexColor("#c2185b")
    DARK_PINK  = colors.HexColor("#880e4f")
    LIGHT_PINK = colors.HexColor("#f48fb1")
    PALE_PINK  = colors.HexColor("#fce4ec")
    GREY       = colors.HexColor("#888888")
    BLACK      = colors.HexColor("#2d2d2d")
    GREEN      = colors.HexColor("#2e7d32")
    ORANGE     = colors.HexColor("#e65100")
    RED        = colors.HexColor("#b71c1c")

    title_style = ParagraphStyle('T',  parent=styles['Title'],
                                  fontSize=22, textColor=PINK, spaceAfter=4, alignment=1)
    sub_style   = ParagraphStyle('S',  parent=styles['Normal'],
                                  fontSize=11, textColor=GREY, spaceAfter=2, alignment=1)
    h2_style    = ParagraphStyle('H2', parent=styles['Heading2'],
                                  fontSize=13, textColor=DARK_PINK, spaceAfter=6, spaceBefore=6)
    h3_style    = ParagraphStyle('H3', parent=styles['Normal'],
                                  fontSize=11, textColor=DARK_PINK, fontName="Helvetica-Bold",
                                  spaceAfter=4, spaceBefore=4)
    n_style     = ParagraphStyle('N',  parent=styles['Normal'],
                                  fontSize=10, leading=17, spaceAfter=3, textColor=BLACK)
    small_style = ParagraphStyle('SM', parent=styles['Normal'],
                                  fontSize=9, leading=14, textColor=GREY, spaceAfter=2)
    tip_style   = ParagraphStyle('TIP',parent=styles['Normal'],
                                  fontSize=10, leading=16, leftIndent=12,
                                  textColor=colors.HexColor("#444444"), spaceAfter=4)
    warn_style  = ParagraphStyle('W',  parent=styles['Normal'],
                                  fontSize=10, leading=16, textColor=RED, spaceAfter=3)

    def hr(thick=0.5, color=LIGHT_PINK):
        return HRFlowable(width="100%", thickness=thick, color=color, spaceAfter=8, spaceBefore=4)

    def score_bar(score, max_score, bar_len=15):
        filled = int((score / max_score) * bar_len) if max_score else 0
        return "█" * filled + "░" * (bar_len - filled)

    score_map  = {0: "Never", 1: "Sometimes", 2: "Often", 3: "Always"}
    q5_labels  = [
        "Cramp severity",
        "Energy / Fatigue level",
        "Bleeding heaviness",
        "Mood",
        "Impact on daily activities"
    ]

    # ── Calculate duration ────────────────────────────────────
    duration = "—"
    try:
        s = datetime.datetime.strptime(cycle['start_date'], "%Y-%m-%d")
        e = datetime.datetime.strptime(cycle['end_date'],   "%Y-%m-%d")
        duration = f"{(e - s).days + 1} days"
    except Exception:
        pass

    # ── Pain relief count ─────────────────────────────────────
    pain_days = [lg for lg in logs if lg['pain_relief'] == 1]

    # ── Symptom pattern analysis ──────────────────────────────
    def symptom_pattern(logs, q_idx):
        vals = []
        for lg in logs:
            v = lg[f'q{q_idx}']
            if v is not None:
                vals.append(v)
        if not vals:
            return "No data"
        avg = sum(vals) / len(vals)
        peak_day = None
        peak_val = -1
        for lg in logs:
            v = lg[f'q{q_idx}']
            if v is not None and v > peak_val:
                peak_val = v
                peak_day = lg['day_number']
        label = score_map.get(round(avg), "—")
        return f"Avg: {label}  |  Peak: Day {peak_day} ({score_map.get(peak_val,'—')})"

    # ── Health recommendation based on cycle data ─────────────
    def cycle_recommendation(logs, cycle):
        if not logs:
            return None
        avg  = cycle['avg_score'] or 0
        high = [lg for lg in logs if lg['score'] >= 10]
        recs = []
        if len(high) >= 2:
            recs.append("Multiple severe symptom days detected. Consider tracking next cycle closely.")
        if any(lg['q3'] >= 2 for lg in logs if lg['q3'] is not None):
            recs.append("Heavy bleeding was reported on multiple days — consult a Gynaecologist if this recurs.")
        if any(lg['q1'] >= 2 for lg in logs if lg['q1'] is not None):
            recs.append("Severe cramps reported — Dysmenorrhea evaluation may be helpful.")
        if len(pain_days) >= 3:
            recs.append(f"Pain medication was used on {len(pain_days)} days this cycle. Consult a doctor for better management.")
        if avg >= 10:
            recs.append("Your overall cycle score indicates significant discomfort. A medical consultation is recommended.")
        return recs if recs else None

    # ── Tips based on worst symptoms ─────────────────────────
    def cycle_tips(logs):
        tips = []
        if any(lg['q1'] is not None and lg['q1'] >= 2 for lg in logs):
            tips.append("For cramps: Use a heating pad on days 2–3. Light stretching before sleep can help.")
        if any(lg['q2'] is not None and lg['q2'] >= 2 for lg in logs):
            tips.append("For fatigue: Eat iron-rich foods (spinach, lentils). Rest on your heaviest days.")
        if any(lg['q3'] is not None and lg['q3'] >= 2 for lg in logs):
            tips.append("For heavy bleeding: Stay hydrated, track flow daily, avoid standing for long periods.")
        if any(lg['q4'] is not None and lg['q4'] >= 2 for lg in logs):
            tips.append("For mood: Limit caffeine, get 7–8 hours sleep, short walks improve mood naturally.")
        if any(lg['q5'] is not None and lg['q5'] >= 2 for lg in logs):
            tips.append("For daily impact: Plan lighter workload on your heaviest days next cycle.")
        if not tips:
            tips.append("Your symptoms were manageable this cycle. Keep up your healthy routine!")
        return tips

    # ════════════════════════════════════════════════
    # BUILD PDF
    # ════════════════════════════════════════════════
    story = []

    # Cover
    story.append(Spacer(1, 8))
    story.append(Paragraph("GraceHealth", title_style))
    story.append(Paragraph("Cycle Health Summary Report", sub_style))
    story.append(Spacer(1, 4))
    story.append(hr(1.5, PINK))
    story.append(Spacer(1, 4))

    # Patient + cycle meta
    meta_data = [
        ["Patient",     user['name'],          "Cycle Period",  f"{cycle['start_date']} to {cycle['end_date']}"],
        ["Age",         f"{user['age']} years", "Duration",      duration],
        ["Weight",      f"{user['weight']} kg", "Overall Label", cycle['overall_label'] or "—"],
        ["Height",      f"{user['height']} cm", "Generated",     datetime.datetime.now().strftime("%B %d, %Y")],
    ]
    mt = Table(meta_data, colWidths=[80, 165, 95, 160])
    mt.setStyle(TableStyle([
        ('FONTNAME',  (0,0),(0,-1), 'Helvetica-Bold'),
        ('FONTNAME',  (2,0),(2,-1), 'Helvetica-Bold'),
        ('FONTSIZE',  (0,0),(-1,-1), 10),
        ('TEXTCOLOR', (0,0),(0,-1), DARK_PINK),
        ('TEXTCOLOR', (2,0),(2,-1), DARK_PINK),
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor("#fff8fb"), colors.white]),
        ('PADDING',   (0,0),(-1,-1), 7),
        ('GRID',      (0,0),(-1,-1), 0.3, LIGHT_PINK),
    ]))
    story.append(mt)
    story.append(Spacer(1, 12))
    story.append(hr())

    # Cycle Overview
    story.append(Paragraph("Cycle Overview", h2_style))
    ov_data = [
        ["Average Score",
         f"{cycle['avg_score']} / 15  {score_bar(cycle['avg_score'] or 0, 15)}"],
        ["Best Day",
         f"Day {cycle['best_day']} (Score {cycle['best_score']} — {cycle['overall_label'] or '—'})"
         if cycle['best_day'] else "—"],
        ["Worst Day",
         f"Day {cycle['worst_day']} (Score {cycle['worst_score']})"
         if cycle['worst_day'] else "—"],
        ["Pain Medication Used",
         f"{len(pain_days)} out of {len(logs)} days"
         if logs else "—"],
    ]
    ov_table = Table(ov_data, colWidths=[160, 340])
    ov_table.setStyle(TableStyle([
        ('FONTNAME',  (0,0),(0,-1), 'Helvetica-Bold'),
        ('FONTSIZE',  (0,0),(-1,-1), 10),
        ('TEXTCOLOR', (0,0),(0,-1), DARK_PINK),
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor("#fff8fb"), colors.white]),
        ('PADDING',   (0,0),(-1,-1), 7),
        ('GRID',      (0,0),(-1,-1), 0.3, LIGHT_PINK),
    ]))
    story.append(ov_table)
    story.append(Spacer(1, 12))
    story.append(hr())

    # Day-by-day full log
    if logs:
        story.append(Paragraph("Day-by-Day Symptom Log", h2_style))
        story.append(Paragraph(
            "Each answer is shown as: Never (0) · Sometimes (1) · Often (2) · Always (3)",
            small_style))
        story.append(Spacer(1, 6))

        log_data = [["Day", "Date", "Cramps", "Fatigue", "Bleeding",
                     "Mood", "Impact", "Score", "Status", "💊", "Notes"]]

        for lg in logs:
            def fmt(v):
                return score_map.get(v, "—") if v is not None else "—"

            status_color = GREEN if lg['analysis'] == 'Mild' else                            ORANGE if lg['analysis'] == 'Moderate' else RED

            log_data.append([
                f"Day {lg['day_number']}",
                lg['date'] or "—",
                fmt(lg['q1']),
                fmt(lg['q2']),
                fmt(lg['q3']),
                fmt(lg['q4']),
                fmt(lg['q5']),
                f"{lg['score']}/15",
                lg['analysis'] or "—",
                "Yes" if lg['pain_relief'] else "No",
                (lg['notes'] or "—")[:28],
            ])

        log_table = Table(log_data,
                          colWidths=[35, 52, 52, 45, 52, 45, 45, 36, 48, 22, 68])
        log_table.setStyle(TableStyle([
            ('BACKGROUND',  (0,0),(-1,0), PALE_PINK),
            ('TEXTCOLOR',   (0,0),(-1,0), DARK_PINK),
            ('FONTNAME',    (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',    (0,0),(-1,-1), 8),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor("#fff5f8")]),
            ('GRID',        (0,0),(-1,-1), 0.3, LIGHT_PINK),
            ('PADDING',     (0,0),(-1,-1), 4),
            ('VALIGN',      (0,0),(-1,-1), 'MIDDLE'),
            ('ALIGN',       (7,0),(7,-1), 'CENTER'),
            ('ALIGN',       (9,0),(9,-1), 'CENTER'),
        ]))
        story.append(log_table)
        story.append(Spacer(1, 12))
        story.append(hr())

        # Personal notes timeline
        notes_logs = [lg for lg in logs if lg['notes'] and lg['notes'].strip()]
        if notes_logs:
            story.append(Paragraph("Personal Notes", h2_style))
            for lg in notes_logs:
                story.append(Paragraph(
                    f"<b>Day {lg['day_number']} ({lg['date']}):</b>  {lg['notes']}",
                    n_style))
            story.append(Spacer(1, 10))
            story.append(hr())

        # Symptom pattern analysis
        story.append(Paragraph("Symptom Pattern Analysis", h2_style))
        pat_data = [["Symptom", "Pattern Across This Cycle"]]
        for i, label in enumerate(q5_labels, 1):
            pat_data.append([label, symptom_pattern(logs, i)])
        pat_table = Table(pat_data, colWidths=[160, 340])
        pat_table.setStyle(TableStyle([
            ('BACKGROUND',  (0,0),(-1,0), PALE_PINK),
            ('TEXTCOLOR',   (0,0),(-1,0), DARK_PINK),
            ('FONTNAME',    (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',    (0,0),(-1,-1), 9),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor("#fff5f8")]),
            ('GRID',        (0,0),(-1,-1), 0.3, LIGHT_PINK),
            ('PADDING',     (0,0),(-1,-1), 6),
            ('FONTNAME',    (0,1),(0,-1), 'Helvetica-Bold'),
            ('TEXTCOLOR',   (0,1),(0,-1), DARK_PINK),
        ]))
        story.append(pat_table)
        story.append(Spacer(1, 12))
        story.append(hr())

        # Health recommendations
        recs = cycle_recommendation(logs, cycle)
        if recs:
            story.append(Paragraph("Health Recommendations", h2_style))
            for rec in recs:
                story.append(Paragraph(f"⚠  {rec}", warn_style))
            story.append(Spacer(1, 8))
            story.append(hr())

        # Tips for next cycle
        tips = cycle_tips(logs)
        story.append(Paragraph("Tips for Your Next Cycle", h2_style))
        for tip in tips:
            story.append(Paragraph(f"✦  {tip}", tip_style))
        story.append(Spacer(1, 10))
        story.append(hr())

    else:
        story.append(Paragraph("No daily symptom logs were recorded for this cycle.", n_style))
        story.append(Spacer(1, 10))
        story.append(hr())


    # ── Nearby Doctors Section ───────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(hr(1.5, PINK))
    story.append(Paragraph("👩‍⚕️ Nearby Gynaecologists — Dumka, Jharkhand", h2_style))
    story.append(Paragraph(
        "If you require medical attention, the following specialists are available near you.",
        n_style))
    story.append(Spacer(1, 8))

    doc_data = [
        [
            Paragraph("<b>Doctor / Hospital</b>", ParagraphStyle('DH', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Contact</b>", ParagraphStyle('DH2', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Location</b>", ParagraphStyle('DH3', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
            Paragraph("<b>Speciality</b>", ParagraphStyle('DH4', parent=styles['Normal'],
                fontSize=9, textColor=colors.white, fontName='Helvetica-Bold')),
        ],
        [
            Paragraph("<b>Dr. Ankita Singh</b><br/>Gynaecologist &amp; Obstetrician",
                ParagraphStyle('DC', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("93342 39788<br/>89861 94091<br/><i>Online consults available</i>",
                ParagraphStyle('DC2', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Hope Diagnostics &amp; Healthcare,<br/>Napit Para, Doctor Lane,<br/>Near Durgasthan Road, Dumka",
                ParagraphStyle('DC3', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Pregnancy Management,<br/>High-Risk Cases,<br/>Irregular Periods",
                ParagraphStyle('DC4', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
        [
            Paragraph("<b>Dr. Rukhsana Yasmin</b><br/>Gynaecologist &amp; Obstetrician",
                ParagraphStyle('DC5', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Available via Justdial<br/><i>justdial.com</i><br/>10:00 AM – 7:00 PM",
                ParagraphStyle('DC6', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Hindustan Medical,<br/>Jail Road, Durga Manda Road, Dumka",
                ParagraphStyle('DC7', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("General Gynaecology,<br/>Obstetric Care",
                ParagraphStyle('DC8', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
        [
            Paragraph("<b>Bharati Hospital</b><br/>Multi-Speciality Maternity Centre",
                ParagraphStyle('DC9', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("020-48562555<br/><i>Ambulance on-site</i>",
                ParagraphStyle('DC10', parent=styles['Normal'], fontSize=9, leading=13,
                    textColor=colors.HexColor("#c2185b"))),
            Paragraph("Gidhni Pahari Road,<br/>Near Shiv Pahar Chowk,<br/>Banderjori, Dumka",
                ParagraphStyle('DC11', parent=styles['Normal'], fontSize=9, leading=13)),
            Paragraph("Maternity Services,<br/>Emergency Care,<br/>Ambulance Available",
                ParagraphStyle('DC12', parent=styles['Normal'], fontSize=9, leading=13)),
        ],
    ]

    doc_table = Table(doc_data, colWidths=[130, 105, 145, 125])
    doc_table.setStyle(TableStyle([
        ('BACKGROUND',     (0,0), (-1,0),  PINK),
        ('TEXTCOLOR',      (0,0), (-1,0),  colors.white),
        ('FONTNAME',       (0,0), (-1,0),  'Helvetica-Bold'),
        ('FONTSIZE',       (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor("#fff8fb"), colors.white]),
        ('GRID',           (0,0), (-1,-1), 0.3, LIGHT_PINK),
        ('VALIGN',         (0,0), (-1,-1), 'TOP'),
        ('PADDING',        (0,0), (-1,-1), 7),
        ('BOTTOMPADDING',  (0,1), (-1,-1), 10),
    ]))
    story.append(doc_table)
    story.append(Spacer(1, 10))
    story.append(hr())

    # Footer
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "<i>This report was generated by GraceHealth Women's Health Monitoring System. "
        "This document is intended for personal health tracking and is not a substitute "
        "for professional medical diagnosis or advice.</i>",
        small_style))

    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=50, leftMargin=50,
                            topMargin=50, bottomMargin=50)
    doc.build(story)
    buffer.seek(0)

    safe_name = user['name'].replace(' ', '_')
    filename  = f"GraceHealth_Cycle_{safe_name}_{cycle['start_date']}.pdf"
    return send_file(buffer, as_attachment=True,
                     download_name=filename, mimetype='application/pdf')


# ── Admin Panel ───────────────────────────────────────────────────────────────
@app.route('/admin')
def admin():
    if 'admin' not in session:
        return redirect('/login')

    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT name, email, age, weight, height, phone, password FROM users")
    users = cur.fetchall()

    cur.execute("""
        SELECT id, email, bpm, spo2, temperature, bmr, time, report_downloaded
        FROM history ORDER BY id DESC
    """)
    history = cur.fetchall()

    # Assessment scores per user
    cur.execute("SELECT email, score, analysis FROM period_health_assessment")
    assessments = {r['email']: r for r in cur.fetchall()}

    # Health flags: users with assessment score >= 25 or recent daily score >= 13
    flagged = set()
    cur.execute("SELECT email FROM period_health_assessment WHERE score >= 25")
    for r in cur.fetchall():
        flagged.add(r['email'])

    cur.execute("""
        SELECT email, COUNT(*) as cnt FROM daily_symptoms
        WHERE score >= 10
        GROUP BY email HAVING cnt >= 3
    """)
    for r in cur.fetchall():
        flagged.add(r['email'])

    # Cycle summaries per user
    cur.execute("""
        SELECT email, COUNT(*) as total_cycles, AVG(avg_score) as overall_avg
        FROM period_cycles
        WHERE end_date IS NOT NULL
        GROUP BY email
    """)
    cycle_stats = {r['email']: r for r in cur.fetchall()}

    conn.close()

    # Build avatar map: email -> avatar_url
    avatar_map = {u['email']: get_avatar(u['age']) for u in users}

    return render_template("admin.html",
        users       = users,
        history     = history,
        assessments = assessments,
        flagged     = flagged,
        cycle_stats = cycle_stats,
        avatar_map  = avatar_map
    )



# ── Admin: Get password (eye icon) ───────────────────────────────────────────
@app.route('/admin/get_password/<email>')
def admin_get_password(email):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT password FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({"password": row['password']})
    return jsonify({"error": "User not found"}), 404


# ── Admin: User detail overview (report + assessment + cycles) ────────────────
@app.route('/admin/user/<email>')
def admin_user_detail(email):
    if 'admin' not in session:
        return redirect('/login')

    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT name, email, age, weight, height, phone, selected_avatar FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return "User not found.", 404

    cur.execute("SELECT * FROM period_health_assessment WHERE email = ?", (email,))
    assessment = cur.fetchone()

    cur.execute("""
        SELECT bpm, spo2, temperature, bmr, time, report_downloaded FROM history
        WHERE email = ? ORDER BY id DESC LIMIT 10
    """, (email,))
    history = cur.fetchall()

    cur.execute("""
        SELECT * FROM period_cycles WHERE email = ?
        ORDER BY id DESC
    """, (email,))
    cycles = cur.fetchall()

    cur.execute("""
        SELECT * FROM daily_symptoms WHERE email = ?
        ORDER BY id DESC LIMIT 20
    """, (email,))
    daily_logs = cur.fetchall()

    conn.close()

    bmr = calculate_bmr(user['weight'], user['height'], user['age'])

    return render_template("admin_user.html",
        user        = user,
        assessment  = assessment,
        history     = history,
        cycles      = cycles,
        daily_logs  = daily_logs,
        bmr         = round(bmr, 2),
        bmr_status  = get_bmr_status(bmr),
        avatar_url  = get_avatar(user['age'],
                               user['selected_avatar'] if 'selected_avatar' in user.keys() and user['selected_avatar'] else '')
    )


# ── Admin: Delete user ────────────────────────────────────────────────────────
@app.route('/admin/delete/user/<email>', methods=['POST'])
def admin_delete_user(email):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM users WHERE email = ?", (email,))
    cur.execute("DELETE FROM history WHERE email = ?", (email,))
    cur.execute("DELETE FROM period_health_assessment WHERE email = ?", (email,))
    cur.execute("DELETE FROM period_cycles WHERE email = ?", (email,))
    cur.execute("DELETE FROM daily_symptoms WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ── Admin: Delete single history record ───────────────────────────────────────
@app.route('/admin/delete/history/<int:record_id>', methods=['POST'])
def admin_delete_history(record_id):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM history WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ── Admin: Delete single daily symptom log ────────────────────────────────────
@app.route('/admin/delete/daily/<int:log_id>', methods=['POST'])
def admin_delete_daily(log_id):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM daily_symptoms WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ── Admin: Delete single cycle ────────────────────────────────────────────────
@app.route('/admin/delete/cycle/<int:cycle_id>', methods=['POST'])
def admin_delete_cycle(cycle_id):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM period_cycles WHERE id = ?", (cycle_id,))
    cur.execute("DELETE FROM daily_symptoms WHERE cycle_id = ?", (cycle_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ── Admin: Delete assessment ───────────────────────────────────────────────────
@app.route('/admin/delete/assessment/<email>', methods=['POST'])
def admin_delete_assessment(email):
    if 'admin' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM period_health_assessment WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ── Edit Profile ─────────────────────────────────────────────────────────────
@app.route('/edit_profile', methods=['GET', 'POST'])
def edit_profile():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    error = None
    success = None

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT name, age, weight, height, email, phone, selected_avatar, password FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    conn.close()

    if request.method == 'POST':
        name    = request.form.get('name', '').strip()
        try:
            age    = int(request.form.get('age', 0))
            weight = float(request.form.get('weight', 0))
            height = float(request.form.get('height', 0))
        except ValueError:
            error = "Please enter valid numbers."
            return render_template("edit_profile.html", user=user, error=error, success=None)

        new_pw   = request.form.get('new_password', '').strip()
        conf_pw  = request.form.get('confirm_password', '').strip()
        curr_pw  = request.form.get('current_password', '').strip()

        if not name or age < 10 or age > 80 or weight < 20 or weight > 200 or height < 50 or height > 220:
            error = "Please enter valid values for all fields."
        elif new_pw and curr_pw != user['password']:
            error = "Current password is incorrect."
        elif new_pw and len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        elif new_pw and new_pw != conf_pw:
            error = "New passwords do not match."
        else:
            final_pw      = new_pw if new_pw else user['password']
            phone_new     = request.form.get('phone', '').strip()
            chosen_avatar = request.form.get('selected_avatar', '').strip()
            conn2 = get_db()
            cur2  = conn2.cursor()
            cur2.execute("""
                UPDATE users SET name=?, age=?, weight=?, height=?,
                                 phone=?, selected_avatar=?, password=?
                WHERE email=?
            """, (name, age, weight, height, phone_new, chosen_avatar, final_pw, email))
            conn2.commit()
            conn2.close()
            success = "Profile updated successfully!"
            conn3 = get_db()
            cur3  = conn3.cursor()
            cur3.execute("SELECT name, age, weight, height, email, phone, selected_avatar, password FROM users WHERE email = ?", (email,))
            user = cur3.fetchone()
            conn3.close()

    sel_av2 = user['selected_avatar'] if user and 'selected_avatar' in user.keys() and user['selected_avatar'] else ''
    return render_template("edit_profile.html", user=user, error=error, success=success,
                           avatar_url=get_avatar(user['age'], sel_av2),
                           avatar_options=AVATAR_OPTIONS,
                           selected_avatar=sel_av2)


# ── Doctor Visit Tracker ──────────────────────────────────────────────────────
@app.route('/doctor_visit', methods=['GET', 'POST'])
def doctor_visit():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']

    if request.method == 'POST':
        visit_date = request.form.get('visit_date', '')
        diagnosis  = request.form.get('diagnosis', '').strip()[:300]
        medication = request.form.get('medication', '').strip()[:200]
        next_appt  = request.form.get('next_appt', '')
        notes      = request.form.get('notes', '').strip()[:300]

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO doctor_visits (email, visit_date, diagnosis, medication, next_appt, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (email, visit_date, diagnosis, medication, next_appt, notes))
        conn.commit()
        conn.close()
        return redirect('/dashboard')

    return render_template("doctor_tracker.html")


# ── PMS Log ───────────────────────────────────────────────────────────────────
@app.route('/pms_log', methods=['GET', 'POST'])
def pms_log_route():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']

    if request.method == 'POST':
        today    = datetime.date.today().strftime("%Y-%m-%d")
        mood     = int(request.form.get('mood', 0))
        bloating = int(request.form.get('bloating', 0))
        headache = int(request.form.get('headache', 0))
        notes    = request.form.get('notes', '').strip()[:200]

        conn = get_db()
        cur  = conn.cursor()
        # One per day
        cur.execute("SELECT id FROM pms_log WHERE email=? AND date=?", (email, today))
        existing = cur.fetchone()
        if not existing:
            cur.execute("""
                INSERT INTO pms_log (email, date, mood, bloating, headache, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (email, today, mood, bloating, headache, notes))
            conn.commit()
        conn.close()
        return redirect('/dashboard')

    return render_template("pms_log.html")


# ── Debug: show raw Arduino output ───────────────────────────────────────────
@app.route('/debug/serial')
def debug_serial():
    """Visit this route to see exactly what the Arduino is sending."""
    if not ARDUINO_CONNECTED or not arduino:
        return jsonify({"status": "Arduino NOT connected", "ARDUINO_CONNECTED": ARDUINO_CONNECTED})

    lines = []
    try:
        arduino.reset_input_buffer()
        for i in range(10):
            raw = arduino.readline().decode(errors='ignore').strip()
            parsed = parse_sensor(raw)
            lines.append({
                "line":   i + 1,
                "raw":    raw,
                "valid":  parsed['valid'],
                "bpm":    parsed['bpm'],
                "spo2":   parsed['spo2'],
                "temp":   parsed['temperature'],
            })
    except Exception as e:
        lines.append({"error": str(e)})

    return jsonify({"status": "Arduino connected", "readings": lines})


# ── Trends API (JSON for charts) ──────────────────────────────────────────────
@app.route('/api/trends')
def api_trends():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401
    data = get_trends_data(session['user'])
    return jsonify(data)


# ── Cycle daily logs API ──────────────────────────────────────────────────────
@app.route('/api/cycle/<int:cycle_id>/logs')
def api_cycle_logs(cycle_id):
    """Returns all daily logs for a cycle — used by log detail modal."""
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    email = session['user']
    conn  = get_db()
    cur   = conn.cursor()

    # Verify cycle belongs to this user
    cur.execute("SELECT * FROM period_cycles WHERE id=? AND email=?", (cycle_id, email))
    cycle = cur.fetchone()
    if not cycle:
        conn.close()
        return jsonify({"error": "not found"}), 404

    cur.execute("""
        SELECT day_number, date, q1, q2, q3, q4, q5,
               score, analysis, tip, notes, pain_relief
        FROM daily_symptoms
        WHERE email=? AND cycle_id=?
        ORDER BY day_number ASC
    """, (email, cycle_id))
    logs = cur.fetchall()
    conn.close()

    return jsonify({
        "cycle": {
            "id":            cycle['id'],
            "start_date":    cycle['start_date'],
            "end_date":      cycle['end_date'],
            "avg_score":     cycle['avg_score'],
            "worst_day":     cycle['worst_day'],
            "worst_score":   cycle['worst_score'],
            "best_day":      cycle['best_day'],
            "best_score":    cycle['best_score'],
            "overall_label": cycle['overall_label'],
        },
        "logs": [dict(l) for l in logs]
    })


# ── Delete Doctor Visit ───────────────────────────────────────────────────────
@app.route('/doctor_visit/delete/<int:visit_id>', methods=['POST'])
def delete_doctor_visit(visit_id):
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM doctor_visits WHERE id=? AND email=?",
                (visit_id, session['user']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ── Sensor: single sample (no save) ──────────────────────────────────────────
@app.route('/sensor/sample')
def sensor_sample():
    """Returns one raw sensor reading without saving — used by new reading modal."""
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    vitals = read_arduino_line(retries=10)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT age, weight, height FROM users WHERE email = ?", (session['user'],))
    u = cur.fetchone()
    conn.close()

    if not u:
        return jsonify({"error": "user not found"}), 404

    bmr      = calculate_bmr(u['weight'], u['height'], u['age'])
    bmr_stat = get_bmr_status(bmr)
    v_status = vitals_status(vitals['bpm'], vitals['spo2'], vitals['temperature'])
    return jsonify({
        "bpm":         vitals['bpm'],
        "spo2":        vitals['spo2'],
        "temperature": vitals['temperature'],
        "bmr":         round(bmr, 2),
        "bmr_status":  bmr_stat,
        "v_status":    v_status
    })


# ── Sensor: save a confirmed reading ─────────────────────────────────────────
@app.route('/sensor/save', methods=['POST'])
def sensor_save():
    """Saves a confirmed sensor reading to history."""
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    bpm         = data.get('bpm', '—')
    spo2        = data.get('spo2', '—')
    temperature = data.get('temperature', '—')
    bmr         = data.get('bmr', 0)
    now         = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO history (email, bpm, spo2, temperature, bmr, time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (session['user'], str(bpm), str(spo2), str(temperature),
          round(float(bmr), 2), now))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "time": now})



# ── Session Timeout Check ─────────────────────────────────────────────────────
@app.route('/api/keepalive', methods=['POST'])
def keepalive():
    if 'user' not in session and 'admin' not in session:
        return jsonify({"expired": True}), 401
    session.permanent = True
    return jsonify({"ok": True})


# ── Health Intelligence API ───────────────────────────────────────────────────
@app.route('/api/health-intel')
def health_intel():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401
    email = session['user']
    conn  = get_db()
    data  = {
        "anaemia_risk":  get_anaemia_risk(email, conn),
        "cycle_health":  get_cycle_health_score(email, conn),
        "streak":        get_streak(email, conn),
        "missed_alert":  get_missed_period_alert(email, conn),
    }
    conn.close()
    return jsonify(data)


# ── Vitals trend for chart ────────────────────────────────────────────────────
@app.route('/api/vitals-trend')
def vitals_trend():
    if 'user' not in session:
        return jsonify({"error": "login required"}), 401
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""SELECT bpm, spo2, temperature, bmr, time FROM history
                   WHERE email=? ORDER BY time DESC LIMIT 20""", (session['user'],))
    rows = cur.fetchall()
    conn.close()
    rows = list(reversed(rows))
    return jsonify({
        "labels":  [r['time'][5:16] for r in rows],
        "bpm":     [r['bpm']         for r in rows],
        "spo2":    [r['spo2']        for r in rows],
        "temp":    [r['temperature'] for r in rows],
        "bmr":     [r['bmr']         for r in rows],
    })


# ── Admin: Export user data as CSV ───────────────────────────────────────────
@app.route('/admin/export/<email>')
def admin_export_csv(email):
    if 'admin' not in session:
        return redirect('/login')
    import csv, io
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (email,))
    user = cur.fetchone()
    cur.execute("SELECT * FROM daily_symptoms WHERE email=? ORDER BY date", (email,))
    logs = cur.fetchall()
    cur.execute("SELECT * FROM history WHERE email=? ORDER BY time", (email,))
    vitals = cur.fetchall()
    cur.execute("SELECT * FROM doctor_visits WHERE email=? ORDER BY visit_date", (email,))
    visits = cur.fetchall()
    conn.close()

    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(["== USER PROFILE =="])
    if user:
        w.writerow(["Name","Email","Phone","Age","Weight","Height"])
        w.writerow([user['name'], user['email'], user['phone'] or '—',
                    user['age'], user['weight'], user['height']])
    w.writerow([])

    w.writerow(["== VITALS HISTORY =="])
    w.writerow(["Date","BPM","SpO2","Temperature","BMR"])
    for v in vitals:
        w.writerow([v['time'], v['bpm'], v['spo2'], v['temperature'], v['bmr']])
    w.writerow([])

    w.writerow(["== DAILY SYMPTOM LOGS =="])
    w.writerow(["Date","Day","Cramps","Fatigue","Bleeding","Mood","Impact","Score","Flow","Iron","Water","Sleep","Exercise","Notes"])
    for l in logs:
        w.writerow([l['date'], l['day_number'], l['q1'], l['q2'], l['q3'],
                    l['q4'], l['q5'], l['score'],
                    l['flow'] if 'flow' in l.keys() else '—',
                    l['iron_taken'] if 'iron_taken' in l.keys() else '—',
                    l['water'] if 'water' in l.keys() else '—',
                    l['sleep_q'] if 'sleep_q' in l.keys() else '—',
                    l['exercise'] if 'exercise' in l.keys() else '—',
                    l['notes'] or ''])
    w.writerow([])

    w.writerow(["== DOCTOR VISITS =="])
    w.writerow(["Date","Diagnosis","Medication","Next Appt","Notes"])
    for v in visits:
        w.writerow([v['visit_date'], v['diagnosis'], v['medication'],
                    v['next_appt'], v['notes']])

    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment;filename=gracehealth_{email}.csv"}
    )


# ── Admin: DB Backup ──────────────────────────────────────────────────────────
@app.route('/admin/backup')
def admin_backup():
    if 'admin' not in session:
        return redirect('/login')
    import shutil, io
    db_path  = os.path.join(os.path.dirname(__file__), 'database.db')
    from flask import send_file
    return send_file(db_path, as_attachment=True,
                     download_name='gracehealth_backup.db',
                     mimetype='application/octet-stream')


# ── Logout ────────────────────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


if __name__ == "__main__":
    app.run(host='0.0.0.0', debug=True, use_reloader=False)