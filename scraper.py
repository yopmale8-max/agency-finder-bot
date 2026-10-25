import argparse
import datetime
import json
from email.message import EmailMessage
from collections import Counter
from pathlib import Path
import os
import smtplib
import ssl
import time
import traceback
from urllib.parse import urljoin

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials


CUTOFF_DAYS = 365
ROWS_PER_PAGE = 100
SHEET_NAME = "Abroad Agencies"
BASE_URL = "https://nr-review.com"
REPORT_RECIPIENT = "yopmale8@gmail.com"
SHEET_HEADERS = [
    "Agency Name",
    "DMW License No",
    "Job Title",
    "Country",
    "Vacancies",
    "Address",
    "Contact Number",
    "Date Approved",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AgencyFinderBot/1.0)"}


class ScrapeError(RuntimeError):
    """Raised when source data cannot be trusted as a complete scrape."""


def get_google_credentials():
    """Resolves service account credentials from local file or environment variable."""
    credentials_path = Path(__file__).resolve().with_name("credentials.json")
    env_creds = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    errors = []
    last_error = None

    if credentials_path.is_file():
        try:
            return Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
        except Exception as error:
            last_error = error
            errors.append(f"credentials.json: {error}")
            if env_creds:
                print(f"Could not load local credentials.json; trying GOOGLE_CREDENTIALS: {error}")

    if env_creds:
        try:
            creds_info = json.loads(env_creds)
            if not isinstance(creds_info, dict):
                raise ValueError("the JSON value must be a service-account object")
            return Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        except Exception as error:
            last_error = error
            errors.append(f"GOOGLE_CREDENTIALS: {error}")

    if errors:
        raise ScrapeError("Could not load Google credentials. " + "; ".join(errors)) from last_error
    raise ScrapeError(
        "Google credentials not found. Provide either a 'credentials.json' file "
        "or set the GOOGLE_CREDENTIALS environment variable."
    )


def log_system_event(client, component, status, details):
    """Writes a row to the SystemLog tab so scraper health is visible."""
    try:
        sheet = client.open(SHEET_NAME).worksheet("SystemLog")
        sheet.append_row(
            [datetime.datetime.now().isoformat(), component, status, str(details)[:500]]
        )
    except Exception as error:
        print(f"Could not write to SystemLog: {error}")


def fetch_with_retry(url, max_attempts=3, delay=2):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            print(f"    Attempt {attempt}/{max_attempts} failed for {url}: {error}")
            if attempt < max_attempts:
                time.sleep(delay)
    raise ScrapeError(f"Could not fetch {url} after {max_attempts} attempts: {last_error}")


def find_data_table(soup, must_contain_all):
    required = [keyword.casefold() for keyword in must_contain_all]
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        header_text = header_row.get_text(" ", strip=True).casefold()
        if all(keyword in header_text for keyword in required):
            return table
    return None


def parse_date_safe(date_str):
    date_str = date_str.strip()
    if not date_str:
        return None

    iso_candidate = date_str[:-1] + "+00:00" if date_str.endswith("Z") else date_str
    try:
        return datetime.datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def discover_agencies():
    print("Scanning paginated job listings until the source returns a short page...")
    agencies = {}
    page = 1

    while True:
        url = f"{BASE_URL}/FindJobs?page={page}&rows={ROWS_PER_PAGE}"
        response = fetch_with_retry(url)
        soup = BeautifulSoup(response.text, "html.parser")
        table = find_data_table(soup, ["Job Title", "Agency"])
        if table is None:
            raise ScrapeError(f"Page {page} did not contain the expected job-listings table.")

        rows = table.find_all("tr")[1:]
        page_agencies = 0
        for row_number, row in enumerate(rows, start=2):
            link = row.find("a", href=lambda href: href and "/Agency/" in href)
            if link is None:
                raise ScrapeError(
                    f"Page {page}, row {row_number} is missing its agency profile link."
                )

            name = link.get_text(strip=True)
            profile_url = urljoin(BASE_URL + "/", link["href"])
            if not name:
                raise ScrapeError(f"Page {page}, row {row_number} has a blank agency name.")
            if profile_url not in agencies:
                agencies[profile_url] = name
                page_agencies += 1

        print(
            f"  Page {page}: {len(rows)} listing rows, "
            f"{page_agencies} new agencies, {len(agencies)} unique agencies total"
        )
        if len(rows) < ROWS_PER_PAGE:
            break

        page += 1
        time.sleep(0.5)

    return agencies


def scrape_agency_profile(agency_name, profile_url, cutoff_date, debug_used):
    response = fetch_with_retry(profile_url)
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    lowered_text = page_text.casefold()

    def extract_field(labels):
        for label in labels:
            index = lowered_text.find(label.casefold())
            if index == -1:
                continue
            segment = page_text[index + len(label):index + len(label) + 150]
            value = segment.lstrip(": \n").strip().split("\n")[0].strip()
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
    if table is None:
        raise ScrapeError(f"{agency_name}: profile did not contain the expected job-orders table.")

    results = []
    for row_number, row in enumerate(table.find_all("tr")[1:], start=2):
        cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
        if not cells:
            continue
        if len(cells) < 5:
            raise ScrapeError(
                f"{agency_name}: job-orders row {row_number} has only {len(cells)} cells."
            )

        job_title, location, vacancy = cells[0], cells[1], cells[2]
        approval_date_str = cells[4]
        approved_date = parse_date_safe(approval_date_str)
        if approved_date is None:
            raise ScrapeError(
                f"{agency_name}: could not parse approval date {approval_date_str!r} "
                f"on job-orders row {row_number}."
            )
        if approved_date < cutoff_date:
            continue

        results.append(
            [
                agency_name,
                license_status or "Status not listed",
                job_title,
                location,
                vacancy,
                address,
                contact,
                approved_date.isoformat(),
            ]
        )

    return results


def normalized_row(row):
    return tuple(str(row[index]).strip() if index < len(row) else "" for index in range(8))


def count_new_rows(existing_values, scraped_rows):
    existing_rows = Counter(
        normalized_row(row)
        for row in existing_values[1:]
        if any(str(value).strip() for value in row)
    )
    scraped_counts = Counter(normalized_row(row) for row in scraped_rows)
    return sum((scraped_counts - existing_rows).values())


def validate_sheet_headers(existing_values):
    if not existing_values:
        raise ScrapeError("The destination worksheet is empty; its expected header row is missing.")
    actual_headers = [str(value).strip() for value in existing_values[0][: len(SHEET_HEADERS)]]
    if actual_headers != SHEET_HEADERS:
        raise ScrapeError(
            f"Unexpected worksheet headers: {actual_headers!r}; expected {SHEET_HEADERS!r}."
        )


def replace_sheet_rows_atomically(sheet, rows):
    """Replaces A2:H in one Sheets API batch, preserving old data on failure."""
    required_row_count = len(rows) + 1
    target_row_count = max(sheet.row_count, required_row_count)
    requests_body = []

    if required_row_count > sheet.row_count:
        requests_body.append(
            {
                "appendDimension": {
                    "sheetId": sheet.id,
                    "dimension": "ROWS",
                    "length": required_row_count - sheet.row_count,
                }
            }
        )

    if rows:
        requests_body.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                    },
                    "rows": [
                        {
                            "values": [
                                {"userEnteredValue": {"stringValue": str(value)}}
                                for value in row
                            ]
                        }
                        for row in rows
                    ],
                    "fields": "userEnteredValue",
                }
            }
        )

    if target_row_count > required_row_count:
        requests_body.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": required_row_count,
                        "endRowIndex": target_row_count,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(SHEET_HEADERS),
                    },
                    "cell": {},
                    "fields": "userEnteredValue",
                }
            }
        )

    if requests_body:
        sheet.spreadsheet.batch_update({"requests": requests_body})


def send_daily_report(status, scraped_count, new_count, agency_count, details=""):
    smtp_host = os.environ.get("AGENCY_FINDER_SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("AGENCY_FINDER_SMTP_PORT") or "587")
    smtp_username = os.environ.get("AGENCY_FINDER_SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("AGENCY_FINDER_SMTP_PASSWORD", "")
    sender = (os.environ.get("AGENCY_FINDER_SMTP_FROM") or smtp_username).strip()
    recipient = (os.environ.get("AGENCY_FINDER_SMTP_TO") or REPORT_RECIPIENT).strip()

    if not smtp_username or not smtp_password or not sender or not recipient:
        raise RuntimeError(
            "Daily report email is not configured. Set AGENCY_FINDER_SMTP_USERNAME and "
            "AGENCY_FINDER_SMTP_PASSWORD (a Gmail App Password), and optionally "
            "AGENCY_FINDER_SMTP_HOST, AGENCY_FINDER_SMTP_PORT, AGENCY_FINDER_SMTP_FROM, "
            "and AGENCY_FINDER_SMTP_TO."
        )

    report_date = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    subject = f"Agency Finder scrape {report_date}: {new_count} new record(s) [{status}]"
    body = (
        f"Agency Finder daily scrape report\n"
        f"Date (local time): {report_date}\n"
        f"Status: {status}\n"
        f"Agencies checked: {agency_count}\n"
        f"Recent job records scraped: {scraped_count}\n"
        f"New records compared with the previous sheet data: {new_count}\n"
    )
    if details:
        body += f"\nDetails:\n{details}\n"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


def run_scraper():
    client = None
    status = "FAILED"
    scraped_count = 0
    new_count = 0
    agency_count = 0
    details = ""

    try:
        print("Connecting to Google Sheets...")
        credentials = get_google_credentials()
        client = gspread.authorize(credentials)
        sheet = client.open(SHEET_NAME).sheet1
        existing_values = sheet.get_all_values()
        validate_sheet_headers(existing_values)

        cutoff_date = datetime.date.today() - datetime.timedelta(days=CUTOFF_DAYS)
        print(f"Keeping jobs approved on/after {cutoff_date}")

        agencies = discover_agencies()
        agency_count = len(agencies)
        if agency_count == 0:
            raise ScrapeError(
                "No agency profiles were discovered; the existing worksheet was not changed."
            )
        print(f"\nFound {agency_count} unique agencies. Checking profiles...\n")

        all_rows = []
        debug_used = [False]
        profile_errors = []
        for index, (profile_url, name) in enumerate(agencies.items(), start=1):
            print(f"[{index}/{agency_count}] Checking {name}...")
            try:
                all_rows.extend(
                    scrape_agency_profile(name, profile_url, cutoff_date, debug_used)
                )
            except Exception as error:
                profile_errors.append(f"{name}: {error}")
                print(f"    ERROR: {error}")
            time.sleep(0.5)

        scraped_count = len(all_rows)
        new_count = count_new_rows(existing_values, all_rows)
        if profile_errors:
            examples = "\n".join(profile_errors[:10])
            raise ScrapeError(
                f"{len(profile_errors)} of {agency_count} profiles failed validation; "
                f"the existing sheet was not changed.\n{examples}"
            )

        print(
            f"\nCollected {scraped_count} recent job rows; "
            f"{new_count} are new compared with the existing sheet."
        )

        replace_sheet_rows_atomically(sheet, all_rows)
        status = "SUCCESS"
        details = "Worksheet replacement completed atomically."
        print("Done! Check your Google Sheet.")
        log_system_event(
            client,
            "Scraper",
            "OK",
            f"Scraped {scraped_count} rows from {agency_count} agencies; "
            f"{new_count} new records.",
        )
    except Exception as error:
        details = str(error)
        print(f"Scraper failed; existing worksheet data was not intentionally cleared: {error}")
        traceback.print_exc()
        if client is not None:
            log_system_event(client, "Scraper", "ERROR", details)

    try:
        send_daily_report(status, scraped_count, new_count, agency_count, details)
        print(
            f"Daily {status.lower()} report sent to "
            f"{os.environ.get('AGENCY_FINDER_SMTP_TO', REPORT_RECIPIENT)}."
        )
    except Exception as email_error:
        print(f"Could not send daily report email: {email_error}")
        traceback.print_exc()
        return False

    return status == "SUCCESS"


def main():
    parser = argparse.ArgumentParser(description="Scrape Agency Finder job listings.")
    parser.add_argument(
        "--email-test",
        action="store_true",
        help="Send a test report email without scraping or changing the worksheet.",
    )
    args = parser.parse_args()

    if args.email_test:
        try:
            send_daily_report("TEST", 0, 0, 0, "This is a test; no scrape was run.")
        except Exception as error:
            print(f"Could not send test email: {error}")
            return 1
        print(f"Test email sent to {os.environ.get('AGENCY_FINDER_SMTP_TO', REPORT_RECIPIENT)}.")
        return 0

    return 0 if run_scraper() else 1


if __name__ == "__main__":
    raise SystemExit(main())
