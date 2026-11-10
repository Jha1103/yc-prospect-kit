# YC Prospect Kit — Public contact discovery fix

The previous implementation searched YC company profiles only. This patch also visits each company's public website homepage and up to three same-site contact/about/team pages, while respecting robots.txt. It collects only published email text and `mailto:` links, not inferred or private contact addresses.

**This does not promise any email count.** Some sites use contact forms or client-rendered links; their emails cannot be extracted from server-returned HTML. Public business emails are not necessarily founder emails. Each record has a source URL for checking before use.

## Dashboard

1. Launch `Start YC Prospect Kit.command` (or install the ZIP update, which launches it).
2. Select a YC directory URL or choose filters.
3. Select **10 companies** for an initial test, then click **Start collection**.
4. Wait for the scan to finish. Review counts and the collection log for `found`, `none`, `no_website`, and `blocked`.
5. Export **Contacts CSV**. It includes `email`, `source`, and `source_url`. If no emails are available, saved company rows may link to the company's public contact page.

Previously processed records are automatically queued once for re-scanning because the scanner version changed. Later runs obey the usual 30-day rescan interval unless **Every run** is selected.

## Limitations

- Tests simulate websites; the current execution environment cannot reach live YC or startup sites to measure real discovery rates.
- Websites may block requests, disallow crawling, render contacts using JavaScript, or provide only web forms.
- Contact addresses are not verified for identity or deliverability.
- The collector does not bypass restrictions, scrape search engines, guess addresses, or send emails automatically.
