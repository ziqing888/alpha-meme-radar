# Local Operations

Use the read-only status command before changing any running service:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\local\system-status.ps1
```

Machine-readable output:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\local\system-status.ps1 -Json
```

The six components are intentionally independent. A running local report server
or an open frontend does not prove that discovery, intelligence, quotes, or live
execution is running.

This command is diagnostic only. It never starts, stops, or restarts a process.
