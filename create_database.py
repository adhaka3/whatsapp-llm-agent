import pandas as pd
import json

# Load Excel
df = pd.read_excel("Anuvaad_INDB_2024.11.xlsx")

# Normalize column names (optional)
df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

# Convert to dictionary
food_dict = {}
for _, row in df.iterrows():
    food_name = row["food_name"].lower().strip()
    food_dict[food_name] = {
        "calories": float(row["energy_kcal"]),
        "protein_g": float(row["protein_g"])
    }

# Save as JSON
with open("indian_foods.json", "w") as f:
    json.dump(food_dict, f, indent=4)

print("JSON database saved as indian_foods.json")
