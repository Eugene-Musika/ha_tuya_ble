"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
import pycountry
from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from tuya_iot import AuthType

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult

from . import async_reload_account
from .tuya_ble import SERVICE_UUID, TuyaBLEDeviceCredentials

from .const import (
    DOMAIN,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_AUTH_TYPE,
    CONF_APP_TYPE,
    CONF_ENDPOINT,
    CONF_UUID,
    SMARTLIFE_APP,
    TUYA_SMART_APP,
    TUYA_COUNTRIES,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    TUYA_RESPONSE_SUCCESS,
)
from .devices import get_advertised_uuid, get_device_readable_name
from .cloud import CONF_TUYA_DEVICE_KEYS, CONF_TUYA_LOGIN_KEYS, HASSTuyaBLEDeviceManager

_LOGGER = logging.getLogger(__name__)


def _account_unique_id(login: Mapping[str, Any]) -> str:
    return f"{login[CONF_ENDPOINT]}|{login[CONF_ACCESS_ID]}|{login[CONF_USERNAME].lower()}"


@callback
def _address_is_configured(hass: HomeAssistant, address: str) -> bool:
    address = address.upper()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.version == 1 and entry.data.get(CONF_ADDRESS, "").upper() == address:
            return True
        if entry.version >= 2 and any(
            subentry.data.get(CONF_ADDRESS, "").upper() == address
            for subentry in entry.subentries.values()
        ):
            return True
    return False


def _build_device_subentry(
    address: str, title: str, credentials: Mapping[str, Any]
) -> ConfigSubentry:
    address = address.upper()
    data = {key: credentials.get(key) for key in CONF_TUYA_DEVICE_KEYS}
    data[CONF_ADDRESS] = address
    return ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type="device",
        title=title,
        unique_id=address,
    )


@callback
def _unconfigured_devices(
    hass: HomeAssistant,
) -> dict[str, BluetoothServiceInfoBleak]:
    devices: dict[str, BluetoothServiceInfoBleak] = {}
    for discovery in async_discovered_service_info(hass):
        address = discovery.address.upper()
        if (
            not _address_is_configured(hass, address)
            and discovery.service_data
            and SERVICE_UUID in discovery.service_data
        ):
            devices[address] = discovery
    return devices


async def _get_discovered_credentials(
    manager: HASSTuyaBLEDeviceManager,
    discovery: BluetoothServiceInfoBleak,
    force_update: bool = False,
) -> TuyaBLEDeviceCredentials | None:
    address = discovery.address.upper()
    uuid = get_advertised_uuid(discovery)
    credentials = await manager.get_device_credentials(
        address, force_update=force_update, uuid=uuid
    )
    if credentials is None and not force_update:
        credentials = await manager.get_device_credentials(
            address, force_update=True, uuid=uuid
        )
    return credentials


async def _show_device_form(
    flow: FlowHandler,
    step_id: str,
    devices: dict[str, BluetoothServiceInfoBleak],
    manager: HASSTuyaBLEDeviceManager,
    errors: dict[str, str],
    selected: str | None,
) -> FlowResult:
    names = {
        address: await get_device_readable_name(discovery, manager)
        for address, discovery in devices.items()
    }
    return flow.async_show_form(
        step_id=step_id,
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_ADDRESS,
                    default=selected if selected in devices else next(iter(devices)),
                ): vol.In(names)
            }
        ),
        errors=errors,
    )


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None
    data: dict[str, Any]

    country = [
        country
        for country in TUYA_COUNTRIES
        if country.name == user_input[CONF_COUNTRY_CODE]
    ][0]

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> FlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    def_country_name: str | None = None
    if country_code := flow.hass.config.country:
        def_country = pycountry.countries.get(alpha_2=country_code)
        if def_country:
            def_country_name = def_country.name

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders=placeholders,
    )


class TuyaBLEOptionsFlow(OptionsFlow):
    """Handle a Tuya BLE options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return await self.async_step_login(user_input)

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Validate a new login and refresh all device credentials."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}

        if user_input is not None:
            manager = HASSTuyaBLEDeviceManager(self.hass, {})
            login_data = await _try_login(manager, user_input, errors, placeholders)
            if login_data:
                manager.data.update(login_data)
                result = await self._async_save_login(manager, login_data, errors)
                if result is not None:
                    return result

        if user_input is None:
            user_input = dict(
                self.config_entry.options
                if self.config_entry.version == 1
                else self.config_entry.data
            )

        return _show_login_form(self, user_input, errors, placeholders)

    async def _async_save_login(
        self,
        manager: HASSTuyaBLEDeviceManager,
        login_data: dict[str, Any],
        errors: dict[str, str],
    ) -> FlowResult | None:
        entry = self.config_entry
        if entry.version == 1:
            address = entry.data[CONF_ADDRESS]
            credentials = await manager.get_device_credentials(
                address, force_update=True, uuid=entry.options.get(CONF_UUID)
            )
            if credentials is None:
                errors["base"] = "device_not_registered"
                return None
            options = {**entry.options, **login_data, **asdict(credentials)}
            self.hass.config_entries.async_update_entry(entry, options=options)
            if not await async_reload_account(self.hass, entry, force=True):
                errors["base"] = "reload_failed"
                return None
            return self.async_create_entry(
                title=entry.title,
                data=options,
            )

        unique_id = _account_unique_id(login_data)
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if existing is not None and existing.entry_id != entry.entry_id:
            errors["base"] = "account_already_configured"
            return None

        original_subentries = {
            subentry_id: dict(subentry.data)
            for subentry_id, subentry in entry.subentries.items()
        }
        refreshed: list[tuple[ConfigSubentry, TuyaBLEDeviceCredentials]] = []
        for subentry in tuple(entry.subentries.values()):
            credentials = await manager.get_device_credentials(
                subentry.data[CONF_ADDRESS],
                force_update=not refreshed,
                uuid=subentry.data.get(CONF_UUID),
            )
            if credentials is None:
                errors["base"] = "device_not_registered"
                return None
            refreshed.append((subentry, credentials))

        if original_subentries != {
            subentry_id: dict(subentry.data)
            for subentry_id, subentry in entry.subentries.items()
        }:
            errors["base"] = "devices_changed"
            return None
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if existing is not None and existing.entry_id != entry.entry_id:
            errors["base"] = "account_already_configured"
            return None

        for subentry, credentials in refreshed:
            self.hass.config_entries.async_update_subentry(
                entry, subentry, data={**subentry.data, **asdict(credentials)}
            )
        title = (
            login_data[CONF_USERNAME]
            if entry.title == entry.data.get(CONF_USERNAME)
            else entry.title
        )
        self.hass.config_entries.async_update_entry(
            entry, data=login_data, unique_id=unique_id, title=title
        )
        if not await async_reload_account(self.hass, entry, force=True):
            errors["base"] = "reload_failed"
            return None
        return self.async_create_entry(title=entry.title, data={})


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None
        self._get_device_info_error = False
        self._discovery_entry: ConfigEntry | None = None
        self._discovery_credentials: TuyaBLEDeviceCredentials | None = None
        self._discovery_name: str | None = None

    def _get_manager(self) -> HASSTuyaBLEDeviceManager:
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        return self._manager

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        address = discovery_info.address.upper()
        if _address_is_configured(self.hass, address):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.version < 2:
                continue
            manager = HASSTuyaBLEDeviceManager(self.hass, dict(entry.data))
            credentials = await _get_discovered_credentials(manager, discovery_info)
            if credentials is not None:
                self._discovery_entry = entry
                self._discovery_credentials = credentials
                self._discovery_name = await get_device_readable_name(
                    discovery_info, manager
                )
                self.context["title_placeholders"] = {"name": self._discovery_name}
                self._set_confirm_only()
                return await self.async_step_discovery_confirm()

        manager = self._get_manager()
        await manager.build_cache()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(discovery_info, manager)
        }
        return await self.async_step_login()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm adding a discovered device to an existing account."""
        assert self._discovery_info is not None
        assert self._discovery_entry is not None
        assert self._discovery_credentials is not None
        assert self._discovery_name is not None
        if user_input is None:
            return self.async_show_form(
                step_id="discovery_confirm",
                description_placeholders={"name": self._discovery_name},
            )
        address = self._discovery_info.address.upper()
        if _address_is_configured(self.hass, address):
            return self.async_abort(reason="already_configured")
        if self.hass.config_entries.async_get_entry(self._discovery_entry.entry_id) is None:
            return self.async_abort(reason="account_removed")
        subentry = _build_device_subentry(
            address, self._discovery_name, asdict(self._discovery_credentials)
        )
        self.hass.config_entries.async_add_subentry(self._discovery_entry, subentry)
        return self.async_abort(reason="device_added")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step."""
        await self._get_manager().build_cache()
        return await self.async_step_login(user_input)

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}
        manager = self._get_manager()

        if user_input is not None:
            data = await _try_login(manager, user_input, errors, placeholders)
            if data:
                self._data.update(data)
                if self._discovery_info:
                    return await self.async_step_device(
                        {CONF_ADDRESS: self._discovery_info.address.upper()}
                    )
                return await self.async_step_device()

        if user_input is None:
            user_input = {}
            if not self._data:
                manager.get_login_from_cache()
            user_input.update(self._data)

        return _show_login_form(self, user_input, errors, placeholders)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}
        manager = self._get_manager()

        if self._discovery_info:
            address = self._discovery_info.address.upper()
            self._discovered_devices = (
                {}
                if _address_is_configured(self.hass, address)
                else {address: self._discovery_info}
            )
        else:
            self._discovered_devices = _unconfigured_devices(self.hass)

        if not self._discovered_devices:
            if self._discovery_info:
                return self.async_abort(reason="already_configured")
            return await self._async_finish_account(None)

        selected = user_input[CONF_ADDRESS].upper() if user_input is not None else None
        if selected is not None:
            discovery_info = self._discovered_devices.get(selected)
            if discovery_info is None:
                errors["base"] = "device_unavailable"
            else:
                result = await self._async_add_selected_device(
                    manager, discovery_info, selected, errors
                )
                if result is not None:
                    return result

        return await _show_device_form(
            self, "device", self._discovered_devices, manager, errors, selected
        )

    async def _async_add_selected_device(
        self,
        manager: HASSTuyaBLEDeviceManager,
        discovery_info: BluetoothServiceInfoBleak,
        address: str,
        errors: dict[str, str],
    ) -> FlowResult | None:
        credentials = await _get_discovered_credentials(
            manager, discovery_info, self._get_device_info_error
        )
        if credentials is None:
            self._get_device_info_error = True
            errors["base"] = "device_not_registered"
            return None
        title = await get_device_readable_name(discovery_info, manager)
        subentry = _build_device_subentry(address, title, asdict(credentials))
        return await self._async_finish_account(subentry)

    async def _async_finish_account(
        self, subentry: ConfigSubentry | None
    ) -> FlowResult:
        login = {key: self._data[key] for key in CONF_TUYA_LOGIN_KEYS}
        existing = await self.async_set_unique_id(
            _account_unique_id(login), raise_on_progress=False
        )
        if subentry and _address_is_configured(self.hass, subentry.data[CONF_ADDRESS]):
            return self.async_abort(reason="already_configured")
        if existing is not None:
            if subentry is None or existing.version < 2:
                return self.async_abort(reason="already_configured")
            self.hass.config_entries.async_add_subentry(existing, subentry)
            return self.async_abort(reason="device_added")
        return self.async_create_entry(
            title=login[CONF_USERNAME],
            data=login,
            subentries=[subentry.as_dict()] if subentry else [],
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"device": DeviceSubentryFlow} if config_entry.version >= 2 else {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow()


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add a discovered device to an existing account."""

    def __init__(self) -> None:
        self._get_device_info_error = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_known_entry(self.handler[0])
        manager = HASSTuyaBLEDeviceManager(self.hass, dict(entry.data))
        devices = _unconfigured_devices(self.hass)
        if not devices:
            return self.async_abort(reason="no_unconfigured_devices")

        errors: dict[str, str] = {}
        selected = user_input[CONF_ADDRESS].upper() if user_input is not None else None
        if selected is not None:
            discovery = devices.get(selected)
            if discovery is None:
                errors["base"] = "device_unavailable"
            else:
                credentials = await _get_discovered_credentials(
                    manager, discovery, self._get_device_info_error
                )
                if credentials is None:
                    self._get_device_info_error = True
                    errors["base"] = "device_not_registered"
                else:
                    title = await get_device_readable_name(discovery, manager)
                    if _address_is_configured(self.hass, selected):
                        return self.async_abort(reason="already_configured")
                    subentry = _build_device_subentry(selected, title, asdict(credentials))
                    return self.async_create_entry(
                        title=subentry.title,
                        data=subentry.data,
                        unique_id=subentry.unique_id,
                    )

        return await _show_device_form(self, "user", devices, manager, errors, selected)
