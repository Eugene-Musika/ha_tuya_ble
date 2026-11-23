# Design

## Context

- `async_setup_entry` in `__init__.py` sets up one BLE device per entry:
  - `entry.data` holds only `address`.
  - `entry.options` holds the login together with the device credentials (`uuid`, `local_key`, `device_id`, `category`, `product_id`, names).
  - It raises `ConfigEntryNotReady` when the device isn't in range.
- Every platform (`binary_sensor`, `button`, `climate`, `number`, `select`, `sensor`, `switch`, `text`) reads one `TuyaBLEData` from `hass.data[DOMAIN][entry.entry_id]`.
- `HASSTuyaBLEDeviceManager` (`cloud.py`):
  - works from one dict that mixes the login and the device credentials;
  - keeps a module-level cache of logged-in APIs and cloud device lists keyed by the login;
  - `build_cache` reads logins from official `tuya` entries and from `tuya_ble` entry options.
- `add-tdq-mini-relay-switch` added a `Store` (`.storage/tuya_ble.login`) as a stopgap for losing the login. This change replaces it.
- Entity unique IDs are `{device_id}-{key}`. The device registry identifier is `(tuya_ble, <address>)` plus a Bluetooth connection.
- Home Assistant APIs this design relies on:
  - Config subentries and `async_add_entities(..., config_subentry_id=...)` (HA 2025.3+).
  - Adding or removing a subentry runs the entry's update listeners.
  - Removing a subentry clears its devices and entities.
  - Device registry moves:
    - newer cores: `new_config_entry_id` / `new_config_subentry_id`;
    - older cores: `add_config_entry_id` + `remove_config_entry_id` (deprecated in dev).

## Goals / Non-Goals

**Goals:**
- One loaded entry per account. It owns one `HASSTuyaBLEDeviceManager` and the devices of all its subentries.
- Migration keeps entity IDs, history, device names and areas without manual steps.
- The legacy single-device code path keeps working for entries without a full login.

**Non-Goals:**
- Sharing one BLE connection between devices. Each device keeps its own `TuyaBLEDevice`.
- A reauth flow. The options flow covers changing the login.
- Downgrade support.
- Supporting the official `tuya` integration's login. `build_cache` keeps reading it as before, but only for lookups.

## Decisions

1. **Entry layout (VERSION 2)**
   - The account entry has:
     - `data`: the login keys (`CONF_TUYA_LOGIN_KEYS`);
     - `options`: `{}`;
     - `unique_id`: `"{endpoint}|{access_id}|{username.lower()}"`;
     - `title`: the username.
   - Each device subentry has:
     - `subentry_type="device"`;
     - `unique_id`: the BLE address, uppercase;
     - `title`: from `get_device_readable_name`;
     - `data`: `address` plus `CONF_TUYA_DEVICE_KEYS`.
   - Why the unique_id: the Tuya cloud user id would be better, but migration can't reach the cloud. The chosen key is stable, and the account's username doesn't change.
   - Alternative rejected: a device dict in `options`, LocalTuya style. The user chose subentries, which give per-device removal and an "Add device" button in the UI.

2. **Runtime data**
   - `hass.data[DOMAIN][entry_id]` becomes `TuyaBLEAccountData` with:
     - `manager`;
     - `devices: dict[subentry_id, TuyaBLEData]`.
   - The legacy v1 path stores a `TuyaBLEAccountData` with one device under the key `None`.
   - A helper in `devices.py`, `async_add_device_entities(hass, entry, async_add_entities, factory)`, loops over the devices and calls `async_add_entities(entities, config_subentry_id=subentry_id)`. The `None` key adds the entities without a subentry. The eight platform setups shrink to a factory function each. This keeps them identical and avoids eight copies of the loop.

3. **Manager per account**
   - `HASSTuyaBLEDeviceManager` gets the login dict from `entry.data`.
   - Each `TuyaBLEDevice` gets a small per-device manager view whose `get_device_credentials` returns the subentry's stored credentials first. It falls back to the account manager's cloud lookup only on `force_update`.
   - This keeps the existing `AbstaractTuyaBLEDeviceManager` contract that `TuyaBLEDevice` already uses, and keeps devices working offline.
   - The `.storage/tuya_ble.login` `Store` is removed. `build_cache` reads logins from v2 account `data` and from v1 entry `options`.

4. **Device setup and out-of-range devices**
   - Setting up an account sets up each subentry. A device whose BLE device can't be found isn't added to `devices`. Instead, the entry registers a Bluetooth callback for that address, and the first advertisement schedules a reload of the entry.
   - The entry itself never raises `ConfigEntryNotReady` because of one device.
   - Alternative rejected: creating entities for missing devices. `TuyaBLEDevice` needs a BLE device to exist.

5. **Update listener**
   - The listener compares the configured subentry IDs and addresses with a snapshot from setup and reloads when they or the account `data` change. A missing device schedules its own reload when it is seen, so an unrelated update does not repeatedly reload an account while that device is out of range.
   - Concurrent update and discovery reloads are serialized and recheck the current snapshot before reloading. Removed addresses are explicitly rediscovered by Bluetooth.
   - Removal cleanup of the registries is done by HA.

6. **Config flow**
   - `async_get_supported_subentry_types` returns `{"device": DeviceSubentryFlow}`.
   - **Bluetooth discovery:**
     - Set the unique id to the address, then abort `already_configured` if a v1 entry or any v2 subentry has that address. A custom check is needed because subentry unique ids aren't covered by `_abort_if_unique_id_configured`.
     - For each v2 account: log in if not cached, fill the cache, and look up the device with `_find_credentials`, which uses the exact MAC, the uuid, then MAC−1.
     - First match → `discovery_confirm` step (`_set_confirm_only`). On confirm, `async_add_subentry` to that account, then abort with `device_added`.
     - No match → the existing login step. On success, either add a subentry to the existing account with the same unique_id, or create a new account entry with `subentries=[...]`.
   - **User step ("Add entry"):** login → device picker as today.
     - If the account already exists, add the picked device to it and abort `device_added`.
     - Otherwise create the account entry with that device as its initial subentry.
     - If no unconfigured devices are in range, create the account with no devices.
   - **`DeviceSubentryFlow.async_step_user`:**
     - Lists unconfigured devices in range, using the same filter as the current device step, extended to subentry addresses.
     - Fetches credentials through the account manager and creates the subentry.
     - Errors: `no_unconfigured_devices`, `device_not_registered`.
   - **Options flow:**
     - Shows the login form prefilled from `entry.data`.
     - On a successful login: update `entry.data`, refresh every subentry's credentials with `async_update_subentry`, and reload.
     - On failure: show the error and change nothing.

7. **Migration**
   - Done in the component-level `async_setup`, which runs before any entry of the domain is set up. This step needs `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`.
   - It sees all v1 entries at once. Steps:
     1. Group enabled v1 entries that have a full login in `options` by the account unique_id. Leave entries without a full login and disabled v1 entries untouched (legacy path, VERSION stays 1). A missing `app_type` counts as an empty value, matching the cloud login default. A disabled entry enabled after startup uses the legacy path and is eligible for migration on the next restart.
     2. For each group, reuse an existing account entry or pick the first v1 entry. If the existing account is disabled, leave the group untouched and log why; retry after the account is enabled and HA restarts. Before changing a group, check device-registry records for collisions with the target account or other sources in the group. If a collision would prevent preserving both records, leave the group and its registries untouched, log the conflicting device IDs, and retry after the conflict is resolved. Skip reconciliation for that account as well.
     3. Before converting a v1 entry, add or reuse a subentry containing its address and device credentials. Then convert it in place with `async_update_entry`: version 2, data = login, options = {}, unique_id = account id, title = username. This keeps the device data available if conversion is interrupted.
     4. For every entry in the group, including the converted one:
        - move its device to the account entry and subentry. Use `new_config_entry_id`/`new_config_subentry_id` when the running core's `async_update_device` accepts them (checked via `inspect.signature`), otherwise `add_config_entry_id`/`add_config_subentry_id` + `remove_config_entry_id`;
        - move its registry entities with `async_update_entity(config_entry_id=..., config_subentry_id=...)`.
     5. Remove the other entries in the group with `async_remove`. Their devices and entities have already moved, so nothing is deleted. Entity IDs, history, `name_by_user` and `area_id` are kept.
   - Converting the first entry in place keeps its entry id. Its devices and entities only need a subentry assignment.
   - Because Home Assistant invokes `async_migrate_entry` when the flow's VERSION is 2 but an entry remains at VERSION 1, provide a compatibility handler that returns `True` without changing remaining v1 entries, including entries without a full login and entries deferred for the reasons above. Grouping and moving entries remain in `async_setup`; the handler does not migrate sibling entries.
   - An account entry that already exists for the same unique_id, for example one created by a partly finished migration, is reused instead of converting a new entry. Existing subentries are reused before and after conversion, making migration idempotent.

8. **Minimum HA version**
   - Set `"homeassistant": "2025.3.0"` in `hacs.json`.

## Risks / Trade-offs

- [The device registry API differs across cores] → Detect the parameters with `inspect.signature` and test the path on the user's HA. Log the device id and the chosen path at debug level.
- [Migration fails halfway, for example on an exception between converting and removing] → Preserve each device in a subentry before conversion, reuse existing account entries and subentries, and skip devices already moved. Old entries are removed only after their device and entities moved. The next start finishes the job.
- [A disabled entry or a registry collision makes automatic merging unsafe] → Keep the affected entries unchanged and usable, log the reason, and retry on a later restart after the entry is enabled or the collision is resolved.
- [The title shows the email address] → It's the user's own login, and the entry can be renamed in the UI.
- [A device on two accounts, i.e. shared] → The first matching account wins. This is acceptable for discovery, and the user can use "Add device" on the other account.
- [Users can't downgrade] → State it in the README and the changelog. Advise taking an HA backup before upgrading.

## Migration Plan

1. Take an HA backup.
2. Update the integration and restart. Migration runs once in `async_setup`.
3. Check that the lock and the relay are under one account entry and that their entity IDs and areas are unchanged.
4. Rollback: restore the backup. There is no automatic downgrade.
