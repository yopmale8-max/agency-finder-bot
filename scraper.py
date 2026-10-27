import argparse
import datetime
import json
import math
from email.message import EmailMessage
from collections import Counter
from pathlib import Path
import os
import smtplib
import ssl
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials


CUTOFF_DAYS = 365
ROWS_PER_PAGE = 100
HTTP_TIMEOUT_SECONDS = 4
FETCH_ATTEMPTS = 2
FETCH_RETRY_DELAY = 0.25
PROFILE_FETCH_ATTEMPTS = FETCH_ATTEMPTS
PROFILE_RETRY_DELAY = FETCH_RETRY_DELAY
PROFILE_WORKERS = 8
DEFAULT_MAX_FAILURE_RATE = 0.02
MAX_SKIPPED_ROW_RATE = 0.05
SPREADSHEET_ID = "1H7QE2rZ4TiYl4PZlYplciCNSKLKPhbLY4aW7ikEgIOk"
WORKSHEET_TITLE = "Scraper Job Orders"
SYSTEM_LOG_TITLE = "SystemLog"
SYSTEM_LOG_HEADERS = ["Timestamp", "Component", "Status", "Details"]
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


class StructuralScrapeError(ScrapeError):
    """Raised when the source page no longer has the expected structure."""


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
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        try:
            sheet = spreadsheet.worksheet(SYSTEM_LOG_TITLE)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(
                title=SYSTEM_LOG_TITLE,
                rows=1000,
                cols=len(SYSTEM_LOG_HEADERS),
            )
            sheet.update(
                "A1:D1", [SYSTEM_LOG_HEADERS], value_input_option="RAW"
            )
        sheet.append_row(
            [datetime.datetime.now().isoformat(), component, status, str(details)[:500]]
        )
    except Exception as error:
        print(f"Could not write to SystemLog: {error}")


def fetch_with_retry(
    url, max_attempts=FETCH_ATTEMPTS, delay=FETCH_RETRY_DELAY
):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                url, headers=HEADERS, timeout=HTTP_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            print(f"    Attempt {attempt}/{max_attempts} failed for {url}: {error}")
            if attempt < max_attempts:
                time.sleep(delay)
    raise ScrapeError(f"Could not fetch {url} after {max_attempts} attempts: {last_error}")


def get_max_failure_rate():
    raw_value = (
        os.environ.get("MAX_FAILURE_RATE", "").strip()
        or str(DEFAULT_MAX_FAILURE_RATE)
    )
    try:
        max_failure_rate = float(raw_value)
    except ValueError as error:
        raise ScrapeError(
            f"MAX_FAILURE_RATE must be a number from 0 to 1, not {raw_value!r}."
        ) from error
    if not math.isfinite(max_failure_rate) or not 0 <= max_failure_rate <= 1:
        raise ScrapeError(
            f"MAX_FAILURE_RATE must be between 0 and 1, not {raw_value!r}."
        )
    return max_failure_rate


def parse_job_orders_table(table, agency_name, cutoff_date, row_metrics=None):
    if row_metrics is None:
        row_metrics = {"total": 0, "skipped": 0}

    results = []
    for row_number, row in enumerate(table.find_all("tr")[1:], start=2):
        cells = row.find_all("td")
        if not cells:
            if row.find_all("th"):
                row_metrics["total"] += 1
                row_metrics["skipped"] += 1
                print(
                    f"    WARNING: {agency_name}: job-orders row {row_number} "
                    "uses header cells instead of data cells; skipping malformed row."
                )
            continue

        row_text = row.get_text(" ", strip=True).casefold()
        has_colspan = any(cell.has_attr("colspan") for cell in cells)
        is_empty_state = (len(cells) == 1 or has_colspan) and any(
            empty_marker in row_text
            for empty_marker in (
                "no records",
                "no data",
                "no job orders",
                "no available options",
            )
        )
        if is_empty_state:
            continue

        row_metrics["total"] += 1
        values = [cell.get_text(strip=True) for cell in cells]
        if len(values) < 5 or has_colspan:
            row_metrics["skipped"] += 1
            print(
                f"    WARNING: {agency_name}: job-orders row {row_number} has "
                f"{len(values)} cells or an unexpected colspan; "
                "skipping malformed row."
            )
            continue

        job_title, location, vacancy = values[0], values[1], values[2]
        approval_date_str = values[4]
        approved_date = parse_date_safe(approval_date_str)
        if approved_date is None:
            row_metrics["skipped"] += 1
            print(
                f"    WARNING: {agency_name}: could not parse approval date "
                f"{approval_date_str!r} on job-orders row {row_number}; "
                "skipping malformed row."
            )
            continue
        if approved_date < cutoff_date:
            continue

        results.append(
            [job_title, location, vacancy, approved_date.isoformat()]
        )
    return results


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
            raise ScrapeError(
                f"Page {page} did not contain the expected job-listings table."
            )

        rows = table.find_all("tr")[1:]

        # This is the source's explicit end-of-list message, not a malformed listing.
        if (
            len(rows) == 1
            and not rows[0].find("a", href=lambda href: href and "/Agency/" in href)
            and "there is no data"
            in rows[0].get_text(" ", strip=True).casefold()
        ):
            print(
                f"  Page {page}: source indicates there are no more listings; "
                f"{len(agencies)} unique agencies total"
            )
            break

        page_agencies = 0
        for row_number, row in enumerate(rows, start=2):
            link = row.find("a", href=lambda href: href and "/Agency/" in href)
            if link is None:
                print(
                    f"  WARNING: Page {page}, row {row_number} is missing its agency "
                    "profile link; skipping malformed row."
                )
                continue

            name = link.get_text(strip=True)
            if not name:
                print(
                    f"  WARNING: Page {page}, row {row_number} has a blank agency "
                    "name; skipping malformed row."
                )
                continue

            profile_url = urljoin(BASE_URL + "/", link["href"])
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


def scrape_agency_profile(
    agency_name,
    profile_url,
    cutoff_date,
    debug_used,
    row_metrics=None,
    debug_lock=None,
):
    soup = None
    table = None
    last_error = None
    missing_table = False
    for attempt in range(1, PROFILE_FETCH_ATTEMPTS + 1):
        try:
            response = fetch_with_retry(profile_url, max_attempts=1)
            soup = BeautifulSoup(response.text, "html.parser")
            table = find_data_table(soup, ["Position", "Approval Date"])
            if table is None:
                missing_table = True
                raise StructuralScrapeError(
                    "profile did not contain the expected job-orders table"
                )
            break
        except ScrapeError as error:
            last_error = error
            print(
                f"    Attempt {attempt}/{PROFILE_FETCH_ATTEMPTS} failed for "
                f"{agency_name}: {error}"
            )
            if attempt < PROFILE_FETCH_ATTEMPTS:
                time.sleep(PROFILE_RETRY_DELAY * attempt)

    if table is None:
        if missing_table:
            raise StructuralScrapeError(
                f"Profile did not contain the expected job-orders table after "
                f"{PROFILE_FETCH_ATTEMPTS} attempts."
            ) from last_error
        raise ScrapeError(
            f"Profile fetch failed after {PROFILE_FETCH_ATTEMPTS} attempts: "
            f"{last_error}"
        ) from last_error

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

    should_print_debug = False
    if not address and not contact:
        if debug_lock is None:
            should_print_debug = not debug_used[0]
            debug_used[0] = True
        else:
            with debug_lock:
                should_print_debug = not debug_used[0]
                debug_used[0] = True
    if should_print_debug:
        print("    [DEBUG] Could not find address/contact. First 400 characters of page text:")
        print("    " + page_text[:400].replace("\n", " | "))

    parsed_jobs = parse_job_orders_table(
        table, agency_name, cutoff_date, row_metrics
    )
    return [
        [
            agency_name,
            license_status or "Status not listed",
            job_title,
            location,
            vacancy,
            address,
            contact,
            approved_date,
        ]
        for job_title, location, vacancy, approved_date in parsed_jobs
    ]


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


def get_job_orders_worksheet(spreadsheet):
    try:
        sheet = spreadsheet.worksheet(WORKSHEET_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        print(f"Creating dedicated worksheet '{WORKSHEET_TITLE}'...")
        sheet = spreadsheet.add_worksheet(
            title=WORKSHEET_TITLE,
            rows=1000,
            cols=len(SHEET_HEADERS),
        )

    existing_values = sheet.get_all_values()
    if not existing_values or not any(str(value).strip() for value in existing_values[0]):
        sheet.update("A1:H1", [SHEET_HEADERS], value_input_option="RAW")
        existing_values = [SHEET_HEADERS]
    validate_sheet_headers(existing_values)
    return sheet, existing_values


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


def send_daily_report(
    status,
    scraped_count,
    new_count,
    agency_count,
    details="",
    success_count=None,
    failures=None,
    sheet_updated=False,
):
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
    failures = failures or []
    if success_count is None:
        success_count = agency_count - len(failures)
    body = (
        f"Agency Finder daily scrape report\n"
        f"Date (local time): {report_date}\n"
        f"Status: {status}\n"
        f"Total agencies: {agency_count}\n"
        f"Successful profiles: {success_count}\n"
        f"Failed profiles: {len(failures)}\n"
        f"Sheet updated: {'Yes' if sheet_updated else 'No'}\n"
        f"Recent job records scraped: {scraped_count}\n"
        f"New records compared with the previous sheet data: {new_count}\n"
    )
    if failures:
        body += "\nFailed agencies:\n"
        body += "\n".join(f"- {name}: {reason}" for name, reason in failures)
        body += "\n"
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


def write_github_step_summary(
        status, agency_count, success_count, failures, sheet_updated, details
):
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
        if not summary_path:
            return

        lines = [
            "## Agency Finder scraper results",
            f"- Status: {status}",
            f"- Total agencies: {agency_count}",
            f"- Successful profiles: {success_count}",
            f"- Failed profiles: {len(failures)}",
            f"- Sheet updated: {'Yes' if sheet_updated else 'No'}",
        ]
        if failures:
            lines.extend(["", "### Failed agencies"])
            lines.extend(
                f"- **{name}**: {reason}".replace("|", "\\|")
                for name, reason in failures
            )
        if details:
            lines.extend(["", "### Details", details])

        try:
            with open(summary_path, "a", encoding="utf-8") as summary_file:
                summary_file.write("\n".join(lines) + "\n")
        except OSError as error:
            print(f"WARNING: Could not write GitHub step summary: {error}")


def run_scraper():
    client = None
    status = "FAILED"
    scraped_count = 0
    new_count = 0
    agency_count = 0
    success_count = 0
    profile_errors = []
    row_metrics = {"total": 0, "skipped": 0}
    sheet_updated = False
    details = ""

    try:
        print("Connecting to Google Sheets...")
        credentials = get_google_credentials()
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        sheet, existing_values = get_job_orders_worksheet(spreadsheet)

        cutoff_date = datetime.date.today() - datetime.timedelta(days=CUTOFF_DAYS)
        print(f"Keeping jobs approved on/after {cutoff_date}")

        agencies = discover_agencies()
        agency_count = len(agencies)
        if agency_count == 0:
            raise ScrapeError(
                "No agency profiles were discovered; the existing worksheet was not changed."
            )
        print(f"\nFound {agency_count} unique agencies. Checking profiles...\n")

        max_failure_rate = get_max_failure_rate()

        all_rows = []
        debug_used = [False]
        debug_lock = threading.Lock()
        structural_errors = []
        profile_futures = []
        with ThreadPoolExecutor(max_workers=PROFILE_WORKERS) as executor:
            for profile_url, name in agencies.items():
                profile_metrics = {"total": 0, "skipped": 0}
                future = executor.submit(
                    scrape_agency_profile,
                    name,
                    profile_url,
                    cutoff_date,
                    debug_used,
                    profile_metrics,
                    debug_lock,
                )
                profile_futures.append((name, profile_metrics, future))

            for index, (name, profile_metrics, future) in enumerate(
                profile_futures, start=1
            ):
                print(f"[{index}/{agency_count}] Checking {name}...")
                try:
                    all_rows.extend(future.result())
                    success_count += 1
                except StructuralScrapeError as error:
                    profile_errors.append((name, str(error)))
                    structural_errors.append((name, str(error)))
                    print(f"    ERROR: {name}: {error}")
                except Exception as error:
                    profile_errors.append((name, str(error)))
                    print(f"    ERROR: {name}: {error}")
                finally:
                    row_metrics["total"] += profile_metrics["total"]
                    row_metrics["skipped"] += profile_metrics["skipped"]

        scraped_count = len(all_rows)
        new_count = count_new_rows(existing_values, all_rows)
        if structural_errors:
            examples = "\n".join(
                f"{name}: {reason}" for name, reason in structural_errors
            )
            raise ScrapeError(
                "Structural profile failure; the existing sheet was not changed.\n"
                f"{examples}"
            )
        skipped_row_rate = (
            row_metrics["skipped"] / row_metrics["total"]
            if row_metrics["total"]
            else 0
        )
        if skipped_row_rate > MAX_SKIPPED_ROW_RATE:
            raise ScrapeError(
                f"{row_metrics['skipped']} of {row_metrics['total']} job-order "
                f"rows were skipped ({skipped_row_rate:.2%}), above the "
                f"{MAX_SKIPPED_ROW_RATE:.0%} limit; the existing sheet was not changed."
            )
        failure_rate = len(profile_errors) / agency_count
        if failure_rate > max_failure_rate:
            raise ScrapeError(
                f"{len(profile_errors)} of {agency_count} profiles failed "
                f"({failure_rate:.2%}), above MAX_FAILURE_RATE "
                f"({max_failure_rate:.2%}); the existing sheet was not changed."
            )
        if not all_rows:
            raise ScrapeError(
                "No recent job orders were parsed; the existing sheet was not changed."
            )

        agency_name_counts = Counter(
            name.strip().casefold() for name in agencies.values()
        )
        failed_name_keys = {
            name.strip().casefold() for name, _ in profile_errors
        }
        ambiguous_failed_names = sorted(
            name
            for name in failed_name_keys
            if agency_name_counts[name] > 1
        )
        if ambiguous_failed_names:
            raise ScrapeError(
                "Cannot safely preserve previous sheet rows for failed agencies "
                "because agency names are not unique: "
                + ", ".join(ambiguous_failed_names)
                + "; the existing sheet was not changed."
            )

        print(
            f"\nCollected {scraped_count} recent job rows; "
            f"{new_count} are new compared with the existing sheet."
        )

        failed_agencies = {
            name.strip().casefold() for name, _ in profile_errors
        }
        preserved_failed_rows = [
            normalized_row(row)
            for row in existing_values[1:]
            if row
            and str(row[0]).strip().casefold() in failed_agencies
            and any(str(value).strip() for value in row)
        ]
        replace_sheet_rows_atomically(sheet, all_rows + preserved_failed_rows)
        sheet_updated = True
        status = "PARTIAL" if profile_errors else "SUCCESS"
        row_summary = (
            f"Skipped malformed job-order rows: {row_metrics['skipped']} of "
            f"{row_metrics['total']}."
            if row_metrics["skipped"]
            else "No malformed job-order rows were skipped."
        )
        details = "Worksheet replacement completed atomically. "
        if profile_errors:
            details += (
                f"Retained {len(preserved_failed_rows)} existing rows for failed "
                "agencies. "
            )
        details += row_summary
        print("Done! Check your Google Sheet.")
        log_system_event(
            client,
            "Scraper",
            status,
            f"Scraped {scraped_count} rows from {agency_count} agencies; "
            f"{success_count} profiles succeeded; {len(profile_errors)} failed; "
            f"{new_count} new records; {row_metrics['skipped']} of "
            f"{row_metrics['total']} job-order rows skipped. "
            + (
                "Profile failures: "
                + "; ".join(f"{name}: {reason}" for name, reason in profile_errors)
                if profile_errors
                else "Profile failures: none."
            ),
        )
    except Exception as error:
        details = str(error)
        print(f"Scraper failed; existing worksheet data was not intentionally cleared: {error}")
        traceback.print_exc()
        if client is not None:
            log_details = details
            if profile_errors:
                log_details += "\nProfile failures: " + "; ".join(
                    f"{name}: {reason}" for name, reason in profile_errors
                )
            if row_metrics["total"]:
                log_details += (
                    f"\nSkipped job-order rows: {row_metrics['skipped']} of "
                    f"{row_metrics['total']}."
                )
            log_system_event(client, "Scraper", "ERROR", log_details)

    email_sent = True
    try:
        send_daily_report(
            status,
            scraped_count,
            new_count,
            agency_count,
            details,
            success_count,
            profile_errors,
            sheet_updated,
        )
        print(
            f"Daily {status.lower()} report sent to "
            f"{os.environ.get('AGENCY_FINDER_SMTP_TO', REPORT_RECIPIENT)}."
        )
    except Exception as email_error:
        print(f"WARNING: Could not send daily report email: {email_error}")
        traceback.print_exc()
        email_sent = False

    write_github_step_summary(
        status,
        agency_count,
        success_count,
        profile_errors,
        sheet_updated,
        details,
    )

    return status in ("SUCCESS", "PARTIAL") and email_sent


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
