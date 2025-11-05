# app.py
import os
import json
import sqlite3
from datetime import datetime, date
from flask import Flask, request, jsonify
import requests
from urllib.parse import urljoin
from dotenv import load_dotenv
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

# Config / env
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
NUTRITIONIX_APP_ID = os.getenv("NUTRITIONIX_APP_ID")
NUTRITIONIX_APP_KEY = os.getenv("NUTRITIONIX_APP_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
PORT = int(os.getenv("FLASK_PORT", 5000))

# DB setup
DB_PATH = "meals.db"

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
        details TEXT -- JSON string with per-item breakdown
    )
    """)
    conn.commit()
    conn.close()

init_db()

app = Flask(__name__)


with open("indian_foods.json", "r") as f:
    FOOD_DB = json.load(f)

def nutritionix_parse(meal_text):
    """
    Parse meal text using local Indian food JSON database
    """
    meal_text = meal_text.lower()
    items = []
    total_cal = 0.0
    total_protein = 0.0

    # Simple matching: look for foods in JSON keys
    for food_name, info in FOOD_DB.items():
        if food_name in meal_text:
            items.append({
                "name": food_name,
                "qty": 1,  # you can enhance parsing for quantities later
                "unit": "serving",
                "calories": info["calories"],
                "protein_g": info["protein_g"]
            })
            total_cal += info["calories"]
            total_protein += info["protein_g"]

    if not items:
        raise ValueError("Food not found in database")

    return {
        "items": items,
        "total_calories": total_cal,
        "total_protein_g": total_protein,
        "raw": {}
    }

# --- Helper: call Nutritionix natural language endpoint ---
def __nutritionix_parse__(natural_text):
    """
    Send natural language text to Nutritionix /natural/nutrients endpoint.
    Returns parsed items and totals.
    Documentation: https://developer.nutritionix.com/docs/v1_1
    """
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

    # Nutritionix returns a list: each item includes nf_calories, nf_protein, food_name, serving_qty, serving_unit
    items = []
    total_cal = 0.0
    total_protein = 0.0
    for it in data.get("foods", []):
        name = it.get("food_name")
        qty = it.get("serving_qty", 1)
        unit = it.get("serving_unit", "")
        calories = float(it.get("nf_calories", 0.0))
        protein = float(it.get("nf_protein", 0.0))
        items.append({
            "name": name,
            "qty": qty,
            "unit": unit,
            "calories": calories,
            "protein_g": protein
        })
        total_cal += calories
        total_protein += protein

    return {"items": items, "total_calories": total_cal, "total_protein_g": total_protein, "raw": data}

# --- Helper: save meal to DB ---
def save_meal(user_id, meal_text, calories, protein_g, details_json):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO meals (user_id, timestamp, meal_text, calories, protein_g, details) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, datetime.utcnow().isoformat(), meal_text, calories, protein_g, details_json)
    )
    conn.commit()
    conn.close()

# --- Helper: get daily totals for user (UTC date) ---
def get_daily_totals(user_id, target_date=None):
    if target_date is None:
        target_date = date.today().isoformat()  # local deployment date; you may want UTC
    # We'll compare by date prefix on timestamp
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    like_pattern = f"{target_date}%"  # assumes timestamp stores date in ISO local date format; we used utc isoformat -> YYYY-MM-DD...
    c.execute("SELECT SUM(calories), SUM(protein_g) FROM meals WHERE user_id = ? AND timestamp LIKE ?", (user_id, like_pattern))
    row = c.fetchone()
    conn.close()
    total_cal = row[0] if row and row[0] is not None else 0.0
    total_protein = row[1] if row and row[1] is not None else 0.0
    return {"date": target_date, "total_calories": total_cal, "total_protein_g": total_protein}

# --- Optional: use OpenAI to extract/clean items and produce reply (nice to have) ---
def openai_clean_and_confirm(meal_text, nutrition_items):
    """
    Use OpenAI to: (a) create a friendly summary of what it inferred, (b) ask clarifying if ambiguous.
    This function is optional — if OPENAI_API_KEY is not set, skip.
    """
    if not OPENAI_API_KEY:
        # Build a simple reply ourselves
        lines = []
        lines.append("I parsed the following items:")
        for it in nutrition_items:
            lines.append(f"- {it['qty']} {it['unit']} {it['name']}: {it['calories']:.0f} kcal, {it['protein_g']:.1f} g protein")
        return "\n".join(lines)
    # Otherwise call OpenAI Chat completions
    import openai
    openai.api_key = OPENAI_API_KEY
    system_prompt = (
        "You are a helpful assistant that summarizes nutrition parsing results. "
        "Given an original user message and a list of parsed nutrition items, return a short friendly message showing each item, calories and protein, "
        "then the meal totals, and then the user's running total for the day if provided. Use short lines, user-friendly language."
    )
    item_lines = "\n".join([f"{it['qty']} {it['unit']} {it['name']} -> {it['calories']:.1f} kcal, {it['protein_g']:.1f} g protein" for it in nutrition_items])
    prompt = f"Original message: {meal_text}\nParsed items:\n{item_lines}\n\nProduce a short friendly reply."
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",  # or text-davinci-003 or another model you have access to
        messages=[{"role":"system","content":system_prompt}, {"role":"user","content":prompt}],
        max_tokens=250,
        temperature=0.2
    )
    return resp["choices"][0]["message"]["content"].strip()

# --- Webhook endpoint for Twilio WhatsApp incoming messages ---
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """
    Twilio will POST form-encoded body with keys like 'From', 'Body'.
    'From' is e.g. 'whatsapp:+919999999999'
    """
    try:
        from_number = request.form.get("From")  # user number
        body = request.form.get("Body", "").strip()
        if not from_number or not body:
            resp = MessagingResponse()
            resp.message("Sorry — couldn't understand your message. Please send text like: 'I had 2 eggs and a slice of toast' or 'totals' or 'clear'.")
            return str(resp)

        # Quick command handling
        lower = body.lower().strip()
        if lower in ("totals", "total", "today"):
            totals = get_daily_totals(user_id=from_number, target_date=date.today().isoformat())
            resp = MessagingResponse()
            resp.message(f"Your totals for {totals['date']}:\nCalories: {totals['total_calories']:.0f} kcal\nProtein: {totals['total_protein_g']:.1f} g")
            return str(resp)

        if lower in ("clear", "clear today", "reset"):
            # Delete meals for user today
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            like_pattern = f"{date.today().isoformat()}%"
            c.execute("DELETE FROM meals WHERE user_id = ? AND timestamp LIKE ?", (from_number, like_pattern))
            conn.commit()
            conn.close()
            resp = MessagingResponse()
            resp.message("Cleared today's meal records.")
            return str(resp)

        # Otherwise interpret as meal description. Send to Nutritionix
        try:
            nutrition = nutritionix_parse(body)
        except Exception as e:
            # error contacting nutrition api
            resp = MessagingResponse()
            resp.message("Sorry, I couldn't reach the nutrition database. Please try again later.")
            return str(resp)

        items = nutrition["items"]
        total_cal = nutrition["total_calories"]
        total_prot = nutrition["total_protein_g"]

        # Save meal to DB
        import json
        save_meal(user_id=from_number, meal_text=body, calories=total_cal, protein_g=total_prot, details_json=json.dumps(items))

        # Prepare the reply: per-item and totals + running total
        daily = get_daily_totals(user_id=from_number, target_date=date.today().isoformat())

        # Friendly message (use OpenAI to prettify if configured)
        try:
            pretty = openai_clean_and_confirm(body, items)
            # Append totals
            pretty += f"\n\nMeal totals: {total_cal:.0f} kcal, {total_prot:.1f} g protein."
            pretty += f"\nToday's running totals: {daily['total_calories']:.0f} kcal, {daily['total_protein_g']:.1f} g protein."
            resp = MessagingResponse()
            resp.message(pretty)
            return str(resp)
        except Exception:
            # fallback if openai fails
            resp = MessagingResponse()
            lines = ["I logged this meal:"]
            for it in items:
                lines.append(f"- {it['qty']} {it['unit']} {it['name']}: {it['calories']:.0f} kcal, {it['protein_g']:.1f} g protein")
            lines.append(f"\nMeal totals: {total_cal:.0f} kcal, {total_prot:.1f} g protein")
            lines.append(f"Today's running totals: {daily['total_calories']:.0f} kcal, {daily['total_protein_g']:.1f} g protein")
            resp.message("\n".join(lines))
            return str(resp)

    except Exception as e:
        # generic error handling
        resp = MessagingResponse()
        resp.message(f"Unexpected error: {str(e)}")
        return str(resp)

# --- Optional endpoints for debugging ---
@app.route("/totals", methods=["GET"])
def totals_debug():
    user = request.args.get("user")
    if not user:
        return jsonify({"error": "provide user param (whatsapp:+NNN)"}), 400
    totals = get_daily_totals(user)
    return jsonify(totals)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
