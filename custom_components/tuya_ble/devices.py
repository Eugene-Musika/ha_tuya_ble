"""The Tuya BLE integration."""
from __future__ import annotations
from dataclasses import dataclass

from typing import Any
import logging
from homeassistant.const import CONF_ADDRESS, CONF_DEVICE_ID

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import (
    DeviceInfo,
    EntityDescription,
    generate_entity_id,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
import datetime

from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.bluetooth.passive_update_coordinator import PassiveBluetoothCoordinatorEntity, PassiveBluetoothDataUpdateCoordinator
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from .tuya_ble import (
    AbstaractTuyaBLEDeviceManager,
    TuyaBLEDataPoint,
    TuyaBLEDevice,
    TuyaBLEDeviceCredentials,
    decode_advertisement_uuid,
)

from .cloud import HASSTuyaBLEDeviceManager
from .const import (
    DEVICE_DEF_MANUFACTURER,
    DOMAIN,
    FINGERBOT_BUTTON_EVENT,
    SET_DISCONNECTED_DELAY,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLEFingerbotInfo:
    switch: int
    mode: int
    up_position: int
    down_position: int
    hold_time: int
    reverse_positions: int
    manual_control: int = 0
    program: int = 0

@dataclass
class TuyaBLELockInfo:
    alarm_lock: int
    unlock_ble: int
    unlock_fingerprint: int
    unlock_password: int

@dataclass
class TuyaBLEProductInfo:
    name: str
    manufacturer: str = DEVICE_DEF_MANUFACTURER
    fingerbot: TuyaBLEFingerbotInfo | None = None
    lock: TuyaBLELockInfo | None = None
    # Some firmwares answer the status request with only part of their DPs.
    # If state_probe_dp is not reported after connecting, state_refresh_dp is
    # rewritten with its current value, which makes the device report all DPs.
    state_probe_dp: int | None = None
    state_refresh_dp: int | None = None
    # Show the last known state (needs RestoreEntity) until the DP is reported.
    restore_state: bool = False


class TuyaBLEEntity(PassiveBluetoothCoordinatorEntity):
    """Tuya BLE base entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLEPassiveCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._coordinator = coordinator
        self._device = device
        self._product = product
        if description.name is None:
            # Explicit name=None marks the device's main entity (uses device name).
            self._attr_name = None
        elif description.translation_key is None:
            self._attr_translation_key = description.key
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_device_info = get_device_info(self._device)
        self._attr_unique_id = f"{self._device.device_id}-{description.key}"
        self._restored_state: str | None = None
        self.entity_id = generate_entity_id(
            "sensor.{}", self._attr_unique_id, hass=hass
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._product.restore_state and isinstance(self, RestoreEntity):
            last_state = await self.async_get_last_state()
            if last_state and last_state.state not in ("unknown", "unavailable"):
                self._restored_state = last_state.state

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TuyaBLECoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data coordinator for receiving Tuya BLE updates."""

    def __init__(self, hass: HomeAssistant, device: TuyaBLEDevice) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self._device = device
        self._disconnected: bool = True
        self._unsub_disconnect: CALLBACK_TYPE | None = None
        device.register_connected_callback(self._async_handle_connect)
        device.register_callback(self._async_handle_update)
        device.register_disconnected_callback(self._async_handle_disconnect)

    @property
    def connected(self) -> bool:
        return not self._disconnected

    @callback
    def _async_handle_connect(self) -> None:
        if self._unsub_disconnect is not None:
            self._unsub_disconnect()
        if self._disconnected:
            self._disconnected = False
            self.async_update_listeners()

    @callback
    def _async_handle_update(self, updates: list[TuyaBLEDataPoint]) -> None:
        self._async_handle_connect()
        self.async_update_listeners()
        info = get_device_product_info(self._device)
        if not info:
            return
        # Fingerbot events
        if info.fingerbot and info.fingerbot.manual_control != 0:
            for update in updates:
                if update.id == info.fingerbot.switch and update.changed_by_device:
                    self.hass.bus.fire(
                        FINGERBOT_BUTTON_EVENT,
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                        },
                    )
        # Lock events
        if info.lock:
            for update in updates:
                if not update.changed_by_device:
                    continue
                if update.id == info.lock.alarm_lock:
                    self.hass.bus.fire(
                        f"{DOMAIN}_lock_alarm_event",
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                            "event": "alarm_lock",
                            "value": update.value,
                        },
                    )
                elif update.id == info.lock.unlock_ble:
                    self.hass.bus.fire(
                        f"{DOMAIN}_lock_unlock_ble_event",
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                            "event": "unlock_ble",
                            "value": update.value,
                        },
                    )
                elif update.id == info.lock.unlock_fingerprint:
                    self.hass.bus.fire(
                        f"{DOMAIN}_lock_unlock_fingerprint_event",
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                            "event": "unlock_fingerprint",
                            "value": update.value,
                        },
                    )
                elif update.id == info.lock.unlock_password:
                    self.hass.bus.fire(
                        f"{DOMAIN}_lock_unlock_password_event",
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                            "event": "unlock_password",
                            "value": update.value,
                        },
                    )

    @callback
    def _set_disconnected(self, _: "datetime.datetime") -> None:
        """Invoke the idle timeout callback, called when the alarm fires."""
        self._disconnected = True
        self._unsub_disconnect = None
        self.async_update_listeners()

    @callback
    def _async_handle_disconnect(self) -> None:
        """Trigger the callbacks for disconnected."""
        if self._unsub_disconnect is None:
            delay: float = SET_DISCONNECTED_DELAY
            self._unsub_disconnect = async_call_later(
                self.hass, delay, self._set_disconnected
            )


class TuyaBLEPassiveCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Data coordinator для получения обновлений Tuya BLE через пассивный Bluetooth."""
    def __init__(self, hass: HomeAssistant, logger: logging.Logger, address: str, device: TuyaBLEDevice):
        super().__init__(hass, logger, address, BluetoothScanningMode.ACTIVE, connectable=True)
        self._device = device
        self._disconnected: bool = True
        self._unsub_disconnect: CALLBACK_TYPE | None = None
        self._full_state_received: bool = False
        self._full_state_requested: bool = False
        device.register_connected_callback(self._async_handle_connect)
        device.register_callback(self._async_handle_update)
        device.register_disconnected_callback(self._async_handle_disconnect)

    @property
    def connected(self) -> bool:
        return not self._disconnected

    @callback
    def _async_handle_connect(self) -> None:
        if self._unsub_disconnect is not None:
            self._unsub_disconnect()
        if self._disconnected:
            self._disconnected = False
            self.async_update_listeners()

    @callback
    def _async_handle_update(self, updates: list[TuyaBLEDataPoint]) -> None:
        self._async_handle_connect()
        self.async_update_listeners()
        info = get_device_product_info(self._device)
        if info:
            self._request_full_state_if_needed(info, updates)
        if info and info.fingerbot and info.fingerbot.manual_control != 0:
            for update in updates:
                if update.id == info.fingerbot.switch and update.changed_by_device:
                    self.hass.bus.fire(
                        FINGERBOT_BUTTON_EVENT,
                        {
                            CONF_ADDRESS: self._device.address,
                            CONF_DEVICE_ID: self._device.device_id,
                        },
                    )
        if info and info.lock:
            for update in updates:
                if update.changed_by_device:
                    if update.id == info.lock.alarm_lock:
                        self.hass.bus.fire(
                            f"{DOMAIN}_lock_alarm_event",
                            {
                                CONF_ADDRESS: self._device.address,
                                CONF_DEVICE_ID: self._device.device_id,
                                "event": "alarm_lock",
                                "value": update.value,
                            },
                        )
                    elif update.id == info.lock.unlock_ble:
                        self.hass.bus.fire(
                            f"{DOMAIN}_lock_unlock_ble_event",
                            {
                                CONF_ADDRESS: self._device.address,
                                CONF_DEVICE_ID: self._device.device_id,
                                "event": "unlock_ble",
                                "value": update.value,
                            },
                        )
                    elif update.id == info.lock.unlock_fingerprint:
                        self.hass.bus.fire(
                            f"{DOMAIN}_lock_unlock_fingerprint_event",
                            {
                                CONF_ADDRESS: self._device.address,
                                CONF_DEVICE_ID: self._device.device_id,
                                "event": "unlock_fingerprint",
                                "value": update.value,
                            },
                        )
                    elif update.id == info.lock.unlock_password:
                        self.hass.bus.fire(
                            f"{DOMAIN}_lock_unlock_password_event",
                            {
                                CONF_ADDRESS: self._device.address,
                                CONF_DEVICE_ID: self._device.device_id,
                                "event": "unlock_password",
                                "value": update.value,
                            },
                        )

    @callback
    def _request_full_state_if_needed(
        self, info: TuyaBLEProductInfo, updates: list[TuyaBLEDataPoint]
    ) -> None:
        if info.state_probe_dp is None or info.state_refresh_dp is None:
            return
        if self._full_state_received:
            return
        if any(update.id == info.state_probe_dp for update in updates):
            self._full_state_received = True
            return
        if self._full_state_requested:
            return
        datapoint = self._device.datapoints[info.state_refresh_dp]
        if datapoint is None:
            return
        self._full_state_requested = True
        self.logger.debug(
            "%s: DP %s missing from status, rewriting DP %s to get full state",
            self._device.address,
            info.state_probe_dp,
            info.state_refresh_dp,
        )
        self.hass.async_create_task(datapoint.set_value(datapoint.value))

    @callback
    def _set_disconnected(self, _: "datetime.datetime") -> None:
        self._disconnected = True
        self._unsub_disconnect = None
        self.async_update_listeners()

    @callback
    def _async_handle_disconnect(self) -> None:
        self._full_state_received = False
        self._full_state_requested = False
        if self._unsub_disconnect is None:
            delay: float = SET_DISCONNECTED_DELAY
            self._unsub_disconnect = async_call_later(
                self.hass, delay, self._set_disconnected
            )


@dataclass
class TuyaBLEData:
    """Data for the Tuya BLE integration."""

    title: str
    device: TuyaBLEDevice
    product: TuyaBLEProductInfo
    manager: HASSTuyaBLEDeviceManager
    coordinator: TuyaBLEPassiveCoordinator


@dataclass
class TuyaBLECategoryInfo:
    products: dict[str, TuyaBLEProductInfo]
    info: TuyaBLEProductInfo | None = None


devices_database: dict[str, TuyaBLECategoryInfo] = {
    "co2bj": TuyaBLECategoryInfo(
        products={
            "59s19z5m": TuyaBLEProductInfo(  # device product_id
                name="CO2 Detector",
            ),
        },
    ),
    "ms": TuyaBLECategoryInfo(
        products={
            **dict.fromkeys(
                [
                    "ludzroix",
                    "isk2p555",
                    "yy2bmcoh",
                ],
                    TuyaBLEProductInfo(  # device product_id
                    name="Smart Lock",
                ),
            ),
            "mqc2hevy": TuyaBLEProductInfo(  # device product_id
                name="Smart Lock",
                lock=TuyaBLELockInfo(
                    alarm_lock=21,
                    unlock_ble=19,
                    unlock_fingerprint=12,
                    unlock_password=13,
                ),
            ),
        },
    ),
    "szjqr": TuyaBLECategoryInfo(
        products={
            "3yqdo5yt": TuyaBLEProductInfo(  # device product_id
                name="CUBETOUCH 1s",
                fingerbot=TuyaBLEFingerbotInfo(
                    switch=1,
                    mode=2,
                    up_position=5,
                    down_position=6,
                    hold_time=3,
                    reverse_positions=4,
                ),
            ),
            "xhf790if": TuyaBLEProductInfo(  # device product_id
                name="CubeTouch II",
                fingerbot=TuyaBLEFingerbotInfo(
                    switch=1,
                    mode=2,
                    up_position=5,
                    down_position=6,
                    hold_time=3,
                    reverse_positions=4,
                ),
            ),
            **dict.fromkeys(
                [
                    "blliqpsj",
                    "ndvkgsrm",
                    "yiihr7zh",
                    "neq16kgd"
                ],  # device product_ids
                TuyaBLEProductInfo(
                    name="Fingerbot Plus",
                    fingerbot=TuyaBLEFingerbotInfo(
                        switch=2,
                        mode=8,
                        up_position=15,
                        down_position=9,
                        hold_time=10,
                        reverse_positions=11,
                        manual_control=17,
                        program=121,
                    ),
                ),
            ),
            **dict.fromkeys(
                [
                    "ltak7e1p",
                    "y6kttvd6",
                    "yrnk7mnn",
                    "nvr2rocq",
                    "bnt7wajf",
                    "rvdceqjh",
                    "5xhbk964",
                ],  # device product_ids
                TuyaBLEProductInfo(
                    name="Fingerbot",
                    fingerbot=TuyaBLEFingerbotInfo(
                        switch=2,
                        mode=8,
                        up_position=15,
                        down_position=9,
                        hold_time=10,
                        reverse_positions=11,
                        program=121,
                    ),
                ),
            ),
        },
    ),
    "wk": TuyaBLECategoryInfo(
        products={
            **dict.fromkeys(
            [
            "drlajpqc",
            "nhj2j7su",
            ],  # device product_id
            TuyaBLEProductInfo(
                name="Thermostatic Radiator Valve",
                ),
            ),
        },
    ),
    "wsdcg": TuyaBLECategoryInfo(
        products={
            "ojzlzzsw": TuyaBLEProductInfo(  # device product_id
                name="Soil moisture sensor",
            ),
        },
    ),
    "znhsb": TuyaBLECategoryInfo(
        products={
            "cdlandip":  # device product_id
            TuyaBLEProductInfo(
                name="Smart water bottle",
            ),
        },
    ),
    "ggq": TuyaBLECategoryInfo(
        products={
            **dict.fromkeys(
                [
                "6pahkcau",
                "hfgdqhho",
                ],  # device product_id
                TuyaBLEProductInfo(
                    name="Irrigation computer",
                ),
            ),
        },
    ),
    "tdq": TuyaBLECategoryInfo(
        products={
            "vs4ksbpax2dwwm6a": TuyaBLEProductInfo(  # device product_id
                name="Mini Smart Switch",
                state_probe_dp=1,
                state_refresh_dp=47,
                restore_state=True,
            ),
        },
    ),
}


def get_product_info_by_ids(
    category: str, product_id: str
) -> TuyaBLEProductInfo | None:
    category_info = devices_database.get(category)
    if category_info is not None:
        product_info = category_info.products.get(product_id)
        if product_info is not None:
            return product_info
        return category_info.info
    else:
        return None


def get_device_product_info(device: TuyaBLEDevice) -> TuyaBLEProductInfo | None:
    return get_product_info_by_ids(device.category, device.product_id)


def get_short_address(address: str) -> str:
    results = address.replace("-", ":").upper().split(":")
    return f"{results[-3]}{results[-2]}{results[-1]}"[-6:]


def get_advertised_uuid(discovery_info: BluetoothServiceInfoBleak) -> str | None:
    uuid = decode_advertisement_uuid(
        discovery_info.service_data,
        discovery_info.manufacturer_data,
    )
    if uuid is None:
        _LOGGER.debug(
            "Cannot decode uuid of %s; service_data: %s; manufacturer_data: %s",
            discovery_info.address,
            {k: v.hex() for k, v in (discovery_info.service_data or {}).items()},
            {k: v.hex() for k, v in (discovery_info.manufacturer_data or {}).items()},
        )
    return uuid


async def get_device_readable_name(
    discovery_info: BluetoothServiceInfoBleak,
    manager: AbstaractTuyaBLEDeviceManager | None,
) -> str:
    credentials: TuyaBLEDeviceCredentials | None = None
    product_info: TuyaBLEProductInfo | None = None
    if manager:
        credentials = await manager.get_device_credentials(
            discovery_info.address,
            uuid=get_advertised_uuid(discovery_info),
        )
        if credentials:
            product_info = get_product_info_by_ids(
                credentials.category,
                credentials.product_id,
            )
    short_address = get_short_address(discovery_info.address)
    if product_info:
        return "%s %s" % (product_info.name, short_address)
    if credentials:
        return "%s %s" % (credentials.device_name, short_address)
    return "%s %s" % (discovery_info.device.name, short_address)


def get_device_info(device: TuyaBLEDevice) -> DeviceInfo | None:
    product_info = None
    if device.category and device.product_id:
        product_info = get_product_info_by_ids(device.category, device.product_id)
    product_name: str
    if product_info:
        product_name = product_info.name
    else:
        product_name = device.name
    result = DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        hw_version=device.hardware_version,
        identifiers={(DOMAIN, device.address)},
        manufacturer=(
            product_info.manufacturer if product_info else DEVICE_DEF_MANUFACTURER
        ),
        model=("%s (%s)")
        % (
            device.product_model or product_name,
            device.product_id,
        ),
        name=("%s %s")
        % (
            product_name,
            get_short_address(device.address),
        ),
        sw_version=("%s (protocol %s)")
        % (
            device.device_version,
            device.protocol_version,
        ),
    )
    return result
