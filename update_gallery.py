import json
import time
import requests

QUERY = "john kraus"
YEAR_START = 2025
YEAR_END = 2100
PAGES_TO_FETCH = 10
REQUEST_DELAY_S = 0.1

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

def pick_any_jpg(urls):
    for u in urls:
        if u.lower().endswith(".jpg"):
            return u
    return None

def make_variant(url, variant):
    # turns ...~orig.jpg or ...jpg into ...~variant.jpg
    if "~" in url:
        base = url.split("~")[0]
    else:
        base = url.rsplit(".", 1)[0]
    return f"{base}~{variant}.jpg"

def main():
    items = []
    for page in range(1, PAGES_TO_FETCH + 1):
        items.extend(fetch_search_page(page))

    records = []
    for it in items:
        data = (it.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id")
        if not nasa_id:
            continue
        records.append({
            "nasa_id": nasa_id,
            "title": data.get("title", ""),
            "id_date": extract_id_date(nasa_id),
        })

    records.sort(key=lambda r: (r["id_date"], r["nasa_id"]), reverse=True)

    out = []
    for r in records:
        time.sleep(REQUEST_DELAY_S)

        urls = fetch_asset_urls(r["nasa_id"])
        any_jpg = pick_any_jpg(urls)
        if not any_jpg:
            continue

        full_url = make_variant(any_jpg, "orig")
        large_url = make_variant(any_jpg, "large")

        out.append({
            "nasa_id": r["nasa_id"],
            "title": r["title"],
            "id_date": r["id_date"],
            "large_url": large_url,
            "full_url": full_url,
        })

    with open("gallery.json", "w") as f:
        json.dump(out, f, indent=2)

if __name__ == "__main__":
    main()
