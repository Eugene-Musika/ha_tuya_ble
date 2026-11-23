# account-setup Specification

## Purpose

Defines how Tuya cloud accounts and their BLE devices are configured in Home Assistant: one config entry per account, with each device inside it, so the login is entered once and devices are added, removed and migrated without losing their history.

## Requirements

### Requirement: One config entry per Tuya account
The integration SHALL create one config entry per Tuya cloud login. The entry SHALL store the login and SHALL be titled with the account username. A second entry for the same login SHALL NOT be created.

#### Scenario: Account entry title
- **WHEN** the user logs in as `user@example.com` and adds a device
- **THEN** the integration page shows one entry titled `user@example.com`

#### Scenario: Same login entered again
- **WHEN** an entry for a login already exists and the user starts "Add entry" and enters the same login
- **THEN** no second entry is created

### Requirement: Devices listed inside their account
Each BLE device SHALL be stored as a device of its account entry, with the device address and the credentials fetched from the cloud. Entities and the device registry entry SHALL belong to that device, so the integration page lists the device under its account.

#### Scenario: Two devices in one account
- **WHEN** a lock and a relay from the same Tuya account are configured
- **THEN** the integration page shows one account entry with both devices inside it

### Requirement: Adding a discovered device to an existing account
When Bluetooth discovers a Tuya BLE device that is not configured and an account entry exists, confirming the discovery SHALL add the device to an account whose cloud device list contains it. The flow SHALL NOT ask for the login again.

#### Scenario: Confirm without login
- **WHEN** an account entry exists and the user clicks "Add" on a discovered relay that is registered in that account
- **THEN** the flow shows a confirmation with the device name, and confirming adds the relay to the account without showing the login form

#### Scenario: Device not in any account
- **WHEN** no configured account's cloud device list contains the discovered device
- **THEN** the flow shows the login form, and logging in with the account that owns the device adds the device to that account

### Requirement: First device creates the account
When no account entry exists, adding a device SHALL ask for the Tuya login once and SHALL create the account entry with that device inside it.

#### Scenario: First setup from discovery
- **WHEN** no account entry exists and the user adds a discovered device and logs in
- **THEN** one account entry is created and the device appears inside it

### Requirement: Adding a device from the account
Each account entry SHALL offer an "Add device" action that lists Tuya BLE devices in Bluetooth range that are not configured yet, and adds the selected device using the account login.

#### Scenario: Add device from the account
- **WHEN** the user opens "Add device" on an account entry and selects a device in range that is registered in the account
- **THEN** the device is added to the account and its entities appear

#### Scenario: Nothing to add
- **WHEN** the user opens "Add device" and every Tuya BLE device in range is already configured
- **THEN** the flow ends with a "no unconfigured devices" message

#### Scenario: Device not registered in the account
- **WHEN** the selected device is not in the account's cloud device list
- **THEN** the flow shows the "device is not registered in Tuya cloud" error and adds nothing

### Requirement: Device configured only once
A BLE address that is already configured in any account SHALL NOT be offered for discovery or "Add device" again.

#### Scenario: Configured device rediscovered
- **WHEN** Bluetooth advertises a device whose address is already configured
- **THEN** no discovery card is shown for it

### Requirement: Removing one device
Removing a device from an account SHALL remove only that device and its entities. The account entry, its login and its other devices SHALL stay.

#### Scenario: Remove the relay
- **WHEN** the user removes the relay from an account that also has a lock
- **THEN** the relay and its entities are gone, the lock keeps working, and adding the relay again does not ask for the login

### Requirement: Login survives restarts and device removal
The account login SHALL be kept in the account entry. It SHALL be used after a restart and after any device is removed, without asking the user to enter it again.

#### Scenario: Restart
- **WHEN** Home Assistant restarts
- **THEN** every device of the account is set up without asking for the login

#### Scenario: Last device removed
- **WHEN** the user removes every device of an account and then adds a discovered device
- **THEN** the device is added to that account without the login form

### Requirement: Editing the account login
The account entry's configuration SHALL let the user change the login. After a successful login the integration SHALL refresh the cloud credentials of every device in the account and reload it. A failed login SHALL keep the old login.

#### Scenario: New password
- **WHEN** the user enters a new valid password in the account configuration
- **THEN** the account uses the new login, device credentials are refreshed, and the devices reload

#### Scenario: Wrong password
- **WHEN** the login fails
- **THEN** the form shows the login error and the saved login stays unchanged

### Requirement: Devices set up independently
An account entry SHALL load even when some of its devices are out of Bluetooth range. A missing device SHALL be set up once Bluetooth sees it, without blocking the account's other devices.

#### Scenario: Lock out of range at startup
- **WHEN** Home Assistant starts while the relay is in range and the lock is not
- **THEN** the relay's entities work, and the lock's entities start working after the lock is seen

### Requirement: Migration of per-device entries
On upgrade, enabled per-device entries that store a full login SHALL be merged into one enabled account entry per login, except when an existing account is disabled or device-registry records conflict. Entity IDs, entity history, device names and areas SHALL be kept, and the merged per-device entries SHALL be removed.

#### Scenario: Lock and relay with the same login
- **WHEN** Home Assistant starts with the upgraded integration and a lock entry and a relay entry that store the same login
- **THEN** there is one account entry with both devices, and `switch.bra` (or any other existing entity ID) keeps its ID, history, device name and area

#### Scenario: Different logins
- **WHEN** two per-device entries store different logins
- **THEN** each login becomes its own account entry with its device

#### Scenario: Interrupted migration
- **WHEN** Home Assistant restarts after migration was interrupted before or after an account entry was converted
- **THEN** migration finishes without losing device credentials or registry metadata and without creating duplicate devices

#### Scenario: Disabled per-device entry
- **WHEN** a per-device entry with a full login is disabled
- **THEN** it remains separate and disabled; if it is enabled later, it uses the legacy device setup until the next restart, when it is eligible for migration

#### Scenario: Disabled account with an enabled per-device entry
- **WHEN** an enabled per-device entry belongs to an existing disabled account
- **THEN** both entries remain unchanged and the enabled per-device entry continues to work; migration is retried on a later restart after the account is enabled

#### Scenario: Conflicting device-registry records
- **WHEN** moving records for an account would collide with another device record in that account
- **THEN** the conflicting group is left unchanged, its existing enabled entries continue to work, and an error identifies the conflict without exposing credentials
- **AND** migration is retried on a later restart after the conflict is resolved

### Requirement: Per-device entries without a login
A per-device entry without a full saved login SHALL keep working as before after the upgrade and SHALL NOT be merged.

#### Scenario: Entry without login
- **WHEN** a per-device entry has device credentials but no full login
- **THEN** after the upgrade it still loads as a separate entry and its entities keep working
