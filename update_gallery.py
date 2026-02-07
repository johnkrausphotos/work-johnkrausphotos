import json
import time
import requests
import xml.etree.ElementTree as ET

QUERY = "john kraus"
YEAR_START = 2025
YEAR_END = 2100
REQUEST_DELAY_S = 0.15  # polite cadence

SEARCH_URL = "https://images-api.nasa.gov/search"
ASSETS_BASE = "https://images-assets.nasa.gov/image"

# Fetch just the beginning of the JPEG; EXIF + XMP are typically here.
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


def fetch_all_search_items(session: requests.Session):
    """Fetch pages until the API returns an empty items list."""
    all_items = []
    page = 1
    while True:
        items = fetch_search_page(session, page)
        if not items:
            break
        all_items.extend(items)
        page += 1
        time.sleep(REQUEST_DELAY_S)  # be polite to the search endpoint too
    return all_items


def make_full_url(nasa_id: str) -> str:
    return f"{ASSETS_BASE}/{nasa_id}/{nasa_id}~orig.jpg"


def make_large_url(nasa_id: str) -> str:
    return f"{ASSETS_BASE}/{nasa_id}/{nasa_id}~large.jpg"


# -----------------------------
# Minimal EXIF parser (DateTimeOriginal 0x9003 from APP1 Exif)
# -----------------------------
def _u16(b: bytes, off: int, endian: str) -> int:
    return int.from_bytes(b[off:off + 2], endian)


def _u32(b: bytes, off: int, endian: str) -> int:
    return int.from_bytes(b[off:off + 4], endian)


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

        marker = jpg[i + 1]
        i += 2

        # Standalone markers (no length)
        if marker in (0xD9, 0xDA):  # EOI, SOS
            break

        if i + 2 > n:
            break

        seg_len = int.from_bytes(jpg[i:i + 2], "big")
        if seg_len < 2:
            break

        seg_start = i + 2
        seg_end = seg_start + (seg_len - 2)
        if seg_end > n:
            break

        if marker == 0xE1 and (seg_end - seg_start) >= 6:
            if jpg[seg_start:seg_start + 6] == b"Exif\x00\x00":
                return jpg[seg_start + 6:seg_end]  # TIFF header starts here

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

    def read_ifd(ifd_off: int):
        if ifd_off + 2 > len(tiff):
            return []
        count = _u16(tiff, ifd_off, endian)
        entries = []
        base = ifd_off + 2
        for k in range(count):
            off = base + 12 * k
            if off + 12 > len(tiff):
                break
            tag = _u16(tiff, off, endian)
            typ = _u16(tiff, off + 2, endian)
            cnt = _u32(tiff, off + 4, endian)
            val_off = tiff[off + 8:off + 12]
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
                raw = tiff[off:off + byte_count]
            try:
                s = raw.split(b"\x00", 1)[0].decode("ascii", errors="strict").strip()
                return s if s else None
            except Exception:
                return None

    return None


# -----------------------------
# XMP keyword extraction
# -----------------------------
def _find_xmp_packet(jpg: bytes) -> bytes | None:
    # Look for an XMP envelope.
    start = jpg.find(b"<x:xmpmeta")
    if start == -1:
        start = jpg.find(b"<xmpmeta")
        if start == -1:
            return None

    end = jpg.find(b"</x:xmpmeta>", start)
    end_tag = b"</x:xmpmeta>"
    if end == -1:
        end = jpg.find(b"</xmpmeta>", start)
        end_tag = b"</xmpmeta>"
        if end == -1:
            return None

    end += len(end_tag)
    return jpg[start:end]


def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _extract_keywords_from_xmp(xmp: bytes) -> list[str]:
    try:
        xmp_str = xmp.decode("utf-8", errors="ignore").strip("\x00 \t\r\n")
        root = ET.fromstring(xmp_str)
    except Exception:
        return []

    kws: list[str] = []

    # dc:subject -> rdf:Bag -> rdf:li
    # search by localnames to avoid namespace hassle
    for el in root.iter():
        if _localname(el.tag) == "subject":
            for li in el.iter():
                if _localname(li.tag) == "li" and li.text:
                    t = li.text.strip()
                    if t:
                        kws.append(t)

    # lr:hierarchicalSubject (optional): "Program|Artemis II"
    for el in root.iter():
        if _localname(el.tag) == "hierarchicalSubject":
            for li in el.iter():
                if _localname(li.tag) == "li" and li.text:
                    t = li.text.strip()
                    if t:
                        kws.append(t)
                        if "|" in t:
                            leaf = t.split("|")[-1].strip()
                            if leaf:
                                kws.append(leaf)

    # de-dupe preserve order
    seen = set()
    out = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# -----------------------------
# One range fetch -> parse EXIF + XMP
# -----------------------------
def fetch_header_bytes(session: requests.Session, full_url: str) -> bytes | None:
    headers = {"Range": f"bytes=0-{RANGE_BYTES - 1}"}
    r = session.get(full_url, headers=headers, timeout=30)
    if r.status_code not in (200, 206):
        return None
    return r.content


def parse_datetime_and_keywords(jpg_head: bytes) -> tuple[str | None, list[str]]:
    dto = None
    tiff = _find_exif_app1_segment(jpg_head)
    if tiff:
        dto = _extract_datetimeoriginal_from_tiff(tiff)

    keywords: list[str] = []
    xmp = _find_xmp_packet(jpg_head)
    if xmp:
        keywords = _extract_keywords_from_xmp(xmp)

    return dto, keywords


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "johnkraus-nasa-gallery/1.0"})

    # Fetch all search results without a fixed page count
    items = fetch_all_search_items(session)

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

        head = fetch_header_bytes(session, full_url)
        if not head:
            continue

        dto, keywords = parse_datetime_and_keywords(head)
        if not dto:
            continue

        out.append({
            "nasa_id": nasa_id,
            "title": r["title"],
            "id_date": dto,          # "YYYY:MM:DD HH:MM:SS"
            "keywords": keywords,    # list[str]
            "large_url": large_url,
            "full_url": full_url,
        })

    out.sort(key=lambda x: (x["id_date"], x["nasa_id"]), reverse=True)

    with open("gallery.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
