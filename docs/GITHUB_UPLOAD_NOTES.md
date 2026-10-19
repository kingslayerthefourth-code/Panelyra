# GitHub Upload Notes

The repository contains application source, tests, and setup documentation. Local
libraries and private runtime state are not part of the source distribution.

Included:

- Panelyra package, Streamlit dashboard, Selenium engines, and service modules.
- Automated tests and provider-shaped local fixtures.
- Example configuration, requirements, documentation, and macOS launcher.

Excluded:

- `.env`, API keys, virtual environments, OCR model caches, and bytecode.
- Comment history, read markers, owner requests, run records, and logs.
- Uploaded images, chapter downloads, generated reports, and rollback archives.
- Local Android SDK tools, APK/build outputs, device identities, signing keys,
  conversations, cached covers, and reading progress.

Build the tablet app from `android/` using the documented toolchain. APK filenames
in local release notes describe local builds; those binaries are not included in
the source commit. The PKCS12 file under Android test resources is a deliberately
public, test-only identity, never a production key.
The device-specific `library-cleanup.json` inventory stays local; a fresh source
build has no configured cleanup and cannot execute an empty cleanup plan.

Optional third-party downloader binaries are not bundled. See `MANGA_BACKEND.md`
for acquisition, pinning, and their own license terms. A public source repository
alone does not establish a project-wide open-source license; no new license is
assigned by this update.

Before publishing, review target-specific selectors and confirm dependency licenses, package metadata, trademark clearance, domain availability, and repository visibility.
