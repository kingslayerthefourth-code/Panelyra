# Downloads

Open **Downloads → Search titles**, enter a title, choose a result, then select explicit chapters (for example `1,3,5-10`). Search covers 27 sources; MangaDex is optional while its API is unreachable. Results show chapter totals, rank sources with more chapters higher after exact title matches, and can be filtered by source and paginated. “At least” marks a partial list; an unavailable count is not zero. Site-listed totals may include locked chapters. Search itself does not download anything or open Chrome.

**Paste link** accepts a series landing-page URL and includes a filterable directory of 85 sites listed by the installed upstream downloader. This directory describes upstream compatibility, not a guarantee that every site is currently reachable. See [the source audit](DOWNLOADER_SEARCH_AUDIT.md) for live checks and the requested-source evaluation.

Download may open Chrome. Files go to staging for review. The completion receipt compares requested chapter numbers against nonempty archives; process exit zero alone is not proof of success. Use **My downloads** to inspect files and logs, confirm the original chapter URL and library folder, and import. Existing chapter folders are never overwritten.

The optional executable is [elboletaire/manga-downloader v1.9.0](https://github.com/elboletaire/manga-downloader/tree/v1.9.0). Four image requests can run concurrently; chapters remain sequential. Capture and scheduled quiet updates keep their existing engines.

## Checks and limits

- A live Asura search-to-download test produced Solo Leveling chapter 1 with 22 validated images. The test remains in outputs, outside the library.
- 26 search sources returned titles in live checks on 11 October 2026. MangaDex was unreachable. Search success does not prove every chapter can be downloaded.
- Access challenges, rate limits and unrecognized search layouts remain visible, with per-source retry. No challenge bypass or paid-chapter access was added.
- Imports require confirmed source identity; validating images cannot prove every story panel is present. Fractional chapter-range completeness remains unverified.
- The download worker holds updater/browser ownership locks and runs for at most one hour. Stop terminates its process group. Failed runs remain in staging for inspection.

Tests: `title_search_regression.py`, `title_search_sources_regression.py`, `downloads_ui_regression.py`, `manga_backend_regression.py` under `tests/`.

## Third-party notice

The executable is AGPL-3.0. The installer verifies the pinned release SHA-256 and retains its license/readme under `vendor/manga-downloader/1.9.0`. No upstream implementation source was copied into Panelyra.
