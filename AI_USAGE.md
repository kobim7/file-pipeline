# AI Tool Usage 

## Tools I Used
Claude Setting up the environment, explaining concepts, writing code (which I reviewed and modified), and creating tests
VS Code for editing
Docker Desktop for local execution

## What Helped Most
Explaining the mechanisms, not just writing code: Before using `FOR UPDATE SKIP LOCKED` as the queue, I asked how it keeps two workers off the same job. Then I tried it myself with two `psql` windows: one locked a row, and the other skipped it. Without `SKIP LOCKED`, the second one waited.

Checking that the tests catch bugs: After the tests passed, we broke the code on purpose in four places (no retries, re-running completed steps, `validate` not checking columns, and the result expiring too early). Each time, the matching test failed.


## What I Had to Fix

`json.load` in JSON validation. It loaded the whole file into memory. I switched to `ijson`, which streams it. With `docker stats`, the worker stayed at about 55MB while processing a 40MB file.

Stuck workers went unnoticed. The heartbeat runs in its own thread, so it keeps beating even when the main code is stuck. I added a step timeout: a stuck worker exits, and Docker restarts it. Tested with 1.5M rows and a one-second timeout.

Unknown params were ignored. The parameter was called `columns` instead of `select_columns`, so the assignment's example returned all columns with no error. Unknown params are now rejected at upload.

Splitting files into columns and rows. All our test files worked. These problems only showed up with files like real users export. The fix that tied them together: one function now reads every CSV/TXT file, so `validate`, `convert` and `transform` follow the same rules.

*CSV*
- Two columns with the same name: `DictReader` overwrote the first value with the second, silently. Now the step fails.
- A column with no name became a field called `""`. Now the step fails.
- `name, city` made a column called `" city"`. Header names are now trimmed.
- A row with too few values became `null` in `convert`, while `validate` rejected it. Now it fails in every step.
- An unclosed quote swallowed the rest of the file into one value. Now it's an error (`strict` mode).
- A `;` file was read as one column, and `validate` still passed. The delimiter is now detected from the first rows, not just the header.
- Excel's BOM turned the first column into `\ufeffname`. The encoding is now detected from the BOM.

*TXT*
- `.txt` was accepted at upload, but no step could read it. Now it's read as a delimited table, like Excel's "Text (Tab delimited)".
- Plain text passed as a one-column table. Now a `.txt` file needs at least 2 columns, unless `delimiter` is set.

*JSON*
- JSON with a BOM, or in UTF-16, failed with "lexical error". It's now decoded before parsing.
- Broken JSON got 3 retries when it reached `convert` without `validate`. Now it fails at once.

*GZ and ZIP*
- An uploaded `data.csv.gz` was stored as `.gz`, so the type of the file inside was lost. Both extensions are now kept.
- A zip of an app installer (232 files) listed every file name in the error. Now only data files are considered, and the message shows up to 10 names.

Cleanup bug caught by a test. Empty folders were deleted only if old, but deleting a file updates the folder's timestamp, so they never were.

## What AI Struggled With
Partial file edits: "Replace this function" or "add below..." led to broken lines, missing classes and leftover code. Full files to download, plus `python3 -m py_compile` before each restart, worked better.
Runtime details: It didn't always say that the worker doesn't reload on code changes, that `.env` changes need `--force-recreate`, or that without `restart: unless-stopped` a worker that exits just disappears.
Overcomplicated code: The first data model had helpers and settings it didn't need. I asked to simplify it, and switched the statuses to a `StrEnum` stored as a string.