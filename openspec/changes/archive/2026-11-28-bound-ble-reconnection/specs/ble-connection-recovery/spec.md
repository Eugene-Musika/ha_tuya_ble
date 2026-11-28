# Spec Delta

## Purpose

Keep Tuya BLE connection failures from overwhelming Home Assistant while allowing devices to recover automatically.

## ADDED Requirements

### Requirement: Failed connection cycles are paced
The integration SHALL apply a per-device exponential delay after each failed outer connection or initialization cycle: 1 second initially, doubling to a maximum of 60 seconds. Commands and discovery SHALL NOT bypass this delay. Internal attempts performed by the BLE connector are outside this timing guarantee.

#### Scenario: Repeated notification setup failures
- **WHEN** notification setup fails repeatedly for a device
- **THEN** consecutive outer connection cycles start no sooner than 1, 2, 4 and 8 seconds after the preceding failures
- **AND** subsequent delays do not exceed 60 seconds

#### Scenario: User commands arrive during backoff
- **WHEN** several user commands arrive during a device's backoff
- **THEN** they do not bypass the delay or create additional connection cycles

### Requirement: Automatic recovery continues
The integration SHALL continue attempting to connect automatically after failures, including an initial connection failure, until the device reconnects or is stopped. A successful connection and pairing SHALL reset the delay to its initial value.

#### Scenario: Device returns to range
- **WHEN** a previously unreachable device becomes reachable
- **THEN** a subsequent scheduled attempt connects without a Home Assistant restart

#### Scenario: Failure after successful recovery
- **WHEN** a device successfully connects and later fails to reconnect
- **THEN** the first retry delay is 1 second

### Requirement: Connection work is coordinated per device
The integration SHALL allow at most one connection or initialization attempt and one scheduled recovery operation per device at a time. A device's retry delay SHALL NOT prevent other devices from connecting.

#### Scenario: Concurrent reconnect triggers
- **WHEN** a disconnect, a pending command and discovery trigger recovery for the same device
- **THEN** they share the device's recovery process rather than launching overlapping attempts

#### Scenario: Another device is available
- **WHEN** one device is waiting for its retry delay
- **THEN** another device can connect and operate without waiting for that delay

### Requirement: Failed clients are cleaned up
The integration SHALL release a client created by an unsuccessful initialization attempt before retrying. Cleanup failures SHALL be reported without enabling an immediate retry loop.

#### Scenario: Notification characteristic is missing
- **WHEN** a connected client cannot start notifications because the required characteristic is missing
- **THEN** the failed client is disconnected or its cleanup failure is logged
- **AND** the next attempt observes the retry delay

### Requirement: Stopping prevents further recovery
The integration SHALL cancel pending retry delays and connection work when a device is stopped or removed. It SHALL NOT schedule a new attempt from a stale callback after stop.

#### Scenario: Device removed during backoff
- **WHEN** a device is removed or its integration entry is unloaded while it is waiting to retry
- **THEN** no further connection attempt is made for that stopped device

#### Scenario: Device stopped during connection
- **WHEN** a device is stopped while a connection attempt is in progress
- **THEN** its connection work is cancelled or completed and cleaned up without scheduling a retry

### Requirement: Recovery errors remain actionable
The integration SHALL report connection failures and recovery without emitting a traceback on every repeated identical failure. A command that cannot be sent SHALL NOT be reported as successfully executed.

#### Scenario: Persistent identical failure
- **WHEN** a device repeatedly fails for the same reason
- **THEN** the logs identify the device and failure without a traceback for every retry

#### Scenario: Recovery
- **WHEN** a device connects after a series of failures
- **THEN** the recovery is logged and normal device operations resume

### Requirement: Relay state is restored after reconnection
The integration SHALL preserve the existing relay full-state refresh behavior after a successful reconnection.

#### Scenario: Relay reconnects after a failure
- **WHEN** a relay reconnects and its status response omits the main state datapoint
- **THEN** the existing refresh mechanism requests the full state once for that connection
- **AND** the entities update when the device reports their values
