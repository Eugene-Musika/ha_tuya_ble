"""Migration tests with Home Assistant's single-owner device registry."""

import inspect
from types import MappingProxyType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryDisabler
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components import tuya_ble
from custom_components.tuya_ble.config_flow import _account_unique_id, _build_device_subentry
from custom_components.tuya_ble.const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_UUID,
    DOMAIN,
)

LOGIN = {
    CONF_ENDPOINT: "https://example.invalid",
    CONF_ACCESS_ID: "access-id",
    CONF_ACCESS_SECRET: "access-secret",
    CONF_AUTH_TYPE: 0,
    CONF_USERNAME: "User@example.invalid",
    CONF_PASSWORD: "password-secret",
    CONF_COUNTRY_CODE: "US",
    CONF_APP_TYPE: "",
}


@skipUnless(
    "new_config_entry_id" in inspect.signature(dr.DeviceRegistry.async_update_device).parameters,
    "Requires Home Assistant's single-owner device registry",
)
class SingleOwnerMigrationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant(".")
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.config_entries._async_schedule_save = Mock()
        self.hass.config_entries.async_update_issues = Mock()
        self.hass.config_entries._store.async_load = AsyncMock(return_value=None)
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        self.devices = dr.async_get(self.hass)
        self.entities = er.async_get(self.hass)
        self.devices._store.async_load = AsyncMock(return_value=None)
        self.entities._store.async_load = AsyncMock(return_value=None)
        await self.devices.async_load()
        await self.entities.async_load()
        self.devices.async_schedule_save = Mock()
        self.entities.async_schedule_save = Mock()
        integration = SimpleNamespace(
            domain=DOMAIN, async_get_component=AsyncMock(return_value=tuya_ble)
        )
        self.loader = patch(
            "homeassistant.loader.async_get_integration",
            AsyncMock(return_value=integration),
        )
        self.loader.start()
        self.addCleanup(self.loader.stop)

    async def asyncTearDown(self) -> None:
        self.hass.import_executor.shutdown(wait=False)

    def make_entry(self, version, data, options, title, unique_id):
        entry = ConfigEntry(
            version=version,
            minor_version=1,
            domain=DOMAIN,
            title=title,
            data=data,
            options=options,
            source="user",
            unique_id=unique_id,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        self.hass.config_entries._entries[entry.entry_id] = entry
        return entry

    def make_legacy(self, number):
        address = f"AA:BB:CC:DD:EE:{number:02X}"
        credentials = {
            CONF_UUID: f"uuid-{number}",
            CONF_LOCAL_KEY: f"local-secret-{number}",
            CONF_DEVICE_ID: f"device-{number}",
            CONF_CATEGORY: "tdq",
            CONF_PRODUCT_ID: f"product-{number}",
            CONF_DEVICE_NAME: f"Device {number}",
            CONF_PRODUCT_NAME: None,
            CONF_PRODUCT_MODEL: None,
        }
        entry = self.make_entry(
            1, {CONF_ADDRESS: address}, {**LOGIN, **credentials}, f"Device {number}", address
        )
        device = self.devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=f"Device {number}",
        )
        device = self.devices.async_update_device(
            device.id, name_by_user=f"Custom {number}", area_id=f"area-{number}"
        )
        entity = self.entities.async_get_or_create(
            "switch",
            DOMAIN,
            f"device-{number}-switch",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"relay_{number}",
        )
        entity = self.entities.async_update_entity(
            entity.entity_id, name=f"Custom entity {number}", area_id=f"area-{number}"
        )
        return entry, device, entity

    def assert_moved(self, account, original_device, original_entity):
        device = self.devices.async_get(original_device.id)
        entity = self.entities.async_get(original_entity.entity_id)
        self.assertIsNotNone(device)
        self.assertIsNotNone(entity)
        self.assertEqual(device.config_entry_id, account.entry_id)
        self.assertIn(device.config_subentry_id, account.subentries)
        self.assertEqual(device.name_by_user, original_device.name_by_user)
        self.assertEqual(device.area_id, original_device.area_id)
        self.assertEqual(entity.config_entry_id, account.entry_id)
        self.assertEqual(entity.config_subentry_id, device.config_subentry_id)
        self.assertEqual(entity.device_id, device.id)
        self.assertEqual(entity.entity_id, original_entity.entity_id)
        self.assertEqual(entity.name, original_entity.name)
        self.assertEqual(entity.area_id, original_entity.area_id)

    async def test_real_new_api_migrates_and_keeps_registry_metadata(self) -> None:
        first, first_device, first_entity = self.make_legacy(1)
        second, second_device, second_entity = self.make_legacy(2)

        self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
        await self.hass.async_block_till_done()

        self.assertEqual(first.version, 2)
        self.assertIsNone(self.hass.config_entries.async_get_entry(second.entry_id))
        self.assert_moved(first, first_device, first_entity)
        self.assert_moved(first, second_device, second_entity)
        with patch.object(tuya_ble._LOGGER, "debug") as debug:
            await tuya_ble.async_setup(self.hass, {})
        debug.assert_not_called()
        self.assertEqual(len(first.subentries), 2)

    async def test_real_new_api_defers_collisions_and_retries(self) -> None:
        legacy, original, entity = self.make_legacy(1)
        account = self.make_entry(2, LOGIN.copy(), {}, "Account", _account_unique_id(LOGIN))
        duplicate = self.devices.async_get_or_create(
            config_entry_id=account.entry_id,
            identifiers=original.identifiers,
            connections=original.connections,
            name="Existing device",
        )
        duplicate = self.devices.async_update_device(
            duplicate.id, name_by_user="Existing name", area_id="existing-area"
        )
        self.assertNotEqual(original.id, duplicate.id)
        before_account, before_legacy = account.as_dict(), legacy.as_dict()

        with self.assertLogs(tuya_ble.__name__, level="ERROR") as messages:
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
        await self.hass.async_block_till_done()

        self.assertEqual(account.as_dict(), before_account)
        self.assertEqual(legacy.as_dict(), before_legacy)
        self.assertEqual(self.devices.async_get(original.id), original)
        self.assertEqual(self.devices.async_get(duplicate.id), duplicate)
        self.assertEqual(self.entities.async_get(entity.entity_id), entity)
        self.assertIn(original.id, "\n".join(messages.output))
        self.assertIn(duplicate.id, "\n".join(messages.output))

        self.devices.async_remove_device(duplicate.id)
        await tuya_ble.async_setup(self.hass, {})
        await self.hass.async_block_till_done()
        self.assertIsNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assert_moved(account, original, entity)

    async def test_disabled_first_entry_remains_separate(self) -> None:
        disabled, disabled_device, disabled_entity = self.make_legacy(1)
        disabled.disabled_by = ConfigEntryDisabler.USER
        active, active_device, active_entity = self.make_legacy(2)

        await tuya_ble.async_setup(self.hass, {})
        await self.hass.async_block_till_done()

        self.assertEqual(disabled.version, 1)
        self.assertEqual(disabled.disabled_by, ConfigEntryDisabler.USER)
        self.assertEqual(self.devices.async_get(disabled_device.id), disabled_device)
        self.assertEqual(self.entities.async_get(disabled_entity.entity_id), disabled_entity)
        self.assertEqual(active.version, 2)
        self.assert_moved(active, active_device, active_entity)
        disabled.disabled_by = None
        self.assertTrue(await disabled.async_migrate(self.hass))
        self.assertEqual(disabled.version, 1)

    async def test_colliding_legacy_sources_remain_unchanged(self) -> None:
        first, first_device, first_entity = self.make_legacy(1)
        second, second_device, second_entity = self.make_legacy(2)
        second_device = self.devices.async_update_device(
            second_device.id,
            new_identifiers=first_device.identifiers,
            new_connections=first_device.connections,
        )
        before_first, before_second = first.as_dict(), second.as_dict()

        with self.assertLogs(tuya_ble.__name__, level="ERROR"):
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
        await self.hass.async_block_till_done()

        self.assertEqual(first.as_dict(), before_first)
        self.assertEqual(second.as_dict(), before_second)
        self.assertEqual(self.devices.async_get(first_device.id), first_device)
        self.assertEqual(self.devices.async_get(second_device.id), second_device)
        self.assertEqual(self.entities.async_get(first_entity.entity_id), first_entity)
        self.assertEqual(self.entities.async_get(second_entity.entity_id), second_entity)

    async def test_orphan_collision_does_not_change_account_records(self) -> None:
        legacy, original, entity = self.make_legacy(1)
        account = self.make_entry(2, LOGIN.copy(), {}, "Account", _account_unique_id(LOGIN))
        subentry = _build_device_subentry(
            legacy.data[CONF_ADDRESS], legacy.title, legacy.options
        )
        self.hass.config_entries.async_add_subentry(account, subentry)
        duplicate = self.devices.async_get_or_create(
            config_entry_id=account.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers=original.identifiers,
            connections=original.connections,
            name="Existing device",
        )
        del self.hass.config_entries._entries[legacy.entry_id]
        before_account = account.as_dict()

        with self.assertLogs(tuya_ble.__name__, level="ERROR"):
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
        await self.hass.async_block_till_done()

        self.assertEqual(account.as_dict(), before_account)
        self.assertEqual(self.devices.async_get(original.id), original)
        self.assertEqual(self.devices.async_get(duplicate.id), duplicate)
        self.assertEqual(self.entities.async_get(entity.entity_id), entity)
