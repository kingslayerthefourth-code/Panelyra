# Tablet 1.8.0 / Mac 0.16.0

The tablet uses the Mac library's charcoal/mint colours, outlined portrait-cover
cards, alphabetical shelf, Continue reading, and book-cover hero with searchable,
numerically sorted chapter rows. Downloads and Settings share the same sidebar
and visual system. Comic pixels and reading positions are not recoloured or reset.

The requested two-book cleanup is tablet-only. Its manifest pins the exact 38
Rebirth and 20 Assassin chapter versions and both covers. Settings → Current
library enables cleanup only when all replacements exist; clicking cleanup checks
their actual hashes again. Other books and unused old versions leave Reader for
app-owned recoverable Trash. Undo restores their files and reading records.
An interrupted cleanup rolls back before orphan recovery. No remote delete API,
general filesystem access, or automatic destructive upgrade migration was added.

## App updates

Install this initial APK once, over the existing app. Future APKs must be published
with `publish_tablet_release.py --version X --code N` after release verification.
Merely dropping an arbitrary file into releases does not publish it.

The Mac verifies the published hash and uses the established pinned tablet
connection. Once per minute while unpaused, it offers a newer published release.
The tablet must be reachable with Receive mode enabled; both devices need Wi-Fi.
Old receivers are not sent unsupported APK offers. Update downloads can be turned
off in tablet Settings. Publication never resumes an external upload schedule.

The tablet validates the checksum, exact application ID, increasing version code
and the installed app's signing identity. Downloaded is distinct from installed.
Only the APK's narrow internal update directory is shared with Android's installer.
No update deletes books or resets pairing/progress. Android may require the user
to allow app updates from Panelyra and approve installation; this is not silent
installation or an attempt to bypass system protections.

Primary platform reference:
[Android installer user-action requirements](https://developer.android.com/reference/android/content/pm/PackageInstaller.SessionParams).

Physical Huawei update/install/cleanup and responsive touch layouts require an
on-device check. JVM fixtures do not establish those physical behaviours.
