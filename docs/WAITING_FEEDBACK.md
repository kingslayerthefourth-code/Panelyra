# Waiting feedback — Mac 0.15.0 / tablet 1.7.0

Downloads should answer: did the click work, what is happening, what is ready,
and what should I do next? Keep the library, colours and simple navigation.

- Mac: fragment-local Download interaction; cached async chapter lookup; visible
  batch chapter list and source/result counts; persistent inline queue receipt;
  explicit global/queue pause explanation; actual per-chapter stages and timing.
- Tablet: durable WorkManager page/stage snapshots; saved-chapter and current-page
  bars; receiving/checking/saving steps; elapsed time; rotating local tips;
  completion/failure/resume guidance; Reader opening state; honest delivery ETA.
- Estimates require at least two measured chapters on the current tablet run or
  saved Mac job. Skips do not train estimates. Paused/failed work has no ETA.
  Chapter sizes vary: estimates are approximate, not a promised deadline.
- Never replace status with jokes. Tips are short, local, and cost no AI tokens.
  Existing platform progress components respect system animation settings.
- All production background work remains paused during release verification.

Research informing implementation:
[NN/g: waits and interruptions](https://www.nngroup.com/articles/designing-for-waits-and-interruptions/)
supports current-step, remaining-work and completion feedback.
[Android progress indicators](https://developer.android.com/develop/ui/compose/components/progress)
distinguishes measured determinate work from unknown-duration indeterminate work.
[Streamlit fragments](https://docs.streamlit.io/develop/concepts/architecture/fragments)
keep local widget interactions from rerunning unrelated app areas.

Physical Huawei acceptance: confirm touch controls, page progress on a permitted
live download, background/reopen behaviour and installation without data loss.
Local tests and successful file transfer alone do not establish those behaviours.
