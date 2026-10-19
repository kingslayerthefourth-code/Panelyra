# Panelyra folder guide

Launch the app with `Launch_Panelyra.command` at the top of this folder.

| Folder | Purpose |
| --- | --- |
| `panelyra/` | Application packages, workflows, assistant and services |
| `config/` | Saved settings; setup examples are in `config/examples/` |
| `data/history/` | Comment history, read markers, feedback, support requests and failure evidence |
| `data/updates/` | Update sources, downloaded chapter history, failures and status |
| `manhwa/` | Original captured chapters, grouped by series and chapter |
| `manhwa_clean/` | Separate cleaned editions |
| `runs/` | Automation receipts and checkpoints |
| `outputs/` | Generated deliverables, including `test-reports/` and `promo/` |
| `outputs/promo/capture-novatoon/` | Finished Capture promo, source captures, music and editable project |
| `uploads/` | Studio inputs and assets |
| `docs/` | Product and maintenance documentation |
| `tests/` | Current regression checks |
| `scripts/` | Maintenance and packaging helpers |
| `archive/backups/` | Preserved earlier app backups |
| `archive/exports/` | Preserved distribution snapshot |
| `archive/legacy-scripts/` | Older root regression script; current tests remain in `tests/` |
| `archive/organization/` | Move manifest and pre-change source backups |
| `vendor/` | Local bundled runtime |

Root Python files remain in place because they are executable entrypoints or shared engines with source-relative imports. The private `.env`, Python environment and Git directory also retain their expected locations. The reorganization moved 17 items without deleting the original library or merging old application copies. Application readers and writers now use the grouped data paths.

## Recovery

`archive/organization/2026-09-06-organization/manifest.json` records every move and edited source file. `original-files/` beside it contains the pre-change source. To reverse later, first stop Panelyra and update jobs, review subsequent changes, restore only the relevant source edits, and move each manifest destination back to its original location. Do not overwrite files created since this organization.

## Verification

The app restarted successfully and retained its library and history. Local Reader, series update wrapper and quiet update regressions passed. The demo chapter contains 22 validated images and a valid CBZ. Full promo verification is in `outputs/promo/capture-novatoon/qa/verification.json`.
