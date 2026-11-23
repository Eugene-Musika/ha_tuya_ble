# Tasks

## 1. Device registration

- [x] 1.1 Add the `tdq` category with product `vs4ksbpax2dwwm6a` named "Mini Smart Switch" to the device registry in `custom_components/tuya_ble/devices.py`. Verify with `python3 -m py_compile custom_components/tuya_ble/devices.py`.
- [x] 1.2 Add "Mini Smart Switch (category_id 'tdq', product_id 'vs4ksbpax2dwwm6a')" to the README supported devices list. Verify that the entry renders in the list.
- [x] 1.3 Make credential lookup tolerant of an offset BLE address: add `decode_advertisement_uuid`, an optional `uuid` argument to `get_device_credentials`, and a lookup order of exact MAC → uuid → MAC − 1 in `cloud.py`. Pass the advertised uuid from the config flow. Normalize factory MACs. Verify with `py_compile` and a standalone check of the lookup and decode helpers.
- [x] 1.5 Keep the saved Tuya login across reloads: in `cloud.py`, convert a saved integer `auth_type` back to `AuthType` before creating `TuyaOpenAPI`, skip config entries without a full saved login in `build_cache`, and prefill the login form from a saved `tuya_ble` entry when no cloud login has succeeded yet. Also keep the last successful login in HA storage (`.storage/tuya_ble.login`) so it survives removing the last `tuya_ble` entry, and use it in `build_cache` and for the form prefill. Verify with `py_compile`.

## 2. Entity mappings

- [x] 2.1 Add a `tdq` mapping to `switch.py`: DP 1 → key `switch`, enabled by default. Verify with `py_compile` and confirm the `switch` key already exists in the switch translations.
- [x] 2.2 Add a `tdq` mapping to `number.py`: DP 9 → key `countdown`, 0–86400 s, step 1, `UnitOfTime.SECONDS`, icon `mdi:timer`. Verify with `py_compile`.
- [x] 2.3 Add a `tdq` mapping to `select.py`:
  - DP 38 → key `power_on_behavior`, options exactly `[off, on, memory]`.
  - DP 47 → key `switch_type`, options exactly `[flip, sync, button]`.
  - Both use `DT_ENUM` and `EntityCategory.CONFIG`.
  - Verify with `py_compile` and confirm the option order matches the thing model.
- [x] 2.4 Add translations for `number.countdown`, `select.power_on_behavior` and `select.switch_type`, including `state` entries for each option, to `strings.json`, `translations/en.json` and `translations/ru.json`. Verify that each file parses with `python3 -m json.tool` and that the key sets match across the three files.
- [x] 2.5 Make the `tdq` switch (DP 1) the device's main entity: set `name=None` on its description, and make `TuyaBLEEntity` keep an explicit `name=None` instead of applying the key-based translation. Verify with `py_compile` and confirm no other mapping sets `name=None`.
- [x] 2.6 Get the full state after connecting: add `state_probe_dp=1`, `state_refresh_dp=47` and `restore_state=True` to the `tdq` product info. When DP 1 is missing from the first report after a connection, the coordinator rewrites DP 47 with its current value once. The switch, number and select entities mix in `RestoreEntity` and show the last known state until the DP arrives. Verify with `py_compile`.
- [x] 2.7 Verify in Home Assistant: after a restart, without touching anything, all four entities show the real state, and the log shows `DP 1 missing from status, rewriting DP 47`.

## 3. Hardware verification

- [x] 3.0 Add the relay (BLE `C4:82:E1:DE:EE:51`, cloud MAC `…:50`) and confirm there is no "device not registered" error and the discovery card keeps the default name (product name or BLE name plus the last three MAC bytes, for example "Mini Smart Switch DEEE51"). With debug logging, confirm the log shows which match was used (uuid or cloud MAC).
- [x] 3.1 Install the integration in Home Assistant, add the relay and confirm that 4 entities appear: switch, countdown, power-on behavior and switch type. Confirm that DPs 42/43/44 produce no entities and no errors in the log.
- [x] 3.2 Toggle the relay from Home Assistant and from the physical S1/S2 switch. Confirm that the Home Assistant state follows both directions.
- [x] 3.3 Set power-on behavior to `memory`, then power-cycle the relay. Confirm that the state is restored and the Tuya app shows the same option. This checks the enum index order.
- [x] 3.4 Change the switch type, and set the countdown to a short value such as 10 s. Confirm that the device applies the countdown, and record the actual countdown behavior in design.md Risks.
- [x] 3.5 Confirm that an existing Fingerbot or lock device still sets up with unchanged entities.
- [x] 3.6 Superseded by `group-devices-by-account` (one account entry with device subentries replaces the saved-login prefill). Original: After a Home Assistant restart, start adding a device and confirm the login form is prefilled with the saved Tuya login (also after deleting every `tuya_ble` entry), and that the log has no `1004 sign invalid` from entries without a saved login.
