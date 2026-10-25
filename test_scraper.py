import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scraper


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
        self.assertEqual(get.call_count, 4)
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.5,), (2,), (2,)])

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

    @patch.object(scraper, "fetch_with_retry")
    def test_profile_scrape_aborts_on_unrecognized_date(self, fetch):
        fetch.return_value = Mock(
            text=(
                "<table><tr><th>Position</th><th>Approval Date</th></tr>"
                "<tr><td>Nurse</td><td>unknown date format</td><td></td><td></td><td>?</td></tr>"
                "</table>"
            )
        )
        with self.assertRaisesRegex(scraper.ScrapeError, "could not parse approval date"):
            scraper.scrape_agency_profile(
                "Sample Agency",
                "https://nr-review.com/Agency/1",
                datetime.date(2026, 1, 1),
                [False],
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
        authorize.return_value.open_by_key.return_value.get_worksheet_by_id.return_value = sheet

        result = scraper.run_scraper()

        self.assertFalse(result)
        sheet.update.assert_called_once_with(
            "A1:H1", [scraper.SHEET_HEADERS], value_input_option="RAW"
        )
        sheet.spreadsheet.batch_update.assert_not_called()
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
        authorize.return_value.open_by_key.return_value.get_worksheet_by_id.return_value = sheet

        with patch.object(scraper.time, "sleep"):
            result = scraper.run_scraper()

        self.assertFalse(result)
        credentials.assert_called_once()
        authorize.return_value.open_by_key.assert_called_once_with(scraper.SPREADSHEET_ID)
        authorize.return_value.open_by_key.return_value.get_worksheet_by_id.assert_called_once_with(
            scraper.WORKSHEET_ID
        )
        print_traceback.assert_called_once()
        discover.assert_called_once()
        scrape_profile.assert_called_once()
        log_event.assert_called_once()
        self.assertEqual(events, ["traceback", "system-log"])
        sheet.spreadsheet.batch_update.assert_not_called()
        report = send_report.call_args.args
        self.assertEqual(report[:4], ("FAILED", 0, 0, 1))
        self.assertIn("profile changed", report[4])

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
