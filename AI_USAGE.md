# AI Tool Usage 

## Tools I Used
Claude Setting up the environment, explaining concepts, writing code (which I reviewed and modified), and creating tests
VS Code for editing
Docker Desktop for local execution

## What Helped Most
Explaining the mechanisms, not just writing code: Before using `FOR UPDATE SKIP LOCKED` as the queue, I asked how it keeps two workers off the same job. Then I tried it myself with two `psql` windows: one locked a row, and the other skipped it. Without `SKIP LOCKED`, the second one waited.

Checking that the tests catch bugs: After the tests passed, we broke the code on purpose in four places (no retries, re-running completed steps, `validate` not checking columns, and the result expiring too early). Each time, the matching test failed.


## What I Had to Fix

json.load in JSON validation.** It loaded the whole file into memory. I switched to ijson, which streams it. With docker stats, the worker stayed at about 55MB while processing a 40MB file.

Stuck workers went unnoticed.** The heartbeat runs in its own thread, so it keeps beating even when the main code is stuck. I added a step timeout: a stuck worker exits, and Docker restarts it. Tested with 1.5M rows and a one-second timeout.

Wrong results with no error.**
- The parameter was called `columns` instead of `select_columns`, and unknown params were ignored, so the assignment's example returned all columns. Unknown params are now rejected at upload.
- Excel's BOM turned the first column into `\ufeffname`, so selecting it failed.
- A CSV delimited with `;` was read as one column, and `validate` still passed.

All our test files worked. These only showed up with files like real users export.

Error message too long.** A zip of an app installer (232 files) listed every file name in the error. Now only CSV/JSON files are considered, and the message shows up to 10 names.

Cleanup bug caught by a test.** Empty folders were deleted only if old, but deleting a file updates the folder's timestamp, so they never were.

## What AI Struggled With
Partial file edits: "Replace this function" or "add below..." led to broken lines, missing classes and leftover code. Full files to download, plus `python3 -m py_compile` before each restart, worked better.
Runtime details: It didn't always say that the worker doesn't reload on code changes, that `.env` changes need `--force-recreate`, or that without `restart: unless-stopped` a worker that exits just disappears.
Overcomplicated code: The first data model had helpers and settings it didn't need. I asked to simplify it, and switched the statuses to a `StrEnum` stored as a string.