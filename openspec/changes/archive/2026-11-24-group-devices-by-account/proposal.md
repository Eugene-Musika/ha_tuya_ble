# Proposal

## Why

Each Tuya BLE device is now its own config entry, and each entry holds its own copy of the Tuya cloud login. With a lock and a relay the integration page shows two entries for one account. A new device can't reuse the login already saved in another entry, so the login form often comes up empty. Removing the last device also loses the login. LocalTuya and other hub-style integrations show one entry per account with the devices listed inside it, and users expect Tuya BLE to look the same.

## What Changes

- **BREAKING (config entry format)**: A config entry now represents one Tuya cloud account. Its `data` holds the login. Each BLE device is a config subentry of type `device` inside it, and the subentry holds the device address and the credentials fetched from the cloud. Config entry `VERSION` becomes 2.
- Setting up an account entry starts every device subentry. A device that isn't in Bluetooth range doesn't block the other devices. It is set up when it is next seen.
- Entities and devices are attached to their subentry, so the UI shows each device under its account and lets the user remove one device.
- Bluetooth discovery of a new device with an existing account shows a short confirmation and adds the device to that account. It doesn't ask for the login again. With no account yet, the user enters the login once, and the flow creates the account entry with the device inside it.
- Each account entry has an "Add device" subentry flow that lists unconfigured Tuya BLE devices in range.
- The account options flow edits the login and refreshes the credentials of every device in the account.
- On upgrade, the integration merges enabled per-device entries into one account entry per login. It keeps entity IDs, history, device names and areas, and removes the merged entries. Entries without a full saved login remain separate. Disabled entries and groups with conflicting device-registry records also remain separate until they can be migrated safely.
- Removes the `.storage/tuya_ble.login` workaround from `add-tdq-mini-relay-switch`, because the account entry now keeps the login.
- The minimum Home Assistant version becomes 2025.3, the first release with config subentries.

## Capabilities

### New Capabilities
- `account-setup`: How Tuya cloud accounts and their BLE devices are configured: account entries, device subentries, discovery, adding and removing devices, editing the login, and migrating per-device entries.

### Modified Capabilities

None. There are no main specs yet.

## Impact

- Code:
  - `__init__.py`: setup, unload, migration and the update listener.
  - `config_flow.py`: config flow, options flow and the new subentry flow.
  - `cloud.py`: manager per account, removal of the login `Store`.
  - `devices.py`: runtime data per device.
  - All platform files: `binary_sensor.py`, `button.py`, `climate.py`, `number.py`, `select.py`, `sensor.py`, `switch.py`, `text.py`. Each sets up entities per subentry.
  - `translations/en.json`, `translations/ru.json`, `README.md`, `hacs.json`.
- Registries: device and entity registry entries move from the old per-device entries to the account entry and its subentries.
- Compatibility: downgrading after migration is not supported. Older versions of the integration can't read version 2 entries.
- No new Python dependencies.
