# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog],
and this project adheres to [Semantic Versioning].

## [0.1.0] - 2023-04-22

- Initial release

## [0.1.1] - 2023-04-26

### Added

- Added new product_id for Fingerbot Plus (#1)

### Fixed

- Fixed problem in options flow.

### Changed

- Updated strings.json

## [0.1.2] - 2023-04-26

### Changed

- Changed a way to obtain device credentials from Tuya IOT cloud, possible fix to (#2)

## [0.1.4] - 2023-04-30

### Added

- Added support of CUBETOUCH 1s, thanks @damiano75
- Added new product_ids for Fingerbot.
- Added new product_ids for Fingerbot Plus.
- First attempt to support Smart Lock device.

### Fixed

- Fixed possible disconnect of BLE device.

## [0.1.5] - 2023-06-01

### Added

- Added new product_ids for Fingerbot.
- Added event "fingerbot_button_pressed" which is fired on Fingerbot Plus touch button press.
- First attempt to add support of climate entity.

## [0.1.6] - 2023-06-01

### Added

- Added new product_ids for Fingerbot and Fingerbot Plus.

### Changed

- Updated sources to conform Python 3.11

## [0.1.7] - 2023-06-01

### Added

- Added new product_ids.
- Added full support of BLE TRV provided by @forabi
- Added support of programming mode for Fingerbot Plus, thanks @redphx for information.

### Changed

- Improved connection stability.

## [0.1.8] - 2023-07-09

### Added

- Added support of 'Irrigation computer', thanks to @SanMiggel.
- Added new product_ids for Smart locks, thanks to @drewpo28.

### Changed

- Connection to the device is postponed now. Previously some out of range device might prevents HA from fully booting.
- Improved connection stability.

## [0.1.9] - 2025-05-25

### Added

- Added support for Smart Lock mqc2hevy.

## [0.1.10] - 2025-12-05

### Fixed

- Fixed HA 2025.12.0 compatibility issue

## [0.2.0] - 2026-11-23

### Added

- Added support for Mini Smart Switch vs4ksbpax2dwwm6a (category `tdq`): relay switch, countdown, power-on behavior and switch type.
- The account entry keeps the Tuya login even after its last device is removed.

### Changed

- The relay switch is the device's main entity and uses the device name.

### Fixed

- Credentials are found by cloud MAC (including MAC−1) when the advertisement UUID can't be decoded.
- Number entities (e.g. countdown) no longer fail with `AttributeError: min_value`.
- The relay reports its full state after connecting or restarting Home Assistant, and entities restore their last state until the device responds.
- Saved Tuya login options with an integer auth type are handled correctly.

## [1.0.0]

### Changed

- Tuya BLE devices are grouped under one entry per account. Existing entries
  with complete logins are migrated automatically when enabled and safe to move.
- Disabled entries and conflicting device-registry records remain unchanged
  until migration can be retried safely.
- Home Assistant 2025.3.0 or later is required.

### Upgrade

- Back up Home Assistant before upgrading. Downgrading is not supported;
  restore the backup to return to an earlier version.
