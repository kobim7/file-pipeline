# Design Decisions

## 1. Large File Handling
**Approach chosen:**
קבצים מועלים בהזמרמת נתונים ונכתבים במקטעים של 1 מגה בייט
csv - נכתבים שורה אחר שורה
json - נכתבים רשומה אחר רשומה
כך שכל פעם נמצא בזיכרון רק רשומה אחת.
**Why:**
a 100mb Json file can become 500mb because of the header that repeats in each record, and it can kill the Docker container when n workers are running n similar processes

**Memory usage:**
While idle, the worker uses about 55mb. while processing a 40mb file, its memory stayed at about 55mb. this shows that the workers memory doesnt grow with file size

---

## 2. Step Failure Strategy

**Approach chosen:** **Why:**
Two types of errors:
1. StepError, malformed input: fails immediately, with no retry.
Check | Message
Different number of columns in a row | line 2: expected 2 columns, got 1
CSV with no header | csv file has no header row
Column that doesn't exist | unknown columns: ['nme']
Unexpected field in a JSON record | record 2 has unexpected fields: ['z']
JSON that isn't a list of objects | record 2 is not an object
Invalid parameter value | case must be 'upper' or 'lower'
File type not supported by the step | can't validate '.pdf' files
ZIP with multiple files and no member specified | archive has 3 files, pick one...
Extraction too large | extracted file is too large

2 Temporary failure: up to 3 attempts, waiting 1, 2, and 4 seconds between them. (Disk full or unavailable)

Failed step: the job is marked failed, the steps after it are skipped, and the message states which step failed and why.
Crashed worker: a heartbeat every 10 seconds, sent from a separate thread. ש job with no heartbeat for 60 seconds is taken over by another worker.
Resuming from where it stopped: steps that have already completed dont run again
Stuck worker: a step that runs for more than 10 minutes causes the worker to exit, and Docker brings it back up (restart: unless-stopped).
Poison job: attempts are counted before the run, so after 3 crashes the job fails and doesnt keep running forever.

**Partial progress:**
The output of each step is saved and recorded in the database only after the step has been completed. 
If a worker crashes, a job that lacks a heartbeat for 60 seconds is taken over by another worker, which skips the steps that have already been completed. 
If a worker gets stuck on a step for more than 10 minutes, it terminates itself, docker then restarts it, 
and the job is immediately reassigned to another worker. attempts are tracked prior to each execution, 
so a job that repeatedly causes workers to crash will be marked as failed after three attempts.

---

## 3. File Cleanup Strategy
**Approach chosen:**
Each file has an `expires_at` timestamp, which is set only when the job concludes (whether completed, failed, or cancelled). Every 5 minutes, the workers delete expired files 
and set a `deleted_at` timestamp, causing any attempt to download them to return a `410` (file is gone) status code. 
a separate scan removes files that lack a corresponding database entry and are older than one hour—these are remnants left behind by system crashes.

**Input files:** One hour after the job concludes.

**Intermediate files:** One hour after the job concludes.

**Output files:** 24 hours after the job concludes.

As long as a job is running or waiting in the queue, none of its files are deleted; otherwise, a job that remains in the queue for an extended period would lose its input data.

---

## 4. Progress Tracking
**Approach chosen:**
Step-level tracking: status, start and end times, duration, attempt count, and errors for each step, plus a current_step_index for the job.

**Granularity:** 
The database is updated upon the completion of each step. progress_percent is set to 100 when a step finishes, and remains empty while it is running. 
I have not implemented progress percentages within individual steps.

**Trade-offs:** 
Every update involves a database write. Updating for every single row would be slow and create excessive load, whereas updating per step incurs negligible overhead.
For files up to 100MB, a step takes only seconds, which provides sufficient visibility into the job's current progress.

---

## 5. One Thing I Would Do Differently With More Time
I focused on improving file detection and handling, ensuring that no file is discarded even those containing corrupted or broken values