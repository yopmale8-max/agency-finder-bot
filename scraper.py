import gspread
from google.oauth2.service_account import Credentials
import requests
from bs4 import BeautifulSoup
import datetime
import time

# ============================================================
# SETTINGS
# ============================================================
CUTOFF_DAYS = 365
ENUMERATION_PAGES = 20
ROWS_PER_PAGE = 100
SHEET_NAME = "Abroad Agencies"
BASE_URL = "https://nr-review.com"
MIN_EXPECTED_ROWS = 50  # self-healing guard: refuse to overwrite good data with a suspiciously small scrape

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AgencyFinderBot/1.0)"}


def log_system_event(client, component, status, details):
    """Writes a row to the SystemLog tab so scraper health is visible in
    the same place as the digest/payment system's own health logs."""
    try:
        sheet = client.open(SHEET_NAME).worksheet("SystemLog")
        sheet.append_row([datetime.datetime.now().isoformat(), component, status, str(details)[:500]])
    except Exception as e:
        print(f"Could not write to SystemLog (non-fatal): {e}")


def fetch_with_retry(url, max_attempts=3, delay=2):
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp
        except Exception as e:
            print(f"    Attempt {attempt}/{max_attempts} failed for {url}: {e}")
            if attempt < max_attempts:
                time.sleep(delay)
    return None


def find_data_table(soup, must_contain_any):
    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True).lower()
        if any(keyword.lower() in header_text for keyword in must_contain_any):
            return table
    return None


def parse_date_safe(date_str):
    date_str = date_str.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def discover_agencies():
    print(f"Scanning {ENUMERATION_PAGES} pages of job listings to find agencies...")
    agencies = {}

    for page in range(1, ENUMERATION_PAGES + 1):
        url = f"{BASE_URL}/FindJobs?page={page}&rows={ROWS_PER_PAGE}"
        resp = fetch_with_retry(url)
        if resp is None:
            print(f"  Page {page}: failed to fetch after retries, skipping")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        table = find_data_table(soup, ["Job Title", "Agency"])
        if not table:
            print(f"  Page {page}: could not find the listings table, skipping")
            continue

        rows = table.find_all("tr")[1:]
        for row in rows:
            link = row.find("a", href=lambda h: h and "/Agency/" in h)
            if link:
                name = link.get_text(strip=True)
                href = link["href"]
                full_url = href if href.startswith("http") else BASE_URL + href
                agencies[name] = full_url

        print(f"  Page {page}: {len(agencies)} unique agencies found so far")
        time.sleep(0.5)

    return agencies


def scrape_agency_profile(agency_name, profile_url, cutoff_date, debug_used):
    resp = fetch_with_retry(profile_url)
    if resp is None:
        print(f"    Failed to fetch {agency_name} after retries, skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text("\n", strip=True)

    def extract_field(labels):
        lowered = page_text.lower()
        for label in labels:
            idx = lowered.find(label.lower())
            if idx == -1:
                continue
            segment = page_text[idx + len(label):idx + len(label) + 150]
            segment = segment.lstrip(": \n").strip()
            value = segment.split("\n")[0].strip()
            if value:
                return value
        return ""

    address = extract_field(["Office Address", "Address"])
    contact = extract_field(["Contact Number", "Contact", "Telephone", "Mobile", "Phone"])
    license_status = extract_field(["DMW License Status", "License Status"])

    if not address and not contact and not debug_used[0]:
        debug_used[0] = True
        print("    [DEBUG] Could not find address/contact. First 400 characters of page text:")
        print("    " + page_text[:400].replace("\n", " | "))

    table = find_data_table(soup, ["Position", "Approval Date"])
    if not table:
        return []

    results = []
    rows = table.find_all("tr")[1:]
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 5:
            continue
        job_title, location, vacancy, principal, approval_date_str = cells[0], cells[1], cells[2], cells[3], cells[4]

        approved_date = parse_date_safe(approval_date_str)
        if not approved_date or approved_date < cutoff_date:
            continue

        results.append([
            agency_name,
            license_status or "Status not listed",
            job_title,
            location,
            vacancy,
            address,
            contact,
            approved_date.strftime("%Y-%m-%d")
        ])

    return results


def run_scraper():
    print("Connecting to Google Sheets...")
    client = None
    try:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        print("Connected successfully!")
    except Exception as e:
        print(f"Error connecting to Google Sheet: {e}")
        return  # can't even log this failure without a client — GitHub's own
                 # failure-notification email is the fallback here

    try:
        cutoff_date = datetime.date.today() - datetime.timedelta(days=CUTOFF_DAYS)
        print(f"Only keeping jobs approved on/after {cutoff_date}")

        agencies = discover_agencies()
        print(f"\nFound {len(agencies)} unique agencies. Now checking each for recent jobs...\n")

        all_rows = []
        debug_used = [False]
        error_count = 0
        for i, (name, url) in enumerate(agencies.items(), start=1):
            print(f"[{i}/{len(agencies)}] Checking {name}...")
            try:
                rows = scrape_agency_profile(name, url, cutoff_date, debug_used)
                all_rows.extend(rows)
            except Exception as e:
                error_count += 1
                print(f"    Unexpected error on {name}, skipping: {e}")
            time.sleep(0.5)

        print(f"\nCollected {len(all_rows)} recent job rows total.")

        if len(all_rows) < MIN_EXPECTED_ROWS:
            # Self-healing: a near-empty result usually means the source site's
            # HTML structure changed and our parser broke, not that jobs
            # genuinely dried up. Refuse to overwrite good existing data with
            # a broken scrape — leave the sheet untouched and flag it instead.
            msg = (f"Only found {len(all_rows)} rows (expected at least {MIN_EXPECTED_ROWS}). "
                   f"This usually means the source site's structure changed. Skipping the sheet "
                   f"update to protect existing data — please check the scraper manually.")
            print(msg)
            log_system_event(client, "Scraper", "ERROR", msg)
            return

        print("Clearing old data (keeping header row) and writing fresh data...")
        sheet.batch_clear(["A2:H100000"])
        sheet.append_rows(all_rows)

        print("Done! Check your Google Sheet.")
        log_system_event(
            client, "Scraper", "OK",
            f"Scraped {len(all_rows)} rows from {len(agencies)} agencies ({error_count} agency error(s) skipped)"
        )

    except Exception as e:
        print(f"Fatal scraper error: {e}")
        log_system_event(client, "Scraper", "ERROR", str(e))


if __name__ == "__main__":
    run_scraper()
