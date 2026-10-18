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

Enable GitHub Actions failure notifications in your GitHub notification
settings as a backup if email delivery itself is unavailable. The workflow
also marks failed scrapes and email errors as failed runs and adds a summary
with a direct link to the run logs.

Never commit service-account JSON or SMTP credentials into the repository.
