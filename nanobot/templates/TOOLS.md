# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## Capability Boundaries

- `exec` is for shell actions; avoid using it to bypass dedicated tools.
- Prefer built-in tools when available (for example, `cron` instead of shelling `nanobot cron`).
- Do not write to protected system paths such as `/system`.
