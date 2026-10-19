# Panelyra UI/UX audit — 11 October 2026

Implemented in the current `Desktop/Panelyra` app. Streamlit and the existing download/reader engines are retained. No new UI library was installed: React libraries such as shadcn/ui and Mantine would require custom components or a frontend migration; the current controls can support this cleanup directly.

## Acceptance checklist

| Area | Status | Evidence / outcome |
|---|---|---|
| Library-first navigation | Fixed, verified | Six destinations: Library, Downloads, Capture; Engage, Activity, Settings. Fresh sessions default to Library; retained user landing preferences migrate. |
| Studio / Dashboard / floating chat | Removed | No launch screens or sidebar entries; old Studio routes show a retirement notice. Historical records/assets remain. Contextual AI actions have also been removed at the user’s request. |
| Support-ticket management | Removed | Settings no longer exposes ticket-management controls. Stored records remain. |
| Library selection | Fixed, verified | One searchable cover grid; selecting a cover exposes chapter details and collapses the grid. Edition control appears only when alternatives exist. |
| Reading resume | Fixed, verified | Continue reading links directly to the saved chapter. Deleted chapters/editions are skipped when selecting a valid resume target. |
| Library edge cases | Verified | Empty catalog, Clean-only series, missing images, stale progress, edition switching and search with no matches tested with temporary fixtures. |
| Chapter tools | Fixed | File downloads, image access, tablet delivery and Trim page edges are contextual. Trimming crops margins only; Originals stay intact. |
| Capture | Fixed, verified | URL, limit, outputs and summary share one screen; layout, selectors and performance are collapsed. Browser review and explicit start/stop remain. |
| Engage | Fixed, verified | One setup screen; first-comment review available; advanced behavior/context/selectors collapsed. |
| Search feedback | Fixed, verified | Success, genuine empty results, partial failures and complete failures tested independently. Failed sources have individual retry controls; Paste link remains available. |
| Download receipt | Fixed, verified | Requested chapter identities compared with nonempty archive filenames. Missing chapters shown. Exit code alone never proves completion. Fractional-range completeness explicitly unverified. Existing reviewed-import validation retained. |
| Duplicate download / stopping | Verified | Active-process guard exists in both UI and launch function. Stop behavior uses mocked processes; no third-party download or comment submission was performed during regression tests. |
| AI / Lyra | Removed, verified | User requested removal because no local AI is installed. Navigation, settings, reader chat and run actions removed. No model client is started; saved conversations remain on disk. |
| Settings | Fixed, verified | General, Connections, Storage and Help. Tablet pairing no longer starts merely by opening Library. |
| Saved route compatibility | Verified | Reader, Dashboard, Overview, Capture Library → Library; Assistant/Lyra → Library; Run History → Activity; Help/Storage → Settings; retired Studio → Library notice. |
| Responsive layout | Verified | Desktop 1280px and narrow 390px Library checked visually. Narrow page has no horizontal overflow; navigation remains accessible. Native keyboard controls and visible focus styling retained. Reduced-motion CSS disables animation/transitions. |
| Covers | Fixed, verified | Eight missing source covers added; all 11 remaining series folders have decodable local covers. Existing covers and chapter images preserved. Source URLs and hashes recorded alongside new covers. |
| Code organization | Fixed | Touched screens extracted into `panelyra/screens/` with explicit dependencies. Unreachable legacy Reader UI and standalone tablet UI removed. Internal modules deliberately avoid Streamlit’s auto-discovered `pages/` directory. |
| Documentation / product knowledge | Fixed | README, Downloads documentation, navigation metadata and product facts updated together. |

## Verification

Fourteen focused regression scripts passed: `dashboard_readiness`, `ui_audit_regression`, `downloads_ui_regression`, `title_search_regression`, `manga_backend_regression`, `reader_progress_regression`, `manhwa_cleaner_regression`, `local_reader_regression`, `progress_ui_app_test`, `assistant_regression`, `assistant_ui_regression`, `lyra_knowledge_regression`, `reader_routing_regression`, and `capture_dispatch_regression`.

The Capture dispatch test requires the repository on `PYTHONPATH`, for example `PYTHONPATH=. .venv/bin/python tests/capture_dispatch_regression.py`. UI acceptance tests mock external launches and use temporary fixtures for edge cases. Visual checks used the running local app.

Screenshots and test logs are in `outputs/ui-audit/2026-10-11/`. Baseline copies of changed files were preserved in the working audit folder before application.

## Still limited

- External source availability is not guaranteed. The live Solo Leveling check found MangaDex connection failures, a genuine Thunder “Not Found” search page, and Kayn access denial. Unrecognized layouts are now reported as failures instead of zero matches. No challenge was bypassed and no new provider was added.
- Archive filenames provide chapter-count evidence, not semantic proof that every page is correct. Reviewed import still validates archive contents and source identity.
- Live comment submission, full remote chapter downloads and physical tablet transfer were not exercised in this audit. Existing engines and validation were retained and covered by focused regression tests.
- Keyboard/focus and responsive checks are targeted, not a formal accessibility certification.
- Korean originals were moved into the testing archive at the user’s request; English editions remain in the main library.

## Library duplicate cleanup — 11 October

Moved 69 items (1.85 GB) into `archive/library-tests/2026-10-11/`: 49 byte-identical Happy Face generic-page captures with invalid chapter IDs, the earlier `manhwa/1` test capture, and 17 extra export packages/folders. Happy Face chapters 74–78 and Choi Kang High chapters 23–33 remain in the main library. Both Korean series (`naver-845837` and `naver-851610`) were also archived at the user’s explicit request. Every moved file was SHA-256 checked after the move; manifest.json records source paths and restoration instructions. No chapter files were deleted.

Buttons now share consistent sizing, rounded corners, restrained primary/secondary states and visible keyboard focus. Mobile browser-review labels fit on one line.
