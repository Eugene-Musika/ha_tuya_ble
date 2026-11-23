# Proposal

## Why

The "Mini Smart switch" hidden relay (Tuya category `tdq`, product_id `vs4ksbpax2dwwm6a`) is a Tuya BLE device. It advertises service data `0000a201-...`, so the integration already discovers it, but no entities are created because the `tdq` category is not registered anywhere. The goal is to control the relay and its settings from Home Assistant locally over BLE, without a Tuya cloud round trip for each command.

## What Changes

- Register the new category `tdq` with product `vs4ksbpax2dwwm6a` ("Mini Smart Switch") in the device database.
- Expose the relay's datapoints as Home Assistant entities:
  - DP 1 `switch_1` (bool): a switch entity for the relay output.
  - DP 9 `countdown_1` (value, 0–86400 s, step 1): a number entity for the auto-toggle countdown.
  - DP 38 `relay_status` (enum `off`/`on`/`memory`, sent by index 0/1/2): a select entity for the power-on behavior.
  - DP 47 `switch_type` (enum `flip`/`sync`/`button`, sent by index 0/1/2): a select entity for the external wall-switch type.
- Add translation strings for the new entity keys (`strings.json`, `translations/en.json`, `translations/ru.json`).
- Make cloud credential lookup tolerant of a BLE address that differs from the cloud MAC. The relay is a Wi-Fi+BLE module: the cloud factory MAC is `c4:82:e1:de:ee:50`, while it advertises over BLE as `C4:82:E1:DE:EE:51`, so the exact-match lookup fails with "device not registered". Lookup order becomes exact MAC, then the uuid decoded from the BLE advertisement, then cloud MAC = BLE address − 1. Factory MACs are also normalized whether or not they contain separators.
- Out of scope: DP 42 `random_time`, DP 43 `cycle_time` and DP 44 `switch_inching`. These are raw binary or base64 string schedules that standard entities cannot represent. They stay unmapped. Inching may be revisited later as a dedicated feature.

## Capabilities

### New Capabilities
- `relay-switch`: Support for Tuya BLE relay/switch devices (category `tdq`): relay on/off control, countdown, power-on behavior and external switch type exposed as Home Assistant entities, with state kept in sync when the relay is toggled from the physical switch.

### Modified Capabilities
<!-- None: no specs exist yet in openspec/specs/. -->

## Impact

- Code: `custom_components/tuya_ble/devices.py`, `switch.py`, `number.py`, `select.py`.
- Translations: `custom_components/tuya_ble/strings.json`, `translations/en.json`, `translations/ru.json`.
- Credential lookup: `cloud.py`, `config_flow.py`, `devices.py`, `tuya_ble/manager.py` (new optional `uuid` argument), `tuya_ble/tuya_ble.py` (`decode_advertisement_uuid`). No changes to the BLE protocol or discovery (`manifest.json` already matches UUID `a201`).
- Reference data from the Tuya portal: the thing model (DP 1/9/38/42/43/44/47) and shadow properties confirming the dp_id-to-code mapping.
