# tkmaxx_gant_watcher.py
import json
import os
import re
import sys
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass
from typing import List, Tuple, Set, Dict

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv

# ----------------- Config -----------------
# Default brand page for GANT on TK Maxx UK (adjust if needed).
BRAND_URL = "https://www.tkmaxx.com/uk/en/search?prefn1=brand&prefv1=GANT"
STATE_FILE = "seen_items.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
TIMEOUT = 25

# ----------------- Utilities -----------------
@dataclass(frozen=True)
class Product:
    pid: str
    url: str
    title: str

def load_state(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except Exception:
        return set()

def save_state(path: str, ids: Set[str]) -> None:
    tmp = {"seen_ids": sorted(ids)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tmp, f, indent=2)

def sanitize_url(url: str, base: str) -> str:
    if not url:
        return ""
    if bool(urlparse(url).netloc):
        return url
    return urljoin(base, url)

# Generic ID finders to be resilient to HTML changes:
PID_PATTERNS = [
    re.compile(r'data-productid="([^"]+)"', re.I),
    re.compile(r'"productID"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'"id"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'"sku"\s*:\s*"([^"]+)"', re.I),
]

TITLE_PATTERNS = [
    re.compile(r'data-productname="([^"]+)"', re.I),
    re.compile(r'"productName"\s*:\s*"([^"]+)"', re.I),
]

def extract_product_ids(html: str) -> Set[str]:
    ids = set()
    for pat in PID_PATTERNS:
        ids.update(pat.findall(html))
    return {i.strip() for i in ids if i.strip()}

def extract_product_titles(html: str) -> Dict[str, str]:
    # Best-effort title mapping if titles are near IDs in JSON blobs
    titles = {}
    for m in TITLE_PATTERNS:
        for t in m.findall(html):
            titles[t] = t
    return titles

def products_from_listing(soup: BeautifulSoup, base: str) -> List[Product]:
    products: List[Product] = []
    # Common product-card patterns
    cards = soup.select('[data-testid*="product-card"], .product-tile, li[data-productid]')
    seen_ids = set()

    for card in cards:
        pid = None
        # Try direct data attributes
        for attr in ("data-productid", "data-sku", "data-itemid", "data-id"):
            pid = card.get(attr) or pid
        # Try regex on element HTML as fallback
        if not pid:
            html = str(card)
            for pat in PID_PATTERNS:
                m = pat.search(html)
                if m:
                    pid = m.group(1)
                    break

        # Get URL
        a = card.find("a", href=True)
        href = sanitize_url(a["href"], base) if a else ""

        # Title
        title = ""
        if a and a.get("title"):
            title = a["title"].strip()
        elif a and a.text:
            title = a.text.strip()
        else:
            # fallback: look for common title nodes
            title_el = card.select_one(".product-name, .name, [data-testid='product-name']")
            if title_el:
                title = title_el.get_text(strip=True)

        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            products.append(Product(pid=pid, url=href, title=title))

    # If no cards found, try a broad regex pass over full page (last resort)
    if not products:
        html = str(soup)
        ids = extract_product_ids(html)
        titles_map = extract_product_titles(html)
        # We may not have URLs in this mode; leave blank if so.
        for pid in ids:
            products.append(Product(pid=pid, url="", title=titles_map.get(pid, "")))

    return products

def find_next_page(soup: BeautifulSoup, base: str) -> str:
    # Look for rel="next" or a pagination next button
    link = soup.find("link", rel="next")
    if link and link.get("href"):
        return sanitize_url(link["href"], base)
    candidates = soup.select('a[rel="next"], a.pagination__next, a[aria-label="Next"]')
    for a in candidates:
        if a.get("href"):
            return sanitize_url(a["href"], base)
    return ""

@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
def fetch(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def crawl_brand(url: str) -> List[Product]:
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    all_products: Dict[str, Product] = {}

    next_url = url
    while next_url:
        html = fetch(next_url)
        soup = BeautifulSoup(html, "html.parser")
        for p in products_from_listing(soup, base):
            all_products[p.pid] = p
        next_url = find_next_page(soup, base)

    return list(all_products.values())

def diff_new_items(current: List[Product], seen_ids: Set[str]) -> Tuple[List[Product], Set[str]]:
    current_ids = {p.pid for p in current}
    new_products = [p for p in current if p.pid not in seen_ids]
    updated_seen = seen_ids | current_ids
    return new_products, updated_seen

def send_email(smtp_host: str, smtp_port: int, username: str, password: str,
               from_addr: str, to_addr: str, subject: str, html_body: str, text_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(username, password)
        s.send_message(msg)

def build_email(new_items: List[Product], brand_url: str) -> Tuple[str, str]:
    if not new_items:
        subject = "TK Maxx GANT watcher: no new items"
        text = "No new items found."
        html = f"<p>No new items found. <a href='{brand_url}'>Check manually</a>.</p>"
        return subject, (html, text)

    subject = f"New TK Maxx GANT item(s): {len(new_items)} found"
    lines = []
    for p in new_items:
        title = p.title or p.pid
        url = p.url or brand_url
        lines.append(f"- {title} — {url}")

    text_body = "New items:\n" + "\n".join(lines)
    items_html = "".join(
        f"<li><a href='{(p.url or brand_url)}'>{(p.title or p.pid)}</a> "
        f"<small>(ID: {p.pid})</small></li>"
        for p in new_items
    )
    html_body = f"""
    <h3>New TK Maxx GANT item(s): {len(new_items)}</h3>
    <ul>{items_html}</ul>
    <p><a href="{brand_url}">See all GANT at TK Maxx</a></p>
    """
    return subject, (html_body, text_body)

def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if not v:
        print(f"Missing env var: {name}", file=sys.stderr)
    return v

def main():
    load_dotenv()

    # 1) Crawl
    products = crawl_brand(BRAND_URL)

    # 2) Compare with state
    seen = load_state(STATE_FILE)
    new_items, updated_seen = diff_new_items(products, seen)

    # 3) Email if new
    smtp_host = env("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = env("SMTP_USERNAME")
    smtp_pass = env("SMTP_PASSWORD")
    email_from = env("EMAIL_FROM")
    email_to = env("EMAIL_TO")

    subject, (html_body, text_body) = build_email(new_items, BRAND_URL)

    if new_items:
        send_email(smtp_host, smtp_port, smtp_user, smtp_pass, email_from, email_to, subject, html_body, text_body)

    # 4) Persist state (even if none new, we refresh the set with current IDs)
    save_state(STATE_FILE, updated_seen)

    # 5) CLI output
    if new_items:
        print(f"Sent email for {len(new_items)} new item(s).")
    else:
        print("No new items. State updated.")

if __name__ == "__main__":
    main()
