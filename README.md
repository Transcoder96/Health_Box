You can access the report of the project if you dont want to go through this readme file-   https://drive.google.com/drive/folders/1uCVufOU_4yq-jIXGxZY6R3kiptCeHD4p?usp=sharing

🌸 GraceHealth — Women’s Health Monitoring System

A full-stack IoT + Web-based health monitoring platform designed specifically for women, combining real-time sensor data, period tracking, and health analytics.

---

 📌 Overview

GraceHealth is a Flask-based web application integrated with Arduino sensors that enables women to:

Monitor heart rate (BPM), SpO₂, and body temperature
Track menstrual cycles and symptoms
Maintain doctor visit records
Generate health reports
Receive personalized health insights

Built for accessibility, especially in semi-urban and rural environments

---

 🚀 Features

 👩‍⚕️ User Features

Secure signup/login system
Profile management with health parameters
Period Health Assessment (10 questions)
Daily Symptom Logging during cycles
PMS tracking system
Doctor visit tracking
Personalized dashboard with analytics and alerts

---

 📊 Health Monitoring

Real-time data from Arduino:

Heart Rate (BPM)
SpO₂
Body Temperature

Automatic health status classification
BMR calculation (Mifflin-St Jeor)

---

 🧠 Intelligence Layer

Cycle analysis (best and worst days)
Health scoring system
Smart notifications (missed cycle, age-based insights)

---

 🛠️ Admin Panel

View all users and health data
Identify flagged health cases
Access detailed user reports

---
 🧱 Tech Stack

Layer        : Backend        → Python (Flask)
Layer        : Database       → SQLite
Layer        : Frontend       → HTML, CSS (Custom UI)
Layer        : Hardware       → Arduino Uno
Layer        : Sensors        → MAX30102, DS18B20
Layer        : Reports        → ReportLab
Layer        : Communication  → Serial (PySerial)

---

 📂 Project Structure

GraceHealth/

app.py                  # Main Flask application
database.db             # SQLite database

templates/
login.html
signup.html
dashboard.html
assessment.html
daily_log.html
admin.html
admin_user.html
edit_profile.html
doctor_tracker.html
pms_log.html

static/
style.css

GraceHealth_Master_Documentation.pdf
GraceHealth_Technical_Support_Guide.pdf

---

 ⚙️ Installation & Setup

Install dependencies:

pip install flask pyserial reportlab

Run the application:

python app.py

Open browser:

[http://127.0.0.1:5000](http://127.0.0.1:5000)

---

 🔌 Arduino Setup

Sensors used:

MAX30102 → Heart rate and SpO₂
DS18B20 → Temperature

Connections:

MAX30102 SDA → A4
MAX30102 SCL → A5
DS18B20 DATA → D4
DS18B20 VCC → 5V
DS18B20 GND → GND

Use a 4.7k resistor between DATA and VCC for DS18B20

---

 ⚙️ Serial Configuration

Update COM port in app.py:

arduino = serial.Serial('COM7', 9600, timeout=1)

---

 🔐 Admin Access

Email: [admin@gracehealth.com](mailto:admin@gracehealth.com)
Password: Admin@1234

---

 📊 Database Design

users
history (sensor + BMR data)
period_health_assessment
period_cycles
daily_symptoms
doctor_visits
medication_log
pms_log

---

 🎯 Target Users

Girls (10–14) → First cycle tracking
Teenagers → PMS and irregular cycles
Adults → Hormonal and reproductive health
Seniors → Vital monitoring

---
 💡 Key Concept

“Your Health, Your Power” — A data-driven digital companion for women’s health

---
 ⚠️ Known Limitations

Requires Arduino hardware for full functionality
SQLite not ideal for large-scale deployment
Serial communication depends on correct COM port

---
 🔮 Future Improvements

Mobile app (Android/iOS)
Cloud database (Firebase or PostgreSQL)
AI-based prediction system
Doctor integration and telemedicine
Multi-language support



 👨‍💻 Author

Surya Prakash Jha
GraceHealth Project — 2026
