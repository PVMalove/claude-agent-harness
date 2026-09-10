# Store full QA output as sanitised evidence artifacts

QA completion reports will name each command, exit code, concise sanitised evidence, and the path and checksum of an immutable sanitised artifact containing its full stdout/stderr. This keeps debugging evidence reproducible without putting large or secret-bearing raw output in briefs, reports, tracker comments, or Git history.
