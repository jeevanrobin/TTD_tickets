"""Print the date -> avl table from the latest captured get_availability response.

Reads only files already saved by research_interactive.py; makes no network requests.

Usage:
    python inspect_availability.py
"""

import glob
import json
import os

files = glob.glob(os.path.join("output", "interactive_*", "responses", "*_slot_get_availability.json"))
files.sort(key=os.path.getmtime, reverse=True)

if not files:
    print("No get_availability responses found under output/interactive_*/responses/")

for path in files[:1]:
    print("FILE:", path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result = data.get("result", {})

    print("\nDATE -> AVL")
    print("-" * 25)
    for key, value in result.items():
        if key.isdigit() and isinstance(value, dict):
            print(f"{key} -> {value.get('avl')}")

    print("\nOTHER FIELDS")
    print("status:", data.get("status"))
    print("response_time:", data.get("response_time"))
    print("blockedDays:", result.get("blockedDays"))
    print("enableStats:", result.get("enableStats"))
