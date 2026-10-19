# Downloader reliability — 11 October 2026

Downloads now has Find & download, Queue, and My downloads tabs.

## What changed

- Jobs and per-chapter outcomes persist in `data/manga_backend/queue.json`, written atomically under a file lock. One detached worker owns the queue. Closing a browser tab does not stop it.
- Pause takes effect after the current chapter. Cancel interrupts a running job and retains completed archives. Resume is explicit after restarting the Mac. Retry restarts incomplete chapters, not individual pages.
- A chapter succeeds only after its CBZ passes image verification, numeric page sequence and archive checks. A zero process exit code alone is insufficient. Invalid earlier archives are moved to the batch's `incomplete` folder rather than deleted.
- Before enqueue and again before each chapter starts, the downloader checks complete Library chapters by source/title, number and language. Overlapping queued chapters and validated completed queue archives are skipped. It also recognizes archive-only Library chapters.
- Reviewed imports reject the same series/chapter under another folder and exact duplicate image content across folders. Original files remain untouched. New imports record English language and a page fingerprint.
- Chapter availability distinguishes public links, locked rows and unverified rows. Missing is used only when a complete list is known. Partial listings never imply that an unlisted chapter is missing. Cached lists last five minutes and can be refreshed.
- When the source exposes a chapter URL, My downloads prefills it. A reviewed catalog mapping supports sources whose chapter URLs contain IDs instead of numbers.

## Boundaries

A public link is not a promise that all images are downloadable. Availability is a conservative HTML check, not a paid-content unlocker. Some sources require browser verification or hide lists behind scripts. Those remain unverified.

Duplicate matching uses normalized exact title/URL aliases and language, not fuzzy title similarity. Different translated titles can evade the pre-download check; identical page bytes are still caught on import. Recompressed or visually similar pages are not considered exact duplicates. Korean and English editions remain separate. Older matching chapters without a confirmed language are flagged in the UI rather than silently skipped; the current Happy Face library has five such chapters.

Queue processing is sequential and uses the existing browser/updater locks. If another operation owns the browser, the chapter can fail and be retried later. Alternate `-v2` archives still require manual review. Imports remain an explicit review step.

Existing pre-queue batches remain under My downloads. They are not retroactively converted into queue records.

## Validation

- 13 offline lifecycle/identity/availability checks passed, including single-worker exclusion, enqueue during processing, cancellation, pause, interrupted-worker recovery, partial retry, corrupt state preservation, duplicate import rejection, and an archive-only Library recheck.
- 7 existing backend tests and 6 search tests passed.
- Updated UI regression passed: search → choose → queue, validation, duplicate guard, fresh-session persistence, no worker launch merely on opening the page, and full Downloads/Capture navigation.
- Saved source HTML: Kayn 78 and Asura 201 chapter identities match reported totals; WeebCentral's initial nine-row list remains partial.
- No new source chapters were downloaded or added to the user's Library for these tests.
