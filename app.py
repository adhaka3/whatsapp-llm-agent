import os
import sqlite3
from datetime import datetime, date
from flask import Flask, request
import requests
import json
from twilio.twiml.messaging_response import MessagingResponse

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
NUTRITIONIX_APP_ID = os.getenv("NUTRITIONIX_APP_ID")
NUTRITIONIX_APP_KEY = os.getenv("NUTRITIONIX_APP_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

app = Flask(__name__)

DB_PATH = "meals.db"

# --- Initialize DB ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        meal_text TEXT,
        calories REAL,
        protein_g REAL,
        details TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Nutritionix parser ---
def nutritionix_parse(natural_text):
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id": NUTRITIONIX_APP_ID,
        "x-app-key": NUTRITIONIX_APP_KEY,
        "Content-Type": "application/json"
    }
    payload = {"query": natural_text}
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = []
    total_cal = 0.0
    total_protein = 0.0
    for it in data.get("foods", []):
        name = it.get("food_name")
        qty = it.get("serving_qty", 1)
        unit = it.get("serving_unit", "")
        calories = float(it.get("nf_calories", 0.0))
        protein = float(it.get("nf_protein", 0.0))
        items.append({"name": name, "qty": qty, "unit": unit, "calories": calories, "protein_g": protein})
        total_cal += calories
        total_protein += protein
    return {"items": items, "total_calories": total_cal, "total_protein_g": total_protein, "raw": data}

# --- DB helpers ---
def save_meal(user_id, meal_text, calories, protein_g, details_json):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO meals (user_id, timestamp, meal_text, calories, protein_g, details) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, datetime.utcnow().isoformat(), meal_text, calories, protein_g, details_json)
    )
    conn.commit()
    conn.close()

def get_daily_totals(user_id, target_date=None):
    if target_date is None:
        target_date = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    like_pattern = f"{target_date}%"
    c.execute("SELECT SUM(calories), SUM(protein_g) FROM meals WHERE user_id = ? AND timestamp LIKE ?", (user_id, like_pattern))
    row = c.fetchone()
    conn.close()
    total_cal = row[0] if row and row[0] is not None else 0.0
    total_protein = row[1] if row and row[1] is not None else 0.0
    return {"date": target_date, "total_calories": total_cal, "total_protein_g": total_protein}

# --- OpenAI optional friendly message ---
def openai_clean_and_confirm(meal_text, nutrition_items):
    if not OPENAI_API_KEY:
        lines = ["I parsed the following items:"]
        for it in nutrition_items:
            lines.append(f"- {it['qty']} {it['unit']} {it['name']}: {it['calories']:.0f} kcal, {it['protein_g']:.1f} g protein")
        return "\n".join(lines)
    import openai
    openai.api_key = OPENAI_API_KEY
    system_prompt = "Summarize parsed nutrition items and make it user-friendly."
    item_lines = "\n".join([f"{it['qty']} {it['unit']} {it['name']} -> {it['calories']:.1f} kcal, {it['protein_g']:.1f} g protein" for it in nutrition_items])
    prompt = f"Original message: {meal_text}\nParsed items:\n{item_lines}\nProduce a short friendly reply."
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":prompt}],
        max_tokens=250,
        temperature=0.2
    )
    return resp["choices"][0]["message"]["content"].strip()

# --- WhatsApp webhook ---
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get("From")
    body = request.form.get("Body", "").strip()
    resp = MessagingResponse()
    if not from_number or not body:
        resp.message("Send something like '2 eggs and a slice of toast', 'totals' or 'clear'.")
        return str(resp)
    lower = body.lower().strip()
    if lower in ("totals","total","today"):
        totals = get_daily_totals(from_number)
        resp.message(f"Your totals for {totals['date']}:\nCalories: {totals['total_calories']:.0f} kcal\nProtein: {totals['total_protein_g']:.1f} g")
        return str(resp)
    if lower in ("clear","clear today","reset"):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM meals WHERE user_id = ? AND timestamp LIKE ?", (from_number,f"{date.today().isoformat()}%"))
        conn.commit()
        conn.close()
        resp.message("Cleared today's meal records.")
        return str(resp)
    try:
        nutrition = nutritionix_parse(body)
    except:
        resp.message("Couldn't reach nutrition database. Try again later.")
        return str(resp)
    items = nutrition["items"]
    total_cal = nutrition["total_calories"]
    total_prot = nutrition["total_protein_g"]
    save_meal(from_number, body, total_cal, total_prot, json.dumps(items))
    daily = get_daily_totals(from_number)
    try:
        pretty = openai_clean_and_confirm(body, items)
        pretty += f"\n\nMeal totals: {total_cal:.0f} kcal, {total_prot:.1f} g protein."
        pretty += f"\nToday's running totals: {daily['total_calories']:.0f} kcal, {daily['total_protein_g']:.1f} g protein."
        resp.message(pretty)
    except:
        lines = ["Logged meal:"]
        for it in items:
            lines.append(f"- {it['qty']} {it['unit']} {it['name']}: {it['calories']:.0f} kcal, {it['protein_g']:.1f} g protein")
        lines.append(f"\nMeal totals: {total_cal:.0f} kcal, {total_prot:.1f} g protein")
        lines.append(f"Today's running totals: {daily['total_calories']:.0f} kcal, {daily['total_protein_g']:.1f} g protein")
        resp.message("\n".join(lines))
    return str(resp)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
