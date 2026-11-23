# relay-switch Specification

## Purpose

Lets Home Assistant users control a Tuya BLE relay switch (category `tdq`) locally over BLE: turn the relay on or off, set a countdown and configure its power-on behavior and external switch type.

## Requirements

### Requirement: Device recognition
The integration SHALL recognize a Tuya BLE device with category `tdq` and product_id `vs4ksbpax2dwwm6a` as a supported "Mini Smart Switch" and create its entities after setup.

#### Scenario: Supported relay is added
- **WHEN** a user adds a discovered BLE device whose cloud credentials report category `tdq` and product_id `vs4ksbpax2dwwm6a`
- **THEN** the device is registered with the model name "Mini Smart Switch"
- **AND** the relay switch, countdown, power-on behavior and switch type entities are created

#### Scenario: Other devices unaffected
- **WHEN** a device of any previously supported category is set up
- **THEN** its entities are the same as before this change

### Requirement: Credential lookup with mismatched BLE address
The integration SHALL find a device's cloud credentials when its BLE address differs from the cloud factory MAC. Lookup MUST try, in order: the exact MAC, the uuid decoded from the BLE advertisement, then the cloud MAC equal to the BLE address minus one. Credentials found by a fallback MUST be stored with the BLE address.

#### Scenario: Wi-Fi+BLE relay with offset MAC
- **GIVEN** the cloud factory MAC is `C4:82:E1:DE:EE:50`
- **WHEN** the user adds the device advertising as `C4:82:E1:DE:EE:51`
- **THEN** the credentials are found and the device is added without the "device not registered" error

#### Scenario: Match by advertised uuid
- **WHEN** no cloud MAC equals the BLE address and the advertisement decodes to a uuid present in the cloud device list
- **THEN** that device's credentials are used

#### Scenario: Exact match unchanged
- **WHEN** the BLE address equals a cloud MAC
- **THEN** that device's credentials are used, as before this change

### Requirement: Relay on/off control
The integration SHALL expose datapoint 1 (`switch_1`) as a switch entity that turns the relay on and off.

#### Scenario: Turn relay on
- **WHEN** the user turns the switch entity on
- **THEN** the integration writes boolean `true` to datapoint 1
- **AND** the entity state becomes on after the device confirms

#### Scenario: Turn relay off
- **WHEN** the user turns the switch entity off
- **THEN** the integration writes boolean `false` to datapoint 1

#### Scenario: Switch is the device's main entity
- **WHEN** the relay's entities are created
- **THEN** the switch entity has no entity name of its own, so Home Assistant treats it as the device's main entity and shows the device name (for example "Бра")

### Requirement: State sync from physical switch
The integration SHALL update entity states from datapoints the device reports on its own, including when the relay is toggled with the wired external switch.

#### Scenario: Physical toggle
- **GIVEN** the device is connected and the relay is off
- **WHEN** the user flips the wired wall switch and the device reports datapoint 1 = `true`
- **THEN** the switch entity state becomes on without any action in Home Assistant

### Requirement: Full state after connecting
The integration SHALL show the relay's current state after Home Assistant starts or the device reconnects, without user action, even though the device's status response omits most datapoints.

#### Scenario: Status response is partial
- **WHEN** the device answers the status request with only datapoints 47 and 44
- **THEN** the integration writes datapoint 47 with its current value once per connection
- **AND** the device reports datapoints 1, 9, 38 and 47, and the entities show those values

#### Scenario: Last known state before the device reports
- **WHEN** Home Assistant restarts and the device has not yet reported a datapoint
- **THEN** the switch, countdown and select entities show their last known state instead of off, 0 or unknown

### Requirement: Countdown
The integration SHALL expose datapoint 9 (`countdown_1`) as a number entity in seconds with range 0–86400 and step 1. A value of 0 means no countdown is active.

#### Scenario: Set countdown
- **WHEN** the user sets the countdown entity to 60
- **THEN** the integration writes integer 60 to datapoint 9

#### Scenario: Countdown finished
- **WHEN** the device reports datapoint 9 = 0
- **THEN** the countdown entity shows 0

### Requirement: Power-on behavior
The integration SHALL expose datapoint 38 (`relay_status`) as a configuration select entity with options `off`, `on`, `memory`, which map to enum indexes 0, 1 and 2.

#### Scenario: Select memory mode
- **WHEN** the user selects `memory`
- **THEN** the integration writes enum value 2 to datapoint 38

#### Scenario: Reported value shown
- **WHEN** the device reports datapoint 38 = 0
- **THEN** the select entity shows `off`

### Requirement: External switch type
The integration SHALL expose datapoint 47 (`switch_type`) as a configuration select entity with options `flip`, `sync`, `button`, which map to enum indexes 0, 1 and 2.

#### Scenario: Select push button
- **WHEN** the user selects `button`
- **THEN** the integration writes enum value 2 to datapoint 47

#### Scenario: Reported value shown
- **WHEN** the device reports datapoint 47 = 0
- **THEN** the select entity shows `flip`

### Requirement: Localized entity names
Every entity and select option added for this device SHALL have a display name in English and Russian.

#### Scenario: Russian UI
- **WHEN** Home Assistant runs with the Russian language
- **THEN** the countdown, power-on behavior and switch type entities and their options show Russian names instead of raw keys

### Requirement: Unsupported schedule datapoints ignored
The integration SHALL NOT create entities for datapoints 42 (`random_time`), 43 (`cycle_time`) and 44 (`switch_inching`). Reports of these datapoints MUST NOT cause errors.

#### Scenario: Schedule datapoint reported
- **WHEN** the device reports datapoint 44 with value `AAAC`
- **THEN** no entity is created for it and no error is logged
