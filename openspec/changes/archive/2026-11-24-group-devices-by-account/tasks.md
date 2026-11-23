# Tasks

Focused unittest coverage verifies runtime data, flows and migration. Code tasks are also verified with `python3 -m py_compile custom_components/tuya_ble/*.py` and with the listed checks on the user's Home Assistant.

## 1. Runtime data and platforms

- [x] 1.1 In `devices.py`:
  - add `TuyaBLEAccountData` (`manager`, `devices: dict[str | None, TuyaBLEData]`);
  - add the `async_add_device_entities(hass, entry, async_add_entities, factory)` helper, which calls `async_add_entities(..., config_subentry_id=subentry_id)` for each device and omits the argument for the `None` key.

  Verify with `py_compile`.

- [x] 1.2 Switch `binary_sensor.py`, `button.py`, `climate.py`, `number.py`, `select.py`, `sensor.py`, `switch.py` and `text.py` to the helper. Each platform keeps its current mapping and `force_add` logic in a factory function. Verify with `py_compile` and `grep -n "hass.data\[DOMAIN\]\[entry.entry_id\]" custom_components/tuya_ble/*.py`; the only matches left should be in `__init__.py` and `devices.py`.

## 2. Cloud manager per account

- [x] 2.1 In `cloud.py`:
  - make `HASSTuyaBLEDeviceManager` take the account login;
  - add a per-device manager view that returns the subentry's stored credentials first and uses the account cloud lookup only on `force_update`;
  - remove the `.storage/tuya_ble.login` `Store` (`_save_login`, `_load_login`, `_saved_login`);
  - make `build_cache` read logins from v2 entry `data` and v1 entry `options`.

  Verify with `py_compile` and `grep -n "Store\|_saved_login" custom_components/tuya_ble/cloud.py`, which should return nothing.

- [x] 2.2 In `CHANGELOG.md` (0.2.0), replace the "last successful Tuya login is saved" line with a note that the account entry keeps the login (`add-tdq-mini-relay-switch` is archived; its `Store` stopgap is not in the `relay-switch` main spec).

## 3. Entry setup

- [x] 3.1 In `__init__.py`, set up a VERSION 2 account entry:
  - one manager;
  - each subentry set up as a device, stored in `TuyaBLEAccountData.devices`;
  - a missing BLE device registers a Bluetooth callback that schedules an entry reload on its first advertisement;
  - no `ConfigEntryNotReady` because of one device.

  Keep the VERSION 1 path for entries without a full login, wrapped in `TuyaBLEAccountData` under the `None` key. Verify with `py_compile`.

- [x] 3.2 Update the update listener and `async_unload_entry`:
  - reload when the configured subentry IDs or addresses differ from the setup snapshot, when `data` changed, or when an unavailable device is seen; serialize concurrent reloads and rediscover removed addresses;
  - stop every device on unload.

  Verify with `py_compile`.

## 4. Config flow

- [x] 4.1 In `config_flow.py`, set `VERSION = 2` and add a helper that tells whether an address is already configured (v1 entry or any v2 subentry), plus a helper that builds a device subentry from credentials. Verify with `py_compile`.
- [x] 4.2 Bluetooth discovery:
  - abort when the device is already configured;
  - find an account whose cloud has the device and show a confirm-only `discovery_confirm` step; on confirm add the subentry and abort with `device_added`;
  - fall back to the login step;
  - after login, add the device to the existing account or create a new account entry with `subentries=[...]`.

  Verify on HA: with the account present, removing the relay and clicking "Add" on its discovery card shows only the confirmation, and the relay appears under the account.

- [x] 4.3 User step:
  - login → device picker;
  - add the device to an existing account and abort `device_added`, or create the account entry with the device;
  - create the account without devices when none are in range.

  Verify on HA: "Add entry" with the same login doesn't create a second entry.

- [x] 4.4 Add `DeviceSubentryFlow` ("Add device"), register it in `async_get_supported_subentry_types`, and handle the `no_unconfigured_devices` and `device_not_registered` errors. Verify on HA: "Add device" on the account lists only unconfigured devices in range and adds the selected one.
- [x] 4.5 Rework the options flow:
  - prefill the login from `entry.data`;
  - on success update `data`, refresh every subentry's credentials and reload;
  - on failure keep the old login.

  Verify on HA: a wrong password shows the error and the devices keep working.

- [x] 4.6 Add the new strings to `translations/en.json` and `translations/ru.json`:
  - the `discovery_confirm` step and the `device_added` / `already_configured` abort reasons;
  - the `config_subentries.device` flow (title, "Add device" button, `user` step, errors, aborts);
  - the reworked options flow.

  Verify with `python3 -c "import json;[json.load(open(f'custom_components/tuya_ble/translations/{l}.json')) for l in ('en','ru')]"` and by checking on HA that no raw translation keys appear in the dialogs.

## 5. Migration

- [x] 5.1 Add component-level `async_setup` with `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`:
  - group eligible enabled v1 entries with a full login by account unique_id;
  - add or reuse a subentry for the first device before converting its v1 entry in place, or reuse an existing account entry;
  - add or reuse subentries for the remaining devices;
  - move devices, using `new_config_entry_id`/`new_config_subentry_id` when available, else `add_`/`remove_config_entry_id`;
  - move entities with `async_update_entity(config_entry_id=..., config_subentry_id=...)`;
  - remove the merged entries;
  - leave v1 entries without a full login untouched, and implement an `async_migrate_entry` compatibility handler that returns `True` for remaining v1 entries without changing their version.

  Verify with `py_compile`.

- [x] 5.2 Make migration idempotent: skip devices and entities already on a subentry, reuse existing account entries and subentries before and after conversion, and log each step at debug level. Verify with focused tests that interruptions before and after conversion preserve credentials and registry metadata and that a second start creates no duplicates. Also verify on HA that a second restart logs no migration actions.
- [x] 5.3 Preserve disabled v1 entries as separate legacy entries. Keep enabled v1 entries separate when their existing account is disabled. A deferred entry enabled after startup uses the legacy path and is retried for migration on the next restart. Verify both cases with real HA registries.
- [x] 5.4 Before changing a group, detect device-registry collisions with the target account or other sources. Leave conflicting groups and their registries untouched, skip account reconciliation, log the conflicting device IDs without credentials, and retry after the conflict is resolved. Verify with focused collision, recovery and no-partial-migration tests.

## 6. Documentation and packaging

- [x] 6.1 Set `"homeassistant": "2025.3.0"` in `hacs.json`, and bump `version` in `manifest.json` to `1.0.0`. Verify with `python3 -c "import json;json.load(open('hacs.json'));json.load(open('custom_components/tuya_ble/manifest.json'))"`.
- [x] 6.2 Update `README.md`. Cover:
  - one entry per account with devices inside it;
  - adding devices from discovery or "Add device";
  - automatic migration with a backup recommendation;
  - no downgrade;
  - HA 2025.3+.

  Verify by reading the rendered section.

## 7. End-to-end check on Home Assistant

- [x] 7.1 After an HA backup, upgrade and restart:
  - one account entry contains the lock and the relay;
  - the old per-device entries are gone;
  - entity IDs, history, device names and areas of both devices are unchanged;
  - both devices work.
- [x] 7.2 Remove the relay from the account, then add it back from discovery. The login form doesn't appear, and the lock keeps working throughout.
- 7.3 (descoped, not verified manually) Restart HA with the lock out of range. The relay works, and the lock's entities start working after the lock is seen. The physical setup was not available, so this relies on `test_missing_device_does_not_block_and_reloads_once_when_seen`.

## Workflow follow-up

- `add-tdq-mini-relay-switch` is archived; archive this change after the HA checks pass (7.3 descoped).
