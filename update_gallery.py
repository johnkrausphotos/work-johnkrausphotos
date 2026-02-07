import json
import time
import requests

QUERY = "john kraus"
YEAR_START = 2025
YEAR_END = 2100
PAGES_TO_FETCH = 10        # increase later if needed
REQUEST_DELAY_S = 0.1      # be polite to NASA API

SEARCH_URL = "https://images-api.nasa.gov/search"
ASSET_URL = "https://images-api.nasa.gov/asset/"

def extract_id_date(nasa_id: str) -> str:
    # Expect NHQYYYYMMDD_...
    if nasa_id.startswith("NHQ") and len(nasa_id) >= 11 and nasa_id[3:11].isdigit():
        return nasa_id[3:11]
    return ""

def fetch_search_page(page: int):
    params = {
        "q": QUERY,
        "media_type": "image",
        "year_start": str(YEAR_START),
        "year_end": str(YEAR_END),
        "page": str(page),
    }
    r = requests.get(SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("collection", {}).get("items", [])

def fetch_asset_urls(nasa_id: str):
    r = requests.get(f"{ASSET_URL}{nasa_id}", timeout=30)
    r.raise_for_status()
    items = r.json().get("collection", {}).get("items", [])
    return [i.get("href") for i in items if i.get("href")]

def pick_original_jpg(urls):
    # Prefer ~orig.jpg, otherwise any JPG that isn't a thumb if possible
    for u in urls:
        if "~orig.jpg" in u.lower():
            return u
    jpgs = [u for u in urls if u.lower().endswith((".jpg", ".jpeg"))]
    for u in jpgs:
        ul = u.lower()
        if "~thumb" not in ul and "~small" not in ul and "~medium" not in ul:
            return u
    return jpgs[0] if jpgs else (urls[0] if urls else None)

def pick_medium_jpg(urls):
    # Prefer ~large.jpg if present, else ~medium.jpg, else ~small.jpg, else any JPG
    for u in urls:
        if "~large.jpg" in u.lower():
            return u
    for u in urls:
        if "~medium.jpg" in u.lower():
            return u
    for u in urls:
        if "~small.jpg" in u.lower():
            return u
    jpgs = [u for u in urls if u.lower().endswith((".jpg", ".jpeg"))]
    return jpgs[0] if jpgs else (urls[0] if urls else None)

def main():
    items = []
    for page in range(1, PAGES_TO_FETCH + 1):
        items.extend(fetch_search_page(page))

    records = []
    for it in items:
        data = (it.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id", "")
        if not nasa_id:
            continue
        records.append({
            "nasa_id": nasa_id,
            "title": data.get("title", ""),
            "id_date": extract_id_date(nasa_id),
        })

    # Sort by embedded date, then nasa_id (newest first)
    records.sort(key=lambda r: (r["id_date"], r["nasa_id"]), reverse=True)

    out = []
    for r in records:
        time.sleep(REQUEST_DELAY_S)
        urls = fetch_asset_urls(r["nasa_id"])

        full = pick_original_jpg(urls)
        medium = pick_medium_jpg(urls)

        if not full or not medium:
            continue

        out.append({
            "nasa_id": r["nasa_id"],
            "title": r["title"],
            "id_date": r["id_date"],
            "medium_url": medium,
            "full_url": full,
        })

    with open("gallery.json", "w") as f:
        json.dump(out, f, indent=2)

if __name__ == "__main__":
    main()
