# Proposal

## Why

A relay with an unreliable Bluetooth connection produced hundreds of notification-subscription errors within seconds. The connection code retries immediately after setup failures, creating unnecessary load and log noise; the available logs do not establish that this caused Home Assistant Core to exit.

## What Changes

- Pace failed outer BLE connection cycles per device with capped exponential backoff and automatic recovery, while retaining the connector's internal retry behavior.
- Coordinate concurrent connection requests and clean up failed clients and pending recovery work.
- Limit repetitive failure logging while retaining actionable diagnostics.
- Preserve normal operations, independent devices, and relay state synchronization after reconnection.

## Capabilities

### New Capabilities

- `ble-connection-recovery`: Safe, automatic recovery from BLE connection and initialization failures across supported devices.

### Modified Capabilities

None.

## Impact

- BLE connection, notification, retry, and device shutdown lifecycle in `custom_components/tuya_ble/tuya_ble/tuya_ble.py`.
- Writable entity platforms await datapoint commands and surface failures.
- Integration-level device setup and relay state-refresh interactions where needed.
- Focused asynchronous tests and user-facing troubleshooting documentation.
- No new dependency or change to supported device mappings is planned.
