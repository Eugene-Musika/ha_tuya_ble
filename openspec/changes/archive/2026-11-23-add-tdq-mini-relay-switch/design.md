# Design

## Context

See proposal.md (Why). The integration builds entities from static, per-platform tables keyed by category and product_id. One table lives in each of `devices.py`, `switch.py`, `number.py` and `select.py`. The BLE protocol, discovery (service data UUID `a201`) and cloud credential lookup already handle the relay. Only the tables and translations are missing. Requirements are in `specs/relay-switch/spec.md`.

Observed device behavior from the Tuya portal logs:
- After every change, including a physical toggle, the device reports the full set of DPs 1, 9, 38 and 47.
- The cloud `/iot-03` API shows `relay_status` as `"0"/"1"/"2"`, while the thing model lists `off/on/memory`. Over BLE, enums travel as integer indexes.

## Goals / Non-Goals

**Goals:**
- Reuse the existing mapping pattern without new entity classes or base-class changes.

**Non-Goals:**
- Category-wide defaults for all `tdq` products. Only the known product_id is mapped, because other `tdq` products may use different DP ids or more channels.
- Decoding the binary or base64 schedule DPs (42, 43, 44).

## Decisions

1. **Add a `tdq` block to each of the four tables, keyed by product `vs4ksbpax2dwwm6a`.**
   - This follows the `ggq` irrigation precedent: a switch on DP 1 plus a countdown number.
   - Alternative: a generic "relay" mapping applied to the whole category. Rejected, because it is unverified for other products.

2. **Entity keys, which also serve as translation keys:**

   | Platform | DP | Key | Notes |
   |----------|----|-----|-------|
   | switch | 1 | `switch` | Primary entity; reuses the existing `switch` translation. |
   | number | 9 | `countdown` | Seconds, 0–86400, step 1, icon `mdi:timer`. |
   | select | 38 | `power_on_behavior` | Options `off`, `on`, `memory`; entity category CONFIG. |
   | select | 47 | `switch_type` | Options `flip`, `sync`, `button`; entity category CONFIG. |

   - The name `power_on_behavior` matches Home Assistant's naming used in other integrations, such as Z2M and ZHA, and is clearer than `relay_status`.
   - Alternative: reuse `countdown_duration`. Rejected, because that key implies minutes in the existing irrigation translations.

3. **Enum option order must equal the thing-model `range` order.**
   - The select platform maps option position to the DT_ENUM index in both directions.
   - The lists are therefore copied verbatim: `[off, on, memory]` and `[flip, sync, button]`.

4. **Translations:**
   - Add the new number and select keys, with `state` entries for the select options, to `strings.json`, `translations/en.json` and `translations/ru.json`.
   - Update the README's supported devices list.

5. **Credential lookup: exact MAC → advertised uuid → MAC − 1.**
   - Found during hardware testing: the cloud factory MAC (`…:50`) is one less than the BLE address (`…:51`), which is typical for Wi-Fi+BLE modules.
   - `decode_advertisement_uuid` (in `tuya_ble/tuya_ble.py`) decrypts the uuid from manufacturer data, using the product id from service data, the same way `TuyaBLEDevice` already does. The config flow passes it to `get_device_credentials(..., uuid=...)`.
   - The uuid match is preferred because it is unambiguous. The MAC − 1 fallback covers advertisements where the uuid cannot be decoded.
   - Matched credentials are copied with `address` set to the BLE address, so the config entry stores the address the device is actually reachable at.
   - A successful login creates a cache entry before any devices are loaded. The first credential lookup must fetch the device list and factory info if that cache entry is empty, without requiring the user to confirm the device twice.

## Risks / Trade-offs

- **MAC − 1 fallback could match the wrong device** if another cloud device's MAC is exactly one less. This is unlikely, and the uuid match runs first.
- **Wi-Fi+BLE module may stop answering on BLE while connected to Wi-Fi.** The cloud shows it offline now, and it advertises `a201`. If BLE control turns out to be flaky once Wi-Fi is up, document it.

- **Wrong enum index order: the select writes the wrong mode.**
  - Mitigation: verify on the real device that selecting `memory` and then power-cycling keeps the relay state.
  - Mitigation: verify that the Tuya app shows the same value as Home Assistant.
- **Countdown write semantics (verified on hardware).** With the relay on, writing 10 to DP 9 turned it off after 10 s. The device then reported DP 1 = off and DP 9 = 0. It reported no intermediate values, so the entity shows the set value until the timer fires.
  - The number entity needed `NumberEntity` restored as a base class of `TuyaBLENumber`. Without it, every number entity showed `unknown` and `number/set_value` failed with `AttributeError: min_value`.
- **Partial status response (verified on hardware).** `FUN_SENDER_DEVICE_STATUS` returns only DP 47 and DP 44. Any DP write makes the device report DP 47, 38, 9 and 1.
  - Mitigation: after each connection, if DP 1 was not reported, rewrite DP 47 (switch type) with its current value. This doesn't change the device's configuration.
  - Mitigation: entities restore their last known state until the device reports. This state may be stale if the relay was toggled while Home Assistant was down; the DP 47 rewrite corrects it within about a second.
- **Same product_id, different firmware DPs: some entities stay unavailable.** This is accepted, since the mapping is product-scoped.
- **No automated test suite exists in the repo.** Verification is manual on hardware, plus a syntax and JSON validity check.
