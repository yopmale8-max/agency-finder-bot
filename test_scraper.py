import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scraper


FIXTURES_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"


class ScraperTests(unittest.TestCase):
    def test_google_credentials_use_local_file_when_environment_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            module_path = Path(directory) / "scraper.py"
            credentials_path = Path(directory) / "credentials.json"
            credentials_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(scraper, "__file__", str(module_path)),
                patch.dict("os.environ", {}, clear=True),
                patch.object(
                    scraper.Credentials,
                    "from_service_account_file",
                    return_value="file credentials",
                ) as from_file,
                patch.object(scraper.Credentials, "from_service_account_info") as from_info,
            ):
                self.assertEqual(scraper.get_google_credentials(), "file credentials")

        from_file.assert_called_once_with(
            str(credentials_path.resolve()),
            scopes=scraper.SCOPES,
        )
        from_info.assert_not_called()

    def test_google_credentials_use_environment_when_local_file_is_missing(self):
        credentials_info = {"type": "service_account"}
        with tempfile.TemporaryDirectory() as directory:
            module_path = Path(directory) / "scraper.py"
            with (
                patch.object(scraper, "__file__", str(module_path)),
                patch.dict(
                    "os.environ",
                    {"GOOGLE_CREDENTIALS": json.dumps(credentials_info)},
                    clear=True,
                ),
                patch.object(scraper.Credentials, "from_service_account_file") as from_file,
                patch.object(
                    scraper.Credentials,
                    "from_service_account_info",
                    return_value="environment credentials",
                ) as from_info,
            ):
                self.assertEqual(
                    scraper.get_google_credentials(),
                    "environment credentials",
                )

        from_file.assert_not_called()
        from_info.assert_called_once_with(credentials_info, scopes=scraper.SCOPES)

    def test_google_credentials_fall_back_to_environment_if_local_file_is_invalid(self):
        credentials_info = {"type": "service_account"}
        with tempfile.TemporaryDirectory() as directory:
            module_path = Path(directory) / "scraper.py"
            credentials_path = Path(directory) / "credentials.json"
            credentials_path.write_text("invalid", encoding="utf-8")
            with (
                patch.object(scraper, "__file__", str(module_path)),
                patch.dict(
                    "os.environ",
                    {"GOOGLE_CREDENTIALS": json.dumps(credentials_info)},
                    clear=True,
                ),
                patch.object(
                    scraper.Credentials,
                    "from_service_account_file",
                    side_effect=ValueError("invalid local credentials"),
                ) as from_file,
                patch.object(
                    scraper.Credentials,
                    "from_service_account_info",
                    return_value="environment credentials",
                ) as from_info,
            ):
                self.assertEqual(
                    scraper.get_google_credentials(),
                    "environment credentials",
                )

        from_file.assert_called_once()
        from_info.assert_called_once_with(credentials_info, scopes=scraper.SCOPES)

    @patch.dict("os.environ", {}, clear=True)
    def test_google_credentials_report_when_both_sources_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(scraper, "__file__", str(Path(directory) / "scraper.py")):
                with self.assertRaisesRegex(scraper.ScrapeError, "credentials not found"):
                    scraper.get_google_credentials()

    @patch.dict("os.environ", {"MAX_FAILURE_RATE": ""}, clear=True)
    def test_empty_max_failure_rate_uses_default(self):
        self.assertEqual(
            scraper.get_max_failure_rate(),
            scraper.DEFAULT_MAX_FAILURE_RATE,
        )

    @patch.dict("os.environ", {"MAX_FAILURE_RATE": "nan"}, clear=True)
    def test_non_finite_max_failure_rate_is_rejected(self):
        with self.assertRaisesRegex(scraper.ScrapeError, "between 0 and 1"):
            scraper.get_max_failure_rate()

    def test_parse_date_supports_source_and_common_formats(self):
        expected = datetime.date(2026, 10, 18)
        for value in (
            "Oct 18, 2026",
            "October 18, 2026",
            "2026-10-18",
            "2026-10-18T00:00:00Z",
            "10/18/2026",
        ):
            with self.subTest(value=value):
                self.assertEqual(scraper.parse_date_safe(value), expected)
        self.assertIsNone(scraper.parse_date_safe("not a date"))

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper.requests, "get", side_effect=scraper.requests.Timeout("slow"))
    def test_fetch_uses_bounded_timeout_attempts_and_backoff(self, get, sleep):
        with self.assertRaises(scraper.ScrapeError):
            scraper.fetch_with_retry("https://nr-review.test/profile")

        self.assertEqual(get.call_count, scraper.FETCH_ATTEMPTS)
        self.assertTrue(
            all(
                call.kwargs["timeout"] == scraper.HTTP_TIMEOUT_SECONDS
                for call in get.call_args_list
            )
        )
        sleep.assert_called_once_with(scraper.FETCH_RETRY_DELAY)

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper.requests, "get")
    def test_discover_agencies_follows_pages_until_short_page(self, get, sleep):
        def response(rows):
            links = "".join(
                f'<tr><td><a href="/Agency/{index}">Agency {index}</a></td></tr>'
                for index in range(rows)
            )
            result = Mock()
            result.text = (
                "<table><tr><th>Job Title</th><th>Agency</th></tr>"
                f"{links}</table>"
            )
            result.raise_for_status.return_value = None
            return result

        get.side_effect = [response(100), response(1)]
        agencies = scraper.discover_agencies()

        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(agencies), 100)
        self.assertIn("https://nr-review.com/Agency/0", agencies)
        sleep.assert_called_once_with(0.5)

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper.requests, "get")
    def test_discovery_stops_on_source_empty_state_row(self, get, sleep):
        populated_page = Mock(
            text=(
                "<table><tr><th>Job Title</th><th>Agency</th></tr>"
                + "".join(
                    f'<tr><td><a href="/Agency/{index}">Agency {index}</a></td></tr>'
                    for index in range(100)
                )
                + "</table>"
            ),
            raise_for_status=Mock(return_value=None),
        )
        empty_page = Mock(
            text=(
                "<table><tr><th>Job Title</th><th>Agency</th></tr>"
                "<tr><td colspan='2'>There is no data for now, please come back later...</td></tr>"
                "</table>"
            ),
            raise_for_status=Mock(return_value=None),
        )
        get.side_effect = [populated_page, empty_page]

        agencies = scraper.discover_agencies()

        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(agencies), 100)
        sleep.assert_called_once_with(0.5)

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper.requests, "get")
    def test_discovery_warns_and_skips_malformed_rows(self, get, sleep):
        page = Mock(
            text=(
                "<table><tr><th>Job Title</th><th>Agency</th></tr>"
                '<tr><td><a href="/Agency/1">Valid Agency</a></td></tr>'
                "<tr><td>Missing profile link</td></tr>"
                '<tr><td><a href="/Agency/2"> </a></td></tr>'
                "</table>"
            ),
            raise_for_status=Mock(return_value=None),
        )
        get.return_value = page

        with patch("builtins.print") as print_output:
            agencies = scraper.discover_agencies()

        self.assertEqual(agencies, {"https://nr-review.com/Agency/1": "Valid Agency"})
        warning_messages = [
            str(call.args[0])
            for call in print_output.call_args_list
            if call.args and "WARNING:" in str(call.args[0])
        ]
        self.assertEqual(len(warning_messages), 2)
        self.assertIn("row 3", warning_messages[0])
        self.assertIn("row 4", warning_messages[1])
        sleep.assert_not_called()

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper.requests, "get")
    def test_discovery_failure_aborts_instead_of_skipping_page(self, get, sleep):
        get.side_effect = [
            Mock(
                text=(
                    "<table><tr><th>Job Title</th><th>Agency</th></tr>"
                    + "".join(
                        f'<tr><td><a href="/Agency/{index}">Agency {index}</a></td></tr>'
                        for index in range(100)
                    )
                    + "</table>"
                ),
                raise_for_status=Mock(return_value=None),
            ),
            scraper.requests.Timeout("page timed out"),
            scraper.requests.Timeout("page timed out"),
            scraper.requests.Timeout("page timed out"),
        ]
        with self.assertRaises(scraper.ScrapeError):
            scraper.discover_agencies()
        self.assertEqual(get.call_count, 3)
        self.assertEqual(
            [call.args for call in sleep.call_args_list],
            [(0.5,), (scraper.FETCH_RETRY_DELAY,)],
        )

    @patch.object(scraper, "fetch_with_retry")
    def test_profile_scrape_returns_expected_rows(self, fetch):
        fetch.return_value = Mock(
            text=(
                "<p>Address: Manila</p><p>Contact Number: 12345</p>"
                "<p>DMW License Status: Active</p>"
                "<table><tr><th>Position</th><th>Location</th><th>Vacancy</th>"
                "<th>Principal</th><th>Approval Date</th></tr>"
                "<tr><td>Nurse</td><td>Japan</td><td>2</td><td>Employer</td>"
                "<td>2026-10-18</td></tr></table>"
            )
        )

        rows = scraper.scrape_agency_profile(
            "Sample Agency",
            "https://nr-review.com/Agency/1",
            datetime.date(2026, 1, 1),
            [False],
        )

        self.assertEqual(
            rows,
            [["Sample Agency", "Active", "Nurse", "Japan", "2", "Manila", "12345", "2026-10-18"]],
        )

    def test_job_orders_parser_reads_normal_table(self):
        soup = scraper.BeautifulSoup(
            (FIXTURES_DIR / "profile-normal-live.html").read_text(encoding="utf-8"),
            "html.parser",
        )

        rows = scraper.parse_job_orders_table(
            soup.table, "Sample Agency", datetime.date(2026, 1, 1)
        )

        self.assertEqual(
            rows,
            [["PAINTER SPRAY", "AUSTRALIA", "0", "2026-10-19"]],
        )

    def test_job_orders_parser_treats_empty_state_as_no_jobs(self):
        soup = scraper.BeautifulSoup(
            (FIXTURES_DIR / "profile-empty-state-live.html").read_text(encoding="utf-8"),
            "html.parser",
        )

        self.assertEqual(
            scraper.parse_job_orders_table(
                soup.table, "Sample Agency", datetime.date(2026, 1, 1)
            ),
            [],
        )

    def test_job_orders_parser_accepts_header_only_table(self):
        soup = scraper.BeautifulSoup(
            (FIXTURES_DIR / "profile-header-only.html").read_text(encoding="utf-8"),
            "html.parser",
        )

        self.assertEqual(
            scraper.parse_job_orders_table(
                soup.table, "Sample Agency", datetime.date(2026, 1, 1)
            ),
            [],
        )

    def test_job_orders_parser_warns_and_skips_malformed_row(self):
        soup = scraper.BeautifulSoup(
            "<table><tr><th>Position</th><th>Approval Date</th></tr>"
            "<tr><td>Malformed</td></tr>"
            "<tr><td>Nurse</td><td>Japan</td><td>2</td><td>Employer</td>"
            "<td>2026-10-18</td></tr></table>",
            "html.parser",
        )

        row_metrics = {"total": 0, "skipped": 0}
        with patch("builtins.print") as print_output:
            rows = scraper.parse_job_orders_table(
                soup.table,
                "Sample Agency",
                datetime.date(2026, 1, 1),
                row_metrics,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Nurse")
        self.assertEqual(row_metrics, {"total": 2, "skipped": 1})
        self.assertTrue(
            any("WARNING:" in str(call) and "row 2" in str(call)
                for call in print_output.call_args_list)
        )

    @patch.object(scraper, "fetch_with_retry")
    def test_profile_scrape_warns_and_skips_unrecognized_date(self, fetch):
        fetch.return_value = Mock(
            text=(
                "<table><tr><th>Position</th><th>Approval Date</th></tr>"
                "<tr><td>Nurse</td><td>unknown date format</td><td></td><td></td><td>?</td></tr>"
                "</table>"
            )
        )
        with patch("builtins.print") as print_output:
            rows = scraper.scrape_agency_profile(
                "Sample Agency",
                "https://nr-review.com/Agency/1",
                datetime.date(2026, 1, 1),
                [False],
            )

        self.assertEqual(rows, [])
        self.assertTrue(
            any("could not parse approval date" in str(call)
                for call in print_output.call_args_list)
        )

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper, "fetch_with_retry")
    def test_profile_fetch_retries_unexpected_structure(self, fetch, sleep):
        fetch.return_value = Mock(text="<html><p>Unexpected layout</p></html>")

        with self.assertRaises(scraper.StructuralScrapeError):
            scraper.scrape_agency_profile(
                "Sample Agency",
                "https://nr-review.com/Agency/1",
                datetime.date(2026, 1, 1),
                [False],
            )

        self.assertEqual(fetch.call_count, scraper.PROFILE_FETCH_ATTEMPTS)
        self.assertEqual(
            [call.args for call in sleep.call_args_list],
            [(scraper.PROFILE_RETRY_DELAY,)],
        )

    def test_new_record_count_handles_duplicates(self):
        existing = [scraper.SHEET_HEADERS, ["Agency", "License", "Job", "Country", "1", "Address", "Phone", "2026-10-01"]]
        scraped = [
            ["Agency", "License", "Job", "Country", "1", "Address", "Phone", "2026-10-01"],
            ["Agency", "License", "Job", "Country", "1", "Address", "Phone", "2026-10-01"],
            ["Other", "License", "Job", "Country", "1", "Address", "Phone", "2026-10-02"],
        ]
        self.assertEqual(scraper.count_new_rows(existing, scraped), 2)

    def test_sheet_replace_uses_single_atomic_batch(self):
        sheet = Mock()
        sheet.id = 42
        sheet.row_count = 5
        sheet.spreadsheet.batch_update.return_value = {}
        rows = [["Agency", "License", "Job", "Country", "1", "Address", "Phone", "2026-10-01"]]

        scraper.replace_sheet_rows_atomically(sheet, rows)

        sheet.spreadsheet.batch_update.assert_called_once()
        body = sheet.spreadsheet.batch_update.call_args.args[0]
        self.assertEqual(len(body["requests"]), 2)
        self.assertEqual(body["requests"][0]["updateCells"]["range"]["startRowIndex"], 1)
        self.assertEqual(body["requests"][1]["repeatCell"]["range"]["startRowIndex"], 2)

    def test_system_log_opens_spreadsheet_by_id(self):
        client = Mock()

        scraper.log_system_event(client, "Scraper", "ERROR", "test failure")

        client.open_by_key.assert_called_once_with(scraper.SPREADSHEET_ID)

    def test_system_log_tab_is_created_with_headers_when_missing(self):
        client = Mock()
        spreadsheet = client.open_by_key.return_value
        spreadsheet.worksheet.side_effect = scraper.gspread.exceptions.WorksheetNotFound
        sheet = spreadsheet.add_worksheet.return_value

        scraper.log_system_event(client, "Scraper", "ERROR", "test failure")

        spreadsheet.add_worksheet.assert_called_once_with(
            title="SystemLog",
            rows=1000,
            cols=len(scraper.SYSTEM_LOG_HEADERS),
        )
        sheet.update.assert_called_once_with(
            "A1:D1", [scraper.SYSTEM_LOG_HEADERS], value_input_option="RAW"
        )
        sheet.append_row.assert_called_once()

    def test_missing_job_orders_worksheet_is_created_and_initialized(self):
        spreadsheet = Mock()
        sheet = spreadsheet.add_worksheet.return_value
        sheet.get_all_values.return_value = []
        spreadsheet.worksheet.side_effect = scraper.gspread.exceptions.WorksheetNotFound

        result_sheet, existing_values = scraper.get_job_orders_worksheet(spreadsheet)

        self.assertIs(result_sheet, sheet)
        self.assertEqual(existing_values, [scraper.SHEET_HEADERS])
        spreadsheet.worksheet.assert_called_once_with(scraper.WORKSHEET_TITLE)
        spreadsheet.add_worksheet.assert_called_once_with(
            title=scraper.WORKSHEET_TITLE,
            rows=1000,
            cols=len(scraper.SHEET_HEADERS),
        )
        sheet.update.assert_called_once_with(
            "A1:H1", [scraper.SHEET_HEADERS], value_input_option="RAW"
        )

    def test_blank_header_row_is_initialized(self):
        spreadsheet = Mock()
        sheet = spreadsheet.worksheet.return_value
        sheet.get_all_values.return_value = [[]]

        result_sheet, existing_values = scraper.get_job_orders_worksheet(spreadsheet)

        self.assertIs(result_sheet, sheet)
        self.assertEqual(existing_values, [scraper.SHEET_HEADERS])
        sheet.update.assert_called_once_with(
            "A1:H1", [scraper.SHEET_HEADERS], value_input_option="RAW"
        )

    @patch.object(scraper, "send_daily_report")
    @patch.object(scraper, "discover_agencies", return_value={})
    @patch.object(scraper, "log_system_event")
    @patch.object(scraper, "get_google_credentials")
    @patch.object(scraper.gspread, "authorize")
    @patch.object(scraper.traceback, "print_exc")
    def test_empty_destination_sheet_is_initialized_with_headers(
        self, print_traceback, authorize, credentials, log_event, discover, send_report
    ):
        sheet = Mock()
        sheet.get_all_values.return_value = []
        spreadsheet = authorize.return_value.open_by_key.return_value
        spreadsheet.worksheet.return_value = sheet

        result = scraper.run_scraper()

        self.assertFalse(result)
        sheet.update.assert_called_once_with(
            "A1:H1", [scraper.SHEET_HEADERS], value_input_option="RAW"
        )
        sheet.spreadsheet.batch_update.assert_not_called()
        spreadsheet.worksheet.assert_called_once_with(scraper.WORKSHEET_TITLE)
        discover.assert_called_once()
        self.assertIn("No agency profiles were discovered", send_report.call_args.args[4])

    @patch.object(scraper, "send_daily_report")
    @patch.object(scraper, "discover_agencies", return_value={"https://nr-review.test/1": "Agency"})
    @patch.object(scraper, "scrape_agency_profile", side_effect=RuntimeError("profile changed"))
    @patch.object(scraper, "log_system_event")
    @patch.object(scraper, "get_google_credentials")
    @patch.object(scraper.gspread, "authorize")
    @patch.object(scraper.traceback, "print_exc")
    def test_failed_profile_does_not_replace_sheet(
        self, print_traceback, authorize, credentials, log_event, scrape_profile, discover, send_report
    ):
        events = []
        print_traceback.side_effect = lambda: events.append("traceback")
        log_event.side_effect = lambda *args: events.append("system-log")
        sheet = Mock()
        sheet.get_all_values.return_value = [scraper.SHEET_HEADERS]
        spreadsheet = authorize.return_value.open_by_key.return_value
        spreadsheet.worksheet.return_value = sheet

        with patch.object(scraper.time, "sleep"):
            result = scraper.run_scraper()

        self.assertFalse(result)
        credentials.assert_called_once()
        authorize.return_value.open_by_key.assert_called_once_with(scraper.SPREADSHEET_ID)
        spreadsheet.worksheet.assert_called_once_with(scraper.WORKSHEET_TITLE)
        print_traceback.assert_called_once()
        discover.assert_called_once()
        scrape_profile.assert_called_once()
        log_event.assert_called_once()
        self.assertEqual(events, ["traceback", "system-log"])
        sheet.spreadsheet.batch_update.assert_not_called()
        report = send_report.call_args.args
        self.assertEqual(report[:4], ("FAILED", 0, 0, 1))
        self.assertEqual(report[6], [("Agency", "profile changed")])

    @patch.dict("os.environ", {"MAX_FAILURE_RATE": "0.5"})
    @patch.object(scraper, "send_daily_report")
    @patch.object(
        scraper,
        "discover_agencies",
        return_value={
            "https://nr-review.test/1": "Good Agency",
            "https://nr-review.test/2": "Failed Agency",
        },
    )
    @patch.object(scraper, "scrape_agency_profile")
    @patch.object(scraper, "log_system_event")
    @patch.object(scraper, "get_google_credentials")
    @patch.object(scraper.gspread, "authorize")
    def test_partial_failures_update_successes_and_preserve_failed_rows(
        self, authorize, credentials, log_event, scrape_profile, discover, send_report
    ):
        existing_failed_row = [
            "Failed Agency", "Old License", "Old Job", "Old Country",
            "3", "Old Address", "Old Phone", "2026-10-01",
        ]
        sheet = authorize.return_value.open_by_key.return_value.worksheet.return_value
        sheet.id = 42
        sheet.row_count = 5
        sheet.get_all_values.return_value = [scraper.SHEET_HEADERS, existing_failed_row]

        def scrape_by_agency(name, *args):
            if name == "Good Agency":
                return [[
                    "Good Agency", "Active", "Nurse", "Japan", "2",
                    "Manila", "123", "2026-10-18",
                ]]
            raise RuntimeError("temporary profile failure")

        scrape_profile.side_effect = scrape_by_agency

        with patch.object(scraper.time, "sleep"):
            result = scraper.run_scraper()

        self.assertTrue(result)
        batch_body = sheet.spreadsheet.batch_update.call_args.args[0]
        written_rows = batch_body["requests"][0]["updateCells"]["rows"]
        self.assertEqual(len(written_rows), 2)
        self.assertEqual(
            [cell["userEnteredValue"]["stringValue"] for cell in written_rows[1]["values"]],
            existing_failed_row,
        )
        report = send_report.call_args.args
        self.assertEqual(report[0], "PARTIAL")
        self.assertEqual(report[5], 1)
        self.assertEqual(report[6], [("Failed Agency", "temporary profile failure")])
        self.assertTrue(report[7])
        self.assertIn("Failed Agency: temporary profile failure", log_event.call_args.args[3])

    @patch.object(scraper, "send_daily_report")
    @patch.object(
        scraper,
        "discover_agencies",
        return_value={"https://nr-review.test/1": "Agency"},
    )
    @patch.object(scraper, "scrape_agency_profile")
    @patch.object(scraper, "log_system_event")
    @patch.object(scraper, "get_google_credentials")
    @patch.object(scraper.gspread, "authorize")
    @patch.object(scraper.traceback, "print_exc")
    def test_run_aborts_when_skipped_job_order_row_rate_exceeds_limit(
        self,
        print_traceback,
        authorize,
        credentials,
        log_event,
        scrape_profile,
        discover,
        send_report,
    ):
        sheet = authorize.return_value.open_by_key.return_value.worksheet.return_value
        sheet.get_all_values.return_value = [scraper.SHEET_HEADERS]

        def scrape_with_skipped_rows(*args):
            args[-2]["total"] = 10
            args[-2]["skipped"] = 1
            return [[
                "Agency", "Active", "Nurse", "Japan", "2",
                "Manila", "123", "2026-10-18",
            ]]

        scrape_profile.side_effect = scrape_with_skipped_rows
        with patch.object(scraper.time, "sleep"):
            result = scraper.run_scraper()

        self.assertFalse(result)
        sheet.spreadsheet.batch_update.assert_not_called()
        self.assertIn("above the 5% limit", send_report.call_args.args[4])
        self.assertFalse(send_report.call_args.args[7])

    @patch.dict("os.environ", {"MAX_FAILURE_RATE": "0.5"})
    @patch.object(scraper, "send_daily_report")
    @patch.object(
        scraper,
        "discover_agencies",
        return_value={
            "https://nr-review.test/one": "Shared Agency",
            "https://nr-review.test/two": "Shared Agency",
        },
    )
    @patch.object(scraper, "scrape_agency_profile")
    @patch.object(scraper, "log_system_event")
    @patch.object(scraper, "get_google_credentials")
    @patch.object(scraper.gspread, "authorize")
    @patch.object(scraper.traceback, "print_exc")
    def test_duplicate_failed_agency_name_aborts_without_replacing_sheet(
        self,
        print_traceback,
        authorize,
        credentials,
        log_event,
        scrape_profile,
        discover,
        send_report,
    ):
        sheet = authorize.return_value.open_by_key.return_value.worksheet.return_value
        sheet.get_all_values.return_value = [scraper.SHEET_HEADERS]

        def scrape_by_url(name, profile_url, *args):
            if profile_url.endswith("/one"):
                return [[
                    "Shared Agency", "Active", "Nurse", "Japan", "2",
                    "Manila", "123", "2026-10-18",
                ]]
            raise RuntimeError("failed profile")

        scrape_profile.side_effect = scrape_by_url

        with patch.object(scraper.time, "sleep"):
            result = scraper.run_scraper()

        self.assertFalse(result)
        sheet.spreadsheet.batch_update.assert_not_called()
        self.assertIn("agency names are not unique", send_report.call_args.args[4])
        self.assertFalse(send_report.call_args.args[7])

    @patch.object(scraper, "send_daily_report", side_effect=OSError("SMTP unavailable"))
    @patch.object(scraper, "get_google_credentials", side_effect=RuntimeError("Sheets unavailable"))
    @patch.object(scraper.traceback, "print_exc")
    def test_initialization_and_email_failures_are_logged_without_raising(
        self, print_traceback, get_credentials, send_report
    ):
        with patch("builtins.print") as print_output:
            result = scraper.run_scraper()

        self.assertFalse(result)
        get_credentials.assert_called_once()
        send_report.assert_called_once_with(
            "FAILED", 0, 0, 0, "Sheets unavailable", 0, [], False
        )
        self.assertEqual(print_traceback.call_count, 2)
        self.assertTrue(
            any(
                "WARNING: Could not send daily report email: SMTP unavailable" in str(call)
                for call in print_output.call_args_list
            )
        )

    @patch.dict(
        "os.environ",
        {
            "AGENCY_FINDER_SMTP_USERNAME": "sender@example.com",
            "AGENCY_FINDER_SMTP_PASSWORD": "configured-secret",
            "AGENCY_FINDER_SMTP_FROM": "",
            "AGENCY_FINDER_SMTP_TO": "",
            "AGENCY_FINDER_SMTP_HOST": "",
            "AGENCY_FINDER_SMTP_PORT": "",
        },
        clear=True,
    )
    @patch.object(scraper.smtplib, "SMTP")
    def test_daily_report_email_includes_zero_new_records(self, smtp_class):
        server = smtp_class.return_value.__enter__.return_value

        with patch.object(scraper.ssl, "create_default_context", return_value=object()) as context:
            scraper.send_daily_report(
                "SUCCESS", 25, 0, 4, "Worksheet replacement completed."
            )

        smtp_class.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        server.starttls.assert_called_once_with(context=context.return_value)
        server.login.assert_called_once_with("sender@example.com", "configured-secret")
        message = server.send_message.call_args.args[0]
        self.assertIn("0 new record(s)", message["Subject"])
        body = message.get_content()
        self.assertIn("Recent job records scraped: 25", body)
        self.assertIn("New records compared with the previous sheet data: 0", body)
        self.assertIn("yopmale8@gmail.com", message["To"])

    @patch.dict(
        "os.environ",
        {
            "AGENCY_FINDER_SMTP_USERNAME": "sender@example.com",
            "AGENCY_FINDER_SMTP_PASSWORD": "configured-secret",
        },
        clear=True,
    )
    @patch.object(scraper.smtplib, "SMTP")
    def test_daily_report_lists_failures_and_sheet_state(self, smtp_class):
        server = smtp_class.return_value.__enter__.return_value

        with patch.object(scraper.ssl, "create_default_context", return_value=object()):
            scraper.send_daily_report(
                "PARTIAL",
                12,
                2,
                3,
                "Retained prior rows for failed agencies.",
                2,
                [("Broken Agency", "job-orders row 2 was malformed")],
                True,
            )

        body = server.send_message.call_args.args[0].get_content()
        self.assertIn("Total agencies: 3", body)
        self.assertIn("Successful profiles: 2", body)
        self.assertIn("Failed profiles: 1", body)
        self.assertIn("Broken Agency: job-orders row 2 was malformed", body)
        self.assertIn("Sheet updated: Yes", body)

    def test_github_summary_includes_profile_counts_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            with patch.dict(
                "os.environ", {"GITHUB_STEP_SUMMARY": str(summary_path)}
            ):
                scraper.write_github_step_summary(
                    "PARTIAL",
                    3,
                    2,
                    [("Broken Agency", "profile fetch timed out")],
                    True,
                    "Partial update completed.",
                )

            summary = summary_path.read_text(encoding="utf-8")

        self.assertIn("Total agencies: 3", summary)
        self.assertIn("Successful profiles: 2", summary)
        self.assertIn("Failed profiles: 1", summary)
        self.assertIn("Broken Agency", summary)
        self.assertIn("profile fetch timed out", summary)
        self.assertIn("Sheet updated: Yes", summary)

    @patch.dict("os.environ", {}, clear=True)
    def test_daily_report_requires_smtp_credentials(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "AGENCY_FINDER_SMTP_USERNAME and AGENCY_FINDER_SMTP_PASSWORD",
        ):
            scraper.send_daily_report("SUCCESS", 0, 0, 0)

    @patch.object(scraper, "run_scraper")
    @patch.object(scraper, "send_daily_report")
    @patch("sys.argv", ["scraper.py", "--email-test"])
    def test_email_test_command_sends_without_running_scraper(
        self, send_report, run_scraper
    ):
        self.assertEqual(scraper.main(), 0)

        send_report.assert_called_once_with(
            "TEST", 0, 0, 0, "This is a test; no scrape was run."
        )
        run_scraper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
