# Sudo & Permission Probing Discipline

## Hard Constraints

1. **Never Suppress `stderr` on Privilege or Permission Probes:**
   - **NEVER** append `2>/dev/null` (or `>/dev/null 2>&1`) to `sudo`, `chmod`, `chown`, `ls -l`, or other permission-diagnostic commands.
   - Swallowing `stderr` hides actionable failure modes (password prompts, `chmod` permission errors, missing `+x` traversal bits) and leads to false assumptions.

2. **Verify `sudo` Explicitly Before Claiming Unavailability:**
   - **NEVER** assume or report that `sudo` is unavailable without first running an explicit non-interactive probe (`sudo -n true` or `sudo -n <command>`).
   - Inspect the exact exit code and `stderr` output before concluding privilege escalation is blocked.

3. **Preserve Directory Execute (`+x`) Bits on Recursive `chmod`:**
   - **NEVER** run `chmod -R 666` (or any mode without `+x`) across directories, as stripping execute bits breaks directory traversal (including `/dev/bus/usb/*`).
   - Apply `755` (`a+rX`) to directories and restrict `666`/`644` to target device nodes or regular files.
