# Tasks

## 1. Connection lifecycle and retry pacing

- [x] 1.1 Replace immediate connection retries with a per-device recovery owner and capped 1/2/4/.../60-second backoff; verify repeated connection and notification failures with a controlled clock, including the cap and reset after pairing.
- [x] 1.2 Retain `establish_connection`, limit normal internal attempts where supported, and route command, reconnect, resend, and discovery triggers through the same outer-cycle pacing; verify outer-cycle timing, concurrent triggers, initial failure recovery, and a second device connecting during backoff.
- [x] 1.3 Clean up partially initialized clients on notification, device-info, and pairing failures; verify disconnect, cleanup-error logging, and delayed retry for each failure path.
- [x] 1.4 Cancel or await owned work on stop and guard against stale callbacks; verify stop during backoff and during connection, including no subsequent retries or leaked clients.

## 2. Diagnostics and compatibility

- [x] 2.1 Preserve actionable failure and recovery logs without a traceback for every identical retry; verify repeated failures and eventual recovery with captured logs.
- [x] 2.2 Ensure failed user commands are surfaced without unbounded queued or duplicate writes; verify a failed command and a successful command after recovery.
- [x] 2.3 Preserve relay full-state refresh and normal behavior for other device types; verify relay partial status after reconnect and a non-relay connection regression.
- [x] 2.4 Update the README troubleshooting guidance for automatic recovery and persistent BLE errors; verify it describes the implemented behavior.

## 3. Integration verification

- [x] 3.1 Run the focused recovery tests and existing relevant account/runtime tests; record results and any environment limitations.
  - `python -m unittest discover -s tests`: 103 tests, 5 skipped, no failures. Registry migration tests are conditionally skipped for the installed Home Assistant API.
  - The repository virtual environment has a broken Python link; the suite ran with a session-local Python 3.13 and additional test dependencies, using the existing virtual environment's site packages.
- [x] 3.2 When hardware is available, reproduce an unavailable or weak relay and confirm bounded attempts, independent device operation, and automatic recovery; otherwise record hardware verification as outstanding.
  - Outstanding: no physical relay was available for weak-signal, independent-device, and recovery verification.
