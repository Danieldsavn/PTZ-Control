"""Increment version in version.json by 0.1. Run from project root."""
import json
import os

VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")
DEFAULT = "1.0"

def main():
    try:
        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            ver = data.get("version", DEFAULT)
        else:
            ver = DEFAULT
        try:
            n = float(str(ver).strip())
        except ValueError:
            n = 1.0
        n = round(n + 0.1, 1)
        new_ver = str(n) if n != int(n) else str(int(n)) + ".0"
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": new_ver}, f, indent=2)
        print("Version set to", new_ver)
    except Exception as e:
        print("Error:", e)
        raise

if __name__ == "__main__":
    main()
