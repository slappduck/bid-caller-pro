import requests
from bs4 import BeautifulSoup
import os
import time
import io
from pypdf import PdfReader
from urllib.parse import urljoin

DISGUISE = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
MASTER_FILE = "all_regional_intel.txt"
MAX_DEEP_LINKS = 5


def _append_packet(master_file, city, label, text_content):
    if not text_content or not text_content.strip():
        return
    clean = "\n".join(l.strip() for l in text_content.splitlines() if l.strip())
    with open(master_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n=== PACKET | CITY: {city} | PART: {label} ===\n")
        f.write(clean)


def scrape_city_to_file(city, url, master_file=MASTER_FILE, max_deep=MAX_DEEP_LINKS):
    """Scrape one city's bids page (+ deep links/PDFs) into the master file.
    Returns True if the main page was reachable."""
    print(f"--- Scanning: {city}  ({url}) ---")
    try:
        response = requests.get(url, headers=DISGUISE, timeout=15)
        if response.status_code != 200:
            print(f"   ❌ {city}: HTTP {response.status_code}")
            return False

        soup = BeautifulSoup(response.text, 'html.parser')
        _append_packet(master_file, city, "MAIN_PAGE", soup.get_text(separator="\n"))
        print(f"   ✅ Saved main page.")

        sub_links = []
        for link in soup.find_all('a', href=True):
            hl = link['href'].lower()
            if "viewfile" in hl or "bidid" in hl or "bids.aspx?bid" in hl:
                full = urljoin(url, link['href'])
                if full not in sub_links and full != url:
                    sub_links.append(full)

        count = 0
        for sub_url in sub_links:
            if count >= max_deep:
                break
            try:
                sub_res = requests.get(sub_url, headers=DISGUISE, timeout=15)
                sample = sub_res.content.strip()
                if sample.startswith(b'%PDF'):
                    try:
                        with io.BytesIO(sub_res.content) as fpdf:
                            reader = PdfReader(fpdf)
                            txt = ""
                            for p in range(min(5, len(reader.pages))):
                                t = reader.pages[p].extract_text()
                                if t:
                                    txt += t + "\n"
                        _append_packet(master_file, city, f"PDF_{count}", txt)
                    except Exception:
                        print(f"   ⚠️ Unreadable PDF, skipping.")
                else:
                    ssoup = BeautifulSoup(sub_res.text, 'html.parser')
                    _append_packet(master_file, city, f"WEB_{count}", ssoup.get_text(separator="\n"))
                count += 1
                time.sleep(1)
            except Exception as e:
                print(f"   ⚠️ Deep-link skipped: {e}")
        return True

    except Exception as e:
        print(f"   💥 Error on {city}: {e}")
        return False


def run_scraper(master_file=MASTER_FILE):
    """Default quick scan of the 5 SW-Missouri hubs."""
    if os.path.exists(master_file):
        os.remove(master_file)
    print("🚀 Quick scan of default 5 cities...\n")
    targets = {
        "Aurora":      "https://www.aurora-cityhall.org/AgendaCenter",
        "Springfield": "https://www.springfieldmo.gov/bids.aspx",
        "Joplin":      "https://www.joplinmo.org/agendacenter",
        "Republic":    "https://www.republicmo.com/Bids.aspx",
        "Ozark":       "https://ozarkmissouri.com/Bids.aspx",
    }
    for city, url in targets.items():
        scrape_city_to_file(city, url, master_file)
        time.sleep(0.5)
    print(f"\n🎉 Done → {master_file}")


def search_custom_city(city_name, url):
    """Scrape any single page and return a list of structured bid dicts."""
    collected = ""
    try:
        response = requests.get(url, headers=DISGUISE, timeout=15)
        if response.status_code != 200:
            return []
        soup = BeautifulSoup(response.text, 'html.parser')
        collected += soup.get_text(separator="\n")

        sub_links = []
        for link in soup.find_all('a', href=True):
            hl = link['href'].lower()
            if any(k in hl for k in ["bid", "rfp", "rfq", "proposal", "viewfile",
                                     "contract", "solicitation"]):
                full = urljoin(url, link['href'])
                if full not in sub_links and full != url:
                    sub_links.append(full)

        count = 0
        for sub_url in sub_links:
            if count >= MAX_DEEP_LINKS:
                break
            try:
                sub_res = requests.get(sub_url, headers=DISGUISE, timeout=15)
                sample = sub_res.content.strip()
                if sample.startswith(b'%PDF'):
                    try:
                        with io.BytesIO(sub_res.content) as fpdf:
                            reader = PdfReader(fpdf)
                            txt = ""
                            for p in range(min(5, len(reader.pages))):
                                t = reader.pages[p].extract_text()
                                if t:
                                    txt += t + "\n"
                        collected += f"\n[PDF {sub_url}]\n{txt}"
                    except Exception:
                        pass
                else:
                    ssoup = BeautifulSoup(sub_res.text, 'html.parser')
                    collected += f"\n[PAGE {sub_url}]\n{ssoup.get_text(separator=chr(10))}"
                count += 1
                time.sleep(1)
            except Exception:
                pass
    except Exception as e:
        print(f"Error scraping {city_name}: {e}")
        return []

    return _extract_bids_with_ai(city_name, collected)


def _extract_bids_with_ai(city_name, raw_text):
    """Send raw text to OUR server for AI extraction (no local Ollama needed)."""
    if not raw_text.strip():
        return []
    try:
        import subscription
        url = subscription.SERVER_URL.rstrip("/") + "/extract"
        payload = {
            "key": subscription._load_cache().get("key", ""),
            "device_id": subscription._device_id(),
            "city": city_name,
            "text": raw_text,
        }
        r = requests.post(url, json=payload, timeout=130)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok") and isinstance(data.get("bids"), list):
                return data["bids"]
    except Exception as e:
        print(f"AI extraction failed for {city_name}: {e}")
    return []
