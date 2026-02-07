import json
import time
import requests

QUERY = "john kraus"
YEAR_START = 2025
YEAR_END = 2100
PAGES_TO_FETCH = 10
REQUEST_DELAY_S = 0.15  # slightly more polite since we'll do 1 CDN range-request per image

SEARCH_URL = "https://images-api.nasa.gov/search"
ASSETS_BASE = "https://images-assets.nasa.gov/image"

# Fetch just the beginning of the JPEG; EXIF lives in the header region.
RANGE_BYTES = 262144  # 256 KiB

def fetch_search_page(session: requests.Session, page: int):
    params = {
        "q": QUERY,
        "media_type": "image",
        "year_start": str(YEAR_START),
        "year_end": str(YEAR_END),
        "page": str(page),
    }
    r = session.get(SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("collection", {}).get("items", [])

def make_full_url(nasa_id: str) -> str:
    return f"{ASSETS_BASE}/{nasa_id}/{nasa_id}~orig.jpg"

def make_large_url(nasa_id: str) -> str:
    return f"{ASSETS_BASE}/{nasa_id}/{nasa_id}~large.jpg"

# --- Minimal EXIF parser to extract DateTimeOriginal (tag 0x9003) from APP1 Exif ---
def _u16(b, off, endian):
    return int.from_bytes(b[off:off+2], endian)

def _u32(b, off, endian):
    return int.from_bytes(b[off:off+4], endian)

def _find_exif_app1_segment(jpg: bytes) -> bytes | None:
    # JPEG segments start after SOI 0xFFD8; look for APP1 0xFFE1 with "Exif\0\0"
    if len(jpg) < 4 or jpg[0:2] != b"\xFF\xD8":
        return None
    i = 2
    n = len(jpg)
    while i + 4 <= n:
        if jpg[i] != 0xFF:
            i += 1
            continue
        marker = jpg[i+1]
        i += 2
        # Standalone markers (no length)
        if marker in (0xD9, 0xDA):  # EOI, SOS
            break
        if i + 2 > n:
            break
        seg_len = int.from_bytes(jpg[i:i+2], "big")
        if seg_len < 2:
            break
        seg_start = i + 2
        seg_end = seg_start + (seg_len - 2)
        if seg_end > n:
            break
        if marker == 0xE1 and (seg_end - seg_start) >= 6:
            if jpg[seg_start:seg_start+6] == b"Exif\x00\x00":
                return jpg[seg_start+6:seg_end]  # TIFF header starts here
        i = seg_end
    return None

def _extract_datetimeoriginal_from_tiff(tiff: bytes) -> str | None:
    # TIFF header: endian(2) + 0x002A + IFD0 offset (4)
    if len(tiff) < 8:
        return None
    endian = "little" if tiff[0:2] == b"II" else "big" if tiff[0:2] == b"MM" else None
    if endian is None:
        return None
    if _u16(tiff, 2, endian) != 0x2A:
        return None
    ifd0_off = _u32(tiff, 4, endian)
    if ifd0_off >= len(tiff):
        return None

    def read_ifd(ifd_off):
        if ifd_off + 2 > len(tiff):
            return []
        count = _u16(tiff, ifd_off, endian)
        entries = []
        base = ifd_off + 2
        for k in range(count):
            off = base + 12*k
            if off + 12 > len(tiff):
                break
            tag = _u16(tiff, off, endian)
            typ = _u16(tiff, off+2, endian)
            cnt = _u32(tiff, off+4, endian)
            val_off = tiff[off+8:off+12]
            entries.append((tag, typ, cnt, val_off))
        return entries

    # Find ExifIFD pointer (0x8769) in IFD0
    exif_ifd_ptr = None
    for tag, typ, cnt, val_off in read_ifd(ifd0_off):
        if tag == 0x8769:
            exif_ifd_ptr = int.from_bytes(val_off, endian)
            break
    if exif_ifd_ptr is None or exif_ifd_ptr >= len(tiff):
        return None

    # In ExifIFD, look for DateTimeOriginal (0x9003), ASCII type=2
    for tag, typ, cnt, val_off in read_ifd(exif_ifd_ptr):
        if tag == 0x9003 and typ == 2 and cnt > 0:
            byte_count = cnt
            if byte_count <= 4:
                raw = val_off[:byte_count]
            else:
                off = int.from_bytes(val_off, endian)
                if off + byte_count > len(tiff):
                    return None
                raw = tiff[off:off+byte_count]
            try:
                s = raw.split(b"\x00", 1)[0].decode("ascii", errors="strict").strip()
                return s if s else None
            except Exception:
                return None
    return None

def fetch_datetimeoriginal(session: requests.Session, full_url: str) -> str | None:
    headers = {"Range": f"bytes=0-{RANGE_BYTES-1}"}
    r = session.get(full_url, headers=headers, timeout=30)
    # Some servers might ignore Range and return 200 full content; still works.
    if r.status_code not in (200, 206):
        return None
    jpg = r.content
    tiff = _find_exif_app1_segment(jpg)
    if not tiff:
        return None
    return _extract_datetimeoriginal_from_tiff(tiff)

def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "johnkraus-nasa-gallery/1.0"})

    items = []
    for page in range(1, PAGES_TO_FETCH + 1):
        items.extend(fetch_search_page(session, page))

    records = []
    for it in items:
        data = (it.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id")
        if not nasa_id:
            continue
        records.append({
            "nasa_id": nasa_id,
            "title": data.get("title", ""),
        })

    out = []
    for r in records:
        time.sleep(REQUEST_DELAY_S)

        nasa_id = r["nasa_id"]
        full_url = make_full_url(nasa_id)
        large_url = make_large_url(nasa_id)

        dto = fetch_datetimeoriginal(session, full_url)
        if not dto:
            # You said DateTimeOriginal will always be preserved.
            # If this ever happens, skip to avoid incorrect ordering.
            continue

        out.append({
            "nasa_id": nasa_id,
            "title": r["title"],
            "id_date": dto,          # now equals EXIF DateTimeOriginal (e.g. "2026:01:09 14:32:10")
            "large_url": large_url,
            "full_url": full_url,
        })

    # Sort newest first by EXIF DateTimeOriginal, then nasa_id
    out.sort(key=lambda x: (x["id_date"], x["nasa_id"]), reverse=True)

    with open("gallery.json", "w") as f:
        json.dump(out, f, indent=2)

if __name__ == "__main__":
    main()
