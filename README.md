# Agency Finder daily scraper

GitHub Actions runs `.github/workflows/scraper.yml` daily at 5:00 AM
Philippine time (21:00 UTC), and also supports manual runs from the Actions
tab. The scraper emails a report on successful and failed scrape attempts,
including the number of agencies checked, recent records scraped, and new
records compared with the existing sheet. A scrape or report-email failure
also makes the workflow fail so it is visible in GitHub Actions.

## Repository configuration

Add these repository **Actions secrets** under Settings → Secrets and
variables → Actions:

- `GOOGLE_CREDENTIALS`: the service-account JSON used to update the Google
  Sheet.
- `SMTP_USERNAME`: Gmail sender address.
- `SMTP_PASSWORD`: Gmail App Password (not the normal account password).
- `APPS_SCRIPT_URL` and `APP_SHARED_KEY`: retain the existing Apps Script
  watchdog configuration.

Optional Actions secrets:

- `SMTP_FROM`: defaults to `SMTP_USERNAME`.
- `SMTP_TO`: defaults to `yopmale8@gmail.com`.

Optional Actions variables:

- `SMTP_HOST`: defaults to `smtp.gmail.com`.
- `SMTP_PORT`: defaults to `587`.
- `MAX_FAILURE_RATE`: maximum fraction of agency profiles that can fail while
  still allowing a partial sheet update. It defaults to `0.02` (2%) when unset
  or empty and must be between `0` and `1`.

The scraper aborts without replacing sheet data if more than 5% of job-order
rows are malformed, any profile has an unexpected table layout, or no recent
job orders are parsed. Failed profiles below the configured limit retain their
previous rows; SystemLog records their names and reasons. A repeated agency
failure can be tracked across daily SystemLog entries.

Fetches use two attempts, a 4-second request timeout, and a 0.25-second retry
delay. Profile pages are fetched with at most eight concurrent workers. For
251 agencies and 62 listing pages, the bounded fetch phase is about 13.5
minutes, leaving approximately 6.5 minutes of the scraper step's 20-minute
timeout for Sheets and email work.

The scraper writes job-order data to the dedicated `Scraper Job Orders`
worksheet in the configured spreadsheet, creating it if it is missing. The
service account must have Editor access to that spreadsheet. If the dedicated
worksheet is empty, the scraper initializes its expected header row; existing
non-empty worksheets with unexpected headers are left unchanged and reported
as failures.

Enable GitHub Actions failure notifications in your GitHub notification
settings as a backup if email delivery itself is unavailable. The workflow
also marks failed scrapes and email errors as failed runs and adds a summary
with a direct link to the run logs.

Never commit service-account JSON or SMTP credentials into the repository.

## Run the scraper tests

From this repository's root, run:

```sh
python -m unittest discover -s . -p test_scraper.py -v
```
