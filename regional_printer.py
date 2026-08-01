import requests
from bs4 import BeautifulSoup
import time
import io
from pypdf import PdfReader
from urllib.parse import urljoin

DISGUISE = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
MAX_DEEP_LINKS = 5


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
