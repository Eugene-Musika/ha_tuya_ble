"""The Tuya BLE integration."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import get_device

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .tuya_ble import TuyaBLEDevice

from .cloud import (
    CONF_TUYA_DEVICE_KEYS,
    CONF_TUYA_LOGIN_KEYS,
    HASSTuyaBLEDeviceManager,
    TuyaBLEDeviceManagerView,
)
from .const import CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_APP_TYPE, CONF_ENDPOINT, DOMAIN
from .devices import (
    TuyaBLEAccountData,
    TuyaBLEData,
    TuyaBLEPassiveCoordinator,
    get_device_product_info,
)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TEXT,
]

_LOGGER = logging.getLogger(__name__)
_DATA_RELOAD_LOCKS = f"{DOMAIN}_reload_locks"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _saved_login(options: Mapping[str, Any]) -> dict[str, Any]:
    """Read a v1 login, defaulting a missing app type like the cloud login does."""
    login = {key: options.get(key) for key in CONF_TUYA_LOGIN_KEYS}
    if login[CONF_APP_TYPE] is None:
        login[CONF_APP_TYPE] = ""
    return login


def _has_complete_login(options: Mapping[str, Any]) -> bool:
    """Check all saved login fields, allowing valid zero/empty app type values."""
    required_strings = (
        CONF_ENDPOINT,
        CONF_ACCESS_ID,
        CONF_ACCESS_SECRET,
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_COUNTRY_CODE,
    )
    login = _saved_login(options)
    return all(value is not None for value in login.values()) and all(
        isinstance(login[key], str) and login[key] for key in required_strings
    )


def _device_subentry(
    entry: ConfigEntry, address: str
) -> ConfigSubentry | None:
    """Find an existing device subentry by address."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != "device":
            continue
        if any(
            isinstance(value, str) and value.upper() == address
            for value in (subentry.unique_id, subentry.data.get(CONF_ADDRESS))
        ):
            return subentry
    return None


@callback
def _async_ensure_device_subentry(
    hass: HomeAssistant, account: ConfigEntry, source: ConfigEntry
) -> ConfigSubentry:
    """Keep the saved device data before converting or removing its entry."""
    from .config_flow import _build_device_subentry

    address = source.data[CONF_ADDRESS].upper()
    subentry = _device_subentry(account, address)
    if subentry is None:
        subentry = _device_subentry(source, address)
        if subentry is None:
            subentry = _build_device_subentry(address, source.title, source.options)
        hass.config_entries.async_add_subentry(account, subentry)
        _LOGGER.debug(
            "Added device subentry %s to entry %s",
            subentry.subentry_id,
            account.entry_id,
        )

    data = dict(subentry.data)
    changed = False
    for key in (CONF_ADDRESS, *CONF_TUYA_DEVICE_KEYS):
        value = address if key == CONF_ADDRESS else source.options.get(key)
        if data.get(key) is None and value is not None:
            data[key] = value
            changed = True
    if changed:
        hass.config_entries.async_update_subentry(account, subentry, data=data)
        _LOGGER.debug("Recovered device data for subentry %s", subentry.subentry_id)
    return subentry


def _device_subentries(device: dr.DeviceEntry, entry_id: str) -> set[str | None]:
    """Read device ownership on both the single- and multi-owner registries."""
    if hasattr(device, "config_entry_id"):
        if device.config_entry_id == entry_id:
            return {device.config_subentry_id}
        return set()
    return device.config_entries_subentries.get(entry_id, set())


def _device_entry_ids(device: dr.DeviceEntry) -> set[str]:
    if hasattr(device, "config_entry_id"):
        return {device.config_entry_id}
    return set(device.config_entries_subentries)


def _check_device_collisions(
    device_registry: dr.DeviceRegistry,
    account: ConfigEntry,
    devices: list[dr.DeviceEntry],
    use_new_api: bool,
) -> None:
    """Do not change entities if a single-owner registry cannot move their device."""
    if not use_new_api or not hasattr(device_registry, "async_get_devices"):
        return
    for device in devices:
        matches = device_registry.async_get_devices(
            identifiers=device.identifiers or None,
            connections=device.connections or None,
            config_entry_id=account.entry_id,
        )
        for match in matches:
            if match.id != device.id:
                raise RuntimeError(
                    f"Cannot migrate device {device.id}: entry {account.entry_id} "
                    f"already contains device {match.id} with the same identity"
                )


@callback
def _async_move_registry_entries(
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    source_id: str,
    account: ConfigEntry,
    subentry: ConfigSubentry,
    devices: list[dr.DeviceEntry],
    entities: list[er.RegistryEntry],
    use_new_api: bool,
) -> None:
    """Move registry records without triggering HA's orphan-entity cleanup."""
    target_id = account.entry_id
    subentry_id = subentry.subentry_id
    _check_device_collisions(device_registry, account, devices, use_new_api)

    # Device registry callbacks immediately remove entities with mismatched owners.
    for entity in entities:
        if (
            entity.config_entry_id == target_id
            and entity.config_subentry_id == subentry_id
        ):
            continue
        entity_registry.async_update_entity(
            entity.entity_id,
            config_entry_id=target_id,
            config_subentry_id=subentry_id,
        )
        _LOGGER.debug(
            "Moved entity %s to entry %s subentry %s",
            entity.entity_id,
            target_id,
            subentry_id,
        )

    for device in devices:
        current = device_registry.async_get(device.id)
        if current is None:
            raise RuntimeError(f"Device {device.id} disappeared during migration")
        if use_new_api:
            if _device_subentries(current, target_id) != {subentry_id}:
                device_registry.async_update_device(
                    device.id,
                    new_config_entry_id=target_id,
                    new_config_subentry_id=subentry_id,
                )
                _LOGGER.debug(
                    "Moved device %s to entry %s subentry %s using new API",
                    device.id,
                    target_id,
                    subentry_id,
                )
            continue

        if subentry_id not in _device_subentries(current, target_id):
            device_registry.async_update_device(
                device.id,
                add_config_entry_id=target_id,
                add_config_subentry_id=subentry_id,
            )
            _LOGGER.debug(
                "Attached device %s to entry %s subentry %s using add/remove",
                device.id,
                target_id,
                subentry_id,
            )
            current = device_registry.async_get(device.id)
            if current is None:
                raise RuntimeError(f"Device {device.id} disappeared during migration")

        if source_id == target_id:
            obsolete = {
                old_subentry_id
                for old_subentry_id in _device_subentries(current, target_id)
                if old_subentry_id != subentry_id
                and (
                    old_subentry_id is None
                    or old_subentry_id not in account.subentries
                )
            }
            for old_subentry_id in obsolete:
                device_registry.async_update_device(
                    device.id,
                    remove_config_entry_id=target_id,
                    remove_config_subentry_id=old_subentry_id,
                )
                _LOGGER.debug(
                    "Removed obsolete subentry association from device %s", device.id
                )
        elif _device_subentries(current, source_id):
            device_registry.async_update_device(
                device.id, remove_config_entry_id=source_id
            )
            _LOGGER.debug(
                "Removed old entry %s association from device %s",
                source_id,
                device.id,
            )


def _matches_device_address(device: dr.DeviceEntry, address: str) -> bool:
    """Match a subentry to its previously registered Bluetooth device."""
    return any(
        namespace == DOMAIN and value.upper() == address
        for namespace, value in device.identifiers
    ) or any(
        namespace == dr.CONNECTION_BLUETOOTH and value.upper() == address
        for namespace, value in device.connections
    )


def _has_device_identifier(device: dr.DeviceEntry, address: str) -> bool:
    return any(
        namespace == DOMAIN and value.upper() == address
        for namespace, value in device.identifiers
    )


def _devices_for_address(
    device_registry: dr.DeviceRegistry, address: str
) -> list[dr.DeviceEntry]:
    identifiers = {(DOMAIN, address), (DOMAIN, address.lower())}
    if hasattr(device_registry, "async_get_devices"):
        return device_registry.async_get_devices(identifiers=identifiers)
    device = device_registry.async_get_device(identifiers=identifiers)
    return [device] if device is not None else []


@callback
def _migration_collision(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    account: ConfigEntry | None,
    sources: list[ConfigEntry],
) -> tuple[str, str] | None:
    """Find registry records that cannot coexist in the target account."""
    entries = [*sources]
    if account is not None:
        entries.append(account)
    devices = {
        device.id: device
        for entry in entries
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    }
    addresses = {
        address.upper()
        for entry in sources
        if isinstance(address := entry.data.get(CONF_ADDRESS), str)
    }
    if account is not None:
        addresses.update(
            address.upper()
            for subentry in account.subentries.values()
            if isinstance(address := subentry.data.get(CONF_ADDRESS), str)
        )
    for address in addresses:
        for device in _devices_for_address(device_registry, address):
            if _has_device_identifier(device, address) and any(
                hass.config_entries.async_get_entry(entry_id) is None
                for entry_id in _device_entry_ids(device)
            ):
                devices[device.id] = device

    identities: dict[tuple[str, tuple[str, str]], str] = {}
    for device in devices.values():
        for kind, values in (
            ("identifier", device.identifiers),
            ("connection", device.connections),
        ):
            for identity in values:
                key = (kind, identity)
                previous = identities.setdefault(key, device.id)
                if previous != device.id:
                    return previous, device.id
    return None


def _log_migration_collision(
    entries: list[ConfigEntry], collision: tuple[str, str]
) -> None:
    _LOGGER.error(
        "Deferring Tuya BLE migration for entries %s: device registry records %s "
        "and %s share an identifier or connection; resolve the conflict and restart",
        [entry.entry_id for entry in entries],
        *collision,
    )


@callback
def _async_reconcile_account(
    hass: HomeAssistant,
    account: ConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    use_new_api: bool,
) -> None:
    """Finish assignments after the original entry was converted in place."""
    subentries = [
        subentry
        for subentry in account.subentries.values()
        if subentry.subentry_type == "device"
        and isinstance(subentry.data.get(CONF_ADDRESS), str)
    ]

    for subentry in subentries:
        address = subentry.data[CONF_ADDRESS].upper()
        devices = {
            device.id: device
            for device in dr.async_entries_for_config_entry(
                device_registry, account.entry_id
            )
            if _matches_device_address(device, address)
        }
        for device in _devices_for_address(device_registry, address):
            if _has_device_identifier(device, address) and any(
                hass.config_entries.async_get_entry(entry_id) is None
                for entry_id in _device_entry_ids(device)
            ):
                devices[device.id] = device

        matching_devices = []
        for device in devices.values():
            owned_subentries = _device_subentries(device, account.entry_id)
            valid_other_subentries = {
                subentry_id
                for subentry_id in owned_subentries
                if subentry_id in account.subentries
                and subentry_id != subentry.subentry_id
            }
            if valid_other_subentries and subentry.subentry_id not in owned_subentries:
                continue
            matching_devices.append(device)

        device_ids = {device.id for device in matching_devices}
        device_id = subentry.data.get(CONF_DEVICE_ID)
        _check_device_collisions(
            device_registry, account, matching_devices, use_new_api
        )
        matching_entities = []
        for entity in entity_registry.entities.values():
            if entity.config_entry_id == account.entry_id:
                if entity.config_subentry_id in account.subentries:
                    continue
            elif (
                entity.platform != DOMAIN
                or entity.config_entry_id is None
                or hass.config_entries.async_get_entry(entity.config_entry_id)
                is not None
            ):
                continue
            if entity.device_id in device_ids:
                matching_entities.append(entity)
                continue
            if entity.device_id is not None or not isinstance(entity.unique_id, str):
                continue
            candidates = [
                candidate
                for candidate in subentries
                if isinstance(candidate.data.get(CONF_DEVICE_ID), str)
                and entity.unique_id.startswith(
                    f"{candidate.data[CONF_DEVICE_ID]}-"
                )
            ]
            if candidates:
                longest = max(
                    len(candidate.data[CONF_DEVICE_ID]) for candidate in candidates
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if len(candidate.data[CONF_DEVICE_ID]) == longest
                ]
            if len(candidates) == 1 and candidates[0] is subentry:
                matching_entities.append(entity)
            elif (
                not candidates
                and len(subentries) == 1
                and not device_id
                and entity.config_entry_id == account.entry_id
            ):
                matching_entities.append(entity)

        _async_move_registry_entries(
            device_registry,
            entity_registry,
            account.entry_id,
            account,
            subentry,
            [],
            matching_entities,
            use_new_api,
        )
        for device in matching_devices:
            orphaned_entries = [
                entry_id
                for entry_id in _device_entry_ids(device)
                if hass.config_entries.async_get_entry(entry_id) is None
            ]
            for entry_id in orphaned_entries:
                _async_move_registry_entries(
                    device_registry,
                    entity_registry,
                    entry_id,
                    account,
                    subentry,
                    [device],
                    [],
                    use_new_api,
                )
            current = device_registry.async_get(device.id)
            if current is not None and _device_subentries(current, account.entry_id):
                _async_move_registry_entries(
                    device_registry,
                    entity_registry,
                    account.entry_id,
                    account,
                    subentry,
                    [current],
                    [],
                    use_new_api,
                )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Merge old device entries before setting up any account entries."""
    from .config_flow import _account_unique_id

    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False)
    accounts = {
        entry.unique_id: entry
        for entry in entries
        if entry.version == 2 and entry.unique_id is not None
    }
    groups: dict[str, list[ConfigEntry]] = {}
    for entry in entries:
        if (
            entry.version != 1
            or entry.disabled_by
            or not _has_complete_login(entry.options)
        ):
            continue
        groups.setdefault(_account_unique_id(_saved_login(entry.options)), []).append(
            entry
        )

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    parameters = inspect.signature(device_registry.async_update_device).parameters
    use_new_api = (
        "new_config_entry_id" in parameters
        and "new_config_subentry_id" in parameters
    )

    deferred_accounts: set[str] = set()
    for account_id, sources in groups.items():
        account = accounts.get(account_id)
        if account is not None and account.disabled_by:
            _LOGGER.warning(
                "Deferring Tuya BLE migration for entries %s: account entry %s is disabled",
                [source.entry_id for source in sources],
                account.entry_id,
            )
            deferred_accounts.add(account.entry_id)
            continue
        collision = _migration_collision(hass, device_registry, account, sources)
        if collision is not None:
            _log_migration_collision(
                [*sources, *([account] if account is not None else [])], collision
            )
            if account is not None:
                deferred_accounts.add(account.entry_id)
            continue
        for source in sources:
            if account is None:
                account = source
                subentry = _async_ensure_device_subentry(hass, account, source)
                login = _saved_login(source.options)
                hass.config_entries.async_update_entry(
                    account,
                    version=2,
                    data=login,
                    options={},
                    unique_id=account_id,
                    title=login[CONF_USERNAME],
                )
                accounts[account_id] = account
                _LOGGER.debug("Converted entry %s to an account", account.entry_id)
            else:
                subentry = _async_ensure_device_subentry(hass, account, source)

            devices = dr.async_entries_for_config_entry(
                device_registry, source.entry_id
            )
            entities = er.async_entries_for_config_entry(
                entity_registry, source.entry_id
            )
            if source.entry_id == account.entry_id:
                devices = [
                    device
                    for device in devices
                    if any(
                        subentry_id not in account.subentries
                        or subentry_id == subentry.subentry_id
                        for subentry_id in _device_subentries(device, account.entry_id)
                    )
                ]
                entities = [
                    entity
                    for entity in entities
                    if entity.config_subentry_id not in account.subentries
                ]
            _async_move_registry_entries(
                device_registry,
                entity_registry,
                source.entry_id,
                account,
                subentry,
                devices,
                entities,
                use_new_api,
            )
            if source.entry_id != account.entry_id:
                await hass.config_entries.async_remove(source.entry_id)
                _LOGGER.debug("Removed migrated entry %s", source.entry_id)

    for account in hass.config_entries.async_entries(DOMAIN, include_ignore=False):
        if account.version == 2 and account.entry_id not in deferred_accounts:
            collision = _migration_collision(hass, device_registry, account, [])
            if collision is not None:
                _log_migration_collision([account], collision)
                continue
            _async_reconcile_account(
                hass, account, device_registry, entity_registry, use_new_api
            )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Keep any unmerged v1 entry working with its legacy runtime."""
    return entry.version in (1, 2)


async def _async_setup_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manager: HASSTuyaBLEDeviceManager,
    ble_device: BLEDevice,
    device_data: Mapping[str, Any],
    title: str,
) -> TuyaBLEData | None:
    """Set up one device and its Bluetooth updates."""
    address = ble_device.address
    device = TuyaBLEDevice(TuyaBLEDeviceManagerView(manager, device_data), ble_device)
    await device.initialize()
    product_info = get_device_product_info(device)
    if product_info is None:
        _LOGGER.error("Could not determine product info for Tuya BLE device %s", address)
        return None

    coordinator = TuyaBLEPassiveCoordinator(hass, _LOGGER, address, device)
    hass.add_job(device.update())

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a ble callback."""
        device.set_ble_device_and_advertisement_data(
            service_info.device, service_info.advertisement
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher(address=address),
            BluetoothScanningMode.ACTIVE,
        )
    )

    return TuyaBLEData(title, device, product_info, manager, coordinator)


@callback
def _async_wait_for_device(
    hass: HomeAssistant, entry: ConfigEntry, address: str
) -> None:
    """Reload the account when a missing device is first seen."""
    reload_scheduled = False

    async def _async_reload_seen_device() -> None:
        nonlocal reload_scheduled
        reloaded = False
        try:
            reloaded = await async_reload_account(hass, entry, seen_address=address)
        finally:
            if not reloaded:
                reload_scheduled = False

    @callback
    def _async_device_seen(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        nonlocal reload_scheduled
        if not reload_scheduled:
            reload_scheduled = True
            hass.async_create_task(_async_reload_seen_device())

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_device_seen,
            BluetoothCallbackMatcher(address=address),
            BluetoothScanningMode.ACTIVE,
        )
    )


async def _async_setup_legacy_device(
    hass: HomeAssistant, entry: ConfigEntry, account: TuyaBLEAccountData
) -> None:
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address.upper(), True
    ) or await get_device(address)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Tuya BLE device with address {address}"
        )
    data = await _async_setup_device(
        hass, entry, account.manager, ble_device, entry.options, entry.title
    )
    if data is None:
        raise ConfigEntryNotReady(
            f"Could not determine product info for Tuya BLE device with address {address}"
        )
    account.devices[None] = data


async def _async_setup_account_devices(
    hass: HomeAssistant, entry: ConfigEntry, account: TuyaBLEAccountData
) -> None:
    for subentry_id, subentry in entry.subentries.items():
        address = subentry.data[CONF_ADDRESS].upper()
        ble_device = bluetooth.async_ble_device_from_address(hass, address, True)
        if ble_device is None:
            _LOGGER.debug("Waiting for Tuya BLE device %s", address)
            _async_wait_for_device(hass, entry, address)
            continue
        data = await _async_setup_device(
            hass, entry, account.manager, ble_device, subentry.data, subentry.title
        )
        if data is not None:
            account.devices[subentry_id] = data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an account and its available devices."""
    if entry.version == 1:
        login = {
            key: entry.options[key]
            for key in CONF_TUYA_LOGIN_KEYS
            if key in entry.options
        }
    else:
        login = dict(entry.data)
    account = TuyaBLEAccountData(
        HASSTuyaBLEDeviceManager(hass, login),
        {},
        {
            subentry_id: subentry.data[CONF_ADDRESS].upper()
            for subentry_id, subentry in entry.subentries.items()
        }
        if entry.version >= 2
        else {},
    )
    if entry.version == 1:
        await _async_setup_legacy_device(hass, entry, account)
    else:
        await _async_setup_account_devices(hass, entry, account)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = account
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop(event: Event) -> None:
        """Close all device connections."""
        for data in account.devices.values():
            await data.device.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True


async def async_reload_account(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    force: bool = False,
    seen_address: str | None = None,
) -> bool:
    """Serialize account reloads and recheck changes after waiting."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_DATA_RELOAD_LOCKS, {})
    async with locks.setdefault(entry.entry_id, asyncio.Lock()):
        current: TuyaBLEAccountData | None = hass.data.get(DOMAIN, {}).get(
            entry.entry_id
        )
        if current is None:
            if not force:
                return True
        else:
            if entry.version == 1:
                should_reload = entry.title != current.devices[None].title
            else:
                configured = {
                    subentry_id: subentry.data[CONF_ADDRESS].upper()
                    for subentry_id, subentry in entry.subentries.items()
                }
                for address in set(current.configured_subentries.values()) - set(
                    configured.values()
                ):
                    bluetooth.async_rediscover_address(hass, address)
                should_reload = (
                    configured != current.configured_subentries
                    or entry.data != current.manager.data
                    or any(
                        address == seen_address and subentry_id not in current.devices
                        for subentry_id, address in configured.items()
                    )
                )
            if not (force or should_reload):
                return True
        reloaded = await hass.config_entries.async_reload(entry.entry_id)
    if not reloaded:
        _LOGGER.error("Could not reload Tuya BLE entry %s", entry.entry_id)
    return reloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the account or its devices change."""
    await async_reload_account(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data: TuyaBLEAccountData = hass.data[DOMAIN].pop(entry.entry_id)
        for device_data in data.devices.values():
            await device_data.device.stop()

    return unload_ok
