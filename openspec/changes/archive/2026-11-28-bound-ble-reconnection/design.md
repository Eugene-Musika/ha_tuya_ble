# Design

## Context

See proposal.md for the motivation and the `ble-connection-recovery` spec for the behavioral contract. `TuyaBLEDevice._ensure_connected` currently loops up to 99 times; notification and pairing failures drop `_client` and immediately continue. `_reconnect`, disconnect callbacks, and packet resend paths can each create tasks. The relay coordinator also starts a datapoint write to refresh partial state.

The existing global connection lock serializes establishment, and the per-device locks protect connection and operation state. A retry delay must not hold the global lock or prevent shutdown.

## Goals / Non-Goals

**Goals:**
- Coordinate every path that can initiate a connection through one device lifecycle.
- Make failure pacing, cleanup, and shutdown deterministic and testable.
- Retain normal device behavior and the relay's existing state refresh.

**Non-Goals:**
- Prove the cause of the observed Core exit.
- Change BLE protocol, cloud credentials, device mappings, or add a user-facing configuration option.
- Force an unsolicited initial connection for devices that previously connect only when needed.
- Assume the missing characteristic is caused by stale services or automatically invalidate the cache without evidence.

## Decisions

1. **One recovery owner per device.** Track a single connection attempt and scheduled recovery task on the device. Route initial setup failures, disconnect callbacks, command-driven connection requests, and resend/reconnect paths through the same coordinator. Concurrent callers share the in-progress attempt or its failure; they must not launch their own retries. A command that cannot be sent reports failure rather than claiming success. Do not accumulate commands for replay merely because recovery continues. Preserve existing explicit resend behavior only where it does not bypass pacing or duplicate work.

   Alternative: independently debounce each caller. Rejected because separate entry points can still create parallel retry loops.

2. **Backoff after each failed outer connection cycle.** Start at 1 second, double after each failure, and cap at 60 seconds; reset only after connection and pairing succeed. Schedule recovery indefinitely while the device is active. The delay begins after the failure and cleanup, and is shared by all entry points. Retain `establish_connection` and limit its normal attempts where supported; some transient errors are retried internally even with `max_attempts=1`, so individual physical attempts inside that helper are not covered by the outer backoff guarantee.

   Alternative: fixed delays are simpler but make prolonged outages unnecessarily noisy. A longer operation timeout alone does not pace immediate failures such as a missing characteristic.

3. **Own client lifecycle and cancellation.** Retain the client reference until cleanup on every unsuccessful notification, device-info, or pairing path. Explicitly disconnect where appropriate; log cleanup errors and still back off. Track and cancel or await background work on stop, clear pairing state, and guard against late callbacks from old clients or stopped devices. Do not catch cancellation as an ordinary connection failure. Release connection locks before sleeping and avoid awaiting a task while holding a lock it needs.

   Alternative: just clear `_client`. Rejected because it does not close an established connection or prevent stale callbacks.

4. **Preserve diagnostics without log storms.** Log the first meaningful failure with context, summarize or rate-limit repeated equivalent failures, and log recovery. Preserve actionable failure information and do not disguise a failed user command as a success.

5. **Preserve device-specific refresh.** Leave relay DP 47 refresh and other device mappings intact. Ensure reconnect still fires the normal connected and datapoint callbacks; test the existing refresh after a failed connection and later recovery.

6. **Surface commands through Home Assistant.** Writable entity platforms await datapoint operations instead of scheduling them in the background. A failed operation raises to the service caller; an unconfirmed value does not replace a known device state. Keep device reports authoritative when they arrive during a write.

## Risks / Trade-offs

- [Slower recovery after a long outage] -> Cap delay at 60 seconds; successful pairing resets it.
- [Stop or stale disconnect callback races with recovery] -> Use an explicit stopped state and task/client ownership checks; test each race.
- [Shared retry logic changes command or device behavior] -> Test successful commands, explicit failures, unrelated devices, and relay state refresh.
- [Underlying connector retries internally] -> Retain its established behavior and test the outer-cycle timing; do not claim a per-physical-attempt limit.
- [Cleanup itself fails] -> Report it, prevent a hot loop, and test that shutdown remains possible.
