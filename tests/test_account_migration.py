"""Account migration tests using Home Assistant's in-memory registries."""

import inspect
from types import MappingProxyType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipIf
from unittest.mock import AsyncMock, Mock, patch

import attr
import orjson

from homeassistant.config_entries import (
    ConfigEntries,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigSubentry,
)
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
from custom_components.tuya_ble.cloud import CONF_TUYA_DEVICE_KEYS
from custom_components.tuya_ble.config_flow import (
    _account_unique_id,
    _build_device_subentry,
)
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


class InterruptedMigration(Exception):
    """Simulate an interruption at a migration boundary."""


@skipIf(
    "new_config_entry_id" in inspect.signature(dr.DeviceRegistry.async_update_device).parameters,
    "These fixtures exercise the legacy multi-owner device registry",
)
class AccountMigrationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await self.reset_hass()
        integration = SimpleNamespace(
            domain=DOMAIN, async_get_component=AsyncMock(return_value=tuya_ble)
        )
        self.loader = patch(
            "homeassistant.loader.async_get_integration",
            AsyncMock(return_value=integration),
        )
        self.loader.start()
        self.addCleanup(self.loader.stop)

    async def reset_hass(
        self,
        entries: dict | None = None,
        devices: dict | None = None,
        entities: dict | None = None,
    ) -> None:
        if hasattr(self, "hass"):
            self.hass.import_executor.shutdown(wait=False)
        self.hass = HomeAssistant(".")
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.config_entries._async_schedule_save = Mock()
        self.hass.config_entries.async_update_issues = Mock()
        self.hass.config_entries._store.async_load = AsyncMock(return_value=entries)
        await self.hass.config_entries.async_initialize()
        self.devices = dr.async_get(self.hass)
        self.entities = er.async_get(self.hass)
        self.devices._store.async_load = AsyncMock(return_value=devices)
        self.entities._store.async_load = AsyncMock(return_value=entities)
        await self.devices.async_load()
        await self.entities.async_load()
        self.devices.async_schedule_save = Mock()
        self.entities.async_schedule_save = Mock()

    async def asyncTearDown(self) -> None:
        self.hass.import_executor.shutdown(wait=False)

    def make_entry(
        self,
        *,
        version: int,
        data: dict,
        options: dict,
        title: str,
        unique_id: str,
    ) -> ConfigEntry:
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

    def make_legacy(
        self, number: int, login: dict | None = None
    ) -> tuple[ConfigEntry, str, str]:
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
            version=1,
            data={CONF_ADDRESS: address.lower()},
            options={**(LOGIN if login is None else login), **credentials},
            title=f"Device {number}",
            unique_id=address,
        )
        device = self.devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=f"Device {number}",
        )
        device = self.devices.async_update_device(
            device.id, name_by_user=f"Custom device {number}", area_id=f"area-{number}"
        )
        entity = self.entities.async_get_or_create(
            "switch",
            DOMAIN,
            f"device-{number}-switch",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"bra_{number}",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        self.entities.async_update_entity(
            entity.entity_id, name=f"Custom entity {number}", area_id=f"area-{number}"
        )
        return entry, device.id, entity.entity_id

    def make_account(self, login: dict = LOGIN) -> ConfigEntry:
        return self.make_entry(
            version=2,
            data=login.copy(),
            options={},
            title="Existing account",
            unique_id=_account_unique_id(login),
        )

    def add_subentry(self, account: ConfigEntry, legacy: ConfigEntry):
        subentry = _build_device_subentry(
            legacy.data[CONF_ADDRESS], legacy.title, legacy.options
        )
        self.hass.config_entries.async_add_subentry(account, subentry)
        return subentry

    def assert_device_moved(
        self, account: ConfigEntry, source: ConfigEntry, device_id: str, entity_id: str
    ) -> ConfigSubentry:
        address = source.data.get(CONF_ADDRESS)
        if address is None:
            address = next(
                subentry.data[CONF_ADDRESS]
                for subentry in account.subentries.values()
                if subentry.data[CONF_DEVICE_ID]
                == self.entities.async_get(entity_id).unique_id.removesuffix("-switch")
            )
        subentry = next(
            item
            for item in account.subentries.values()
            if item.data[CONF_ADDRESS] == address.upper()
        )
        device = self.devices.async_get(device_id)
        entity = self.entities.async_get(entity_id)
        self.assertIsNotNone(device)
        self.assertIsNotNone(entity)
        self.assertEqual(device.config_entries_subentries[account.entry_id], {subentry.subentry_id})
        self.assertEqual(entity.config_entry_id, account.entry_id)
        self.assertEqual(entity.config_subentry_id, subentry.subentry_id)
        self.assertEqual(entity.device_id, device_id)
        self.assertNotIn(source.entry_id, device.config_entries - {account.entry_id})
        return subentry

    async def test_groups_accounts_and_preserves_registry_metadata(self) -> None:
        first, first_device, first_entity = self.make_legacy(1)
        second, second_device, second_entity = self.make_legacy(
            2, {**LOGIN, CONF_USERNAME: LOGIN[CONF_USERNAME].lower()}
        )
        other, other_device, other_entity = self.make_legacy(
            3, {**LOGIN, CONF_ACCESS_ID: "other-account"}
        )
        first_entity_before = self.entities.async_get(first_entity)
        second_entity_before = self.entities.async_get(second_entity)

        with self.assertLogs(tuya_ble.__name__, level="DEBUG") as messages:
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))

        accounts = self.hass.config_entries.async_entries(DOMAIN)
        self.assertEqual({entry.entry_id for entry in accounts}, {first.entry_id, other.entry_id})
        self.assertEqual(first.version, 2)
        self.assertEqual(first.data, LOGIN)
        self.assertEqual(first.options, {})
        self.assertEqual(first.title, LOGIN[CONF_USERNAME])
        self.assertEqual(first.unique_id, _account_unique_id(LOGIN))
        self.assertEqual(len(first.subentries), 2)
        self.assertEqual(len(other.subentries), 1)
        self.assert_device_moved(first, first, first_device, first_entity)
        second_subentry = self.assert_device_moved(first, second, second_device, second_entity)
        self.assert_device_moved(other, other, other_device, other_entity)
        self.assertEqual(
            {key: second_subentry.data[key] for key in CONF_TUYA_DEVICE_KEYS},
            {key: second.options[key] for key in CONF_TUYA_DEVICE_KEYS},
        )
        for number, device_id, entity_id, before in (
            (1, first_device, first_entity, first_entity_before),
            (2, second_device, second_entity, second_entity_before),
        ):
            device = self.devices.async_get(device_id)
            entity = self.entities.async_get(entity_id)
            self.assertEqual(device.name_by_user, f"Custom device {number}")
            self.assertEqual(device.area_id, f"area-{number}")
            self.assertEqual(entity.name, f"Custom entity {number}")
            self.assertEqual(entity.area_id, f"area-{number}")
            self.assertEqual(entity.entity_id, before.entity_id)
            self.assertEqual(entity.id, before.id)
            self.assertEqual(entity.unique_id, before.unique_id)
            self.assertEqual(entity.created_at, before.created_at)
            self.assertEqual(entity.disabled_by, er.RegistryEntryDisabler.USER)
        for secret in ("access-secret", "password-secret", "local-secret-1"):
            self.assertNotIn(secret, "\n".join(messages.output))

        with patch.object(tuya_ble._LOGGER, "debug") as debug:
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
        debug.assert_not_called()
        self.assertEqual(len(first.subentries), 2)
        self.assertEqual(len(self.devices.devices), 3)
        self.assertEqual(len(self.entities.entities), 3)

    async def test_missing_app_type_migrates_with_the_cloud_default(self) -> None:
        account = self.make_account()
        entry, device_id, entity_id = self.make_legacy(
            1, {key: value for key, value in LOGIN.items() if key != CONF_APP_TYPE}
        )

        self.assertTrue(await tuya_ble.async_setup(self.hass, {}))

        self.assertIsNone(self.hass.config_entries.async_get_entry(entry.entry_id))
        self.assertEqual(account.data, LOGIN)
        self.assert_device_moved(account, entry, device_id, entity_id)

    async def test_incomplete_login_stays_legacy_and_loads(self) -> None:
        for number, login in enumerate(
            (
                {key: value for key, value in LOGIN.items() if key != CONF_PASSWORD},
                {**LOGIN, CONF_PASSWORD: None},
                {**LOGIN, CONF_ACCESS_SECRET: ""},
            ),
            start=1,
        ):
            entry, device_id, entity_id = self.make_legacy(number, login)
            data, options, title = dict(entry.data), dict(entry.options), entry.title

            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))
            self.assertTrue(await entry.async_migrate(self.hass))
            self.assertEqual((entry.version, dict(entry.data), dict(entry.options), entry.title),
                             (1, data, options, title))
            self.assertEqual(dict(entry.subentries), {})
            self.assertEqual(self.devices.async_get(device_id).config_entries, {entry.entry_id})
            self.assertEqual(self.entities.async_get(entity_id).config_entry_id, entry.entry_id)

            with (
                patch.object(
                    tuya_ble.bluetooth,
                    "async_ble_device_from_address",
                    return_value=SimpleNamespace(address=data[CONF_ADDRESS].upper()),
                ),
                patch.object(
                    tuya_ble,
                    "_async_setup_device",
                    AsyncMock(return_value=SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))),
                ),
                patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
            ):
                self.assertTrue(await tuya_ble.async_setup_entry(self.hass, entry))
            self.assertIn(None, self.hass.data[DOMAIN][entry.entry_id].devices)

    async def test_disabled_first_entry_does_not_disable_an_active_device(self) -> None:
        disabled, disabled_device, disabled_entity = self.make_legacy(1)
        disabled.disabled_by = ConfigEntryDisabler.USER
        active, active_device, active_entity = self.make_legacy(2)

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(disabled.version, 1)
        self.assertEqual(disabled.disabled_by, ConfigEntryDisabler.USER)
        self.assertIsNotNone(self.hass.config_entries.async_get_entry(disabled.entry_id))
        self.assertEqual(self.devices.async_get(disabled_device).config_entries, {disabled.entry_id})
        self.assertEqual(self.entities.async_get(disabled_entity).config_entry_id, disabled.entry_id)
        self.assertEqual(active.version, 2)
        self.assertIsNone(active.disabled_by)
        self.assert_device_moved(active, active, active_device, active_entity)

        disabled.disabled_by = None
        self.assertTrue(await disabled.async_migrate(self.hass))
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                return_value=SimpleNamespace(address=disabled.data[CONF_ADDRESS].upper()),
            ),
            patch.object(
                tuya_ble,
                "_async_setup_device",
                AsyncMock(return_value=SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))),
            ),
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        ):
            self.assertTrue(await tuya_ble.async_setup_entry(self.hass, disabled))
        self.assertIn(None, self.hass.data[DOMAIN][disabled.entry_id].devices)

        saved_entries = {
            "entries": [
                entry.as_dict() for entry in self.hass.config_entries.async_entries()
            ]
        }
        saved_devices = orjson.loads(orjson.dumps(self.devices._data_to_save()))
        saved_entities = orjson.loads(orjson.dumps(self.entities._data_to_save()))
        await self.reset_hass(saved_entries, saved_devices, saved_entities)
        active = self.hass.config_entries.async_get_entry(active.entry_id)
        await tuya_ble.async_setup(self.hass, {})

        self.assertIsNone(self.hass.config_entries.async_get_entry(disabled.entry_id))
        self.assert_device_moved(active, disabled, disabled_device, disabled_entity)
        self.assert_device_moved(active, active, active_device, active_entity)

    async def test_enabled_legacy_entry_waits_for_a_disabled_account(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        account.disabled_by = ConfigEntryDisabler.USER

        with self.assertLogs(tuya_ble.__name__, level="WARNING"):
            await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(legacy.version, 1)
        self.assertTrue(await legacy.async_migrate(self.hass))
        self.assertEqual(dict(account.subentries), {})
        self.assertEqual(self.devices.async_get(device_id).config_entries, {legacy.entry_id})
        self.assertEqual(self.entities.async_get(entity_id).config_entry_id, legacy.entry_id)

        account.disabled_by = None
        await tuya_ble.async_setup(self.hass, {})
        self.assertIsNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_reuses_existing_account_and_subentry_without_overwriting_credentials(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account({**LOGIN, CONF_USERNAME: LOGIN[CONF_USERNAME].lower()})
        subentry = self.add_subentry(account, legacy)
        self.hass.config_entries.async_update_subentry(
            account, subentry, data={**subentry.data, CONF_LOCAL_KEY: "existing-secret"}
        )

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(self.hass.config_entries.async_entries(DOMAIN), [account])
        self.assertEqual(list(account.subentries), [subentry.subentry_id])
        self.assertEqual(subentry.data[CONF_LOCAL_KEY], "existing-secret")
        self.assertEqual(account.title, "Existing account")
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_recovers_missing_credentials_in_an_existing_subentry(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        subentry = self.add_subentry(account, legacy)
        self.hass.config_entries.async_update_subentry(
            account, subentry, data={**subentry.data, CONF_LOCAL_KEY: None}
        )

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(subentry.data[CONF_LOCAL_KEY], "local-secret-1")
        self.assertEqual(list(account.subentries), [subentry.subentry_id])
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_staged_subentry_is_reused_when_an_account_exists(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        staged = self.add_subentry(legacy, legacy)
        account = self.make_account()

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(list(account.subentries), [staged.subentry_id])
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_existing_account_does_not_claim_loginless_legacy_device(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1, {})
        account = self.make_account()
        self.add_subentry(account, legacy)

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(legacy.version, 1)
        self.assertIsNotNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assertEqual(self.devices.async_get(device_id).config_entries, {legacy.entry_id})
        self.assertEqual(self.entities.async_get(entity_id).config_entry_id, legacy.entry_id)

    async def test_other_integrations_keep_shared_device_and_entities(self) -> None:
        account, _, _ = self.make_legacy(1)
        legacy, device_id, entity_id = self.make_legacy(2)
        other = ConfigEntry(
            version=1,
            minor_version=1,
            domain="other",
            title="Other integration",
            data={},
            options={},
            source="user",
            unique_id="other",
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        self.hass.config_entries._entries[other.entry_id] = other
        self.devices.async_update_device(device_id, add_config_entry_id=other.entry_id)
        other_entity = self.entities.async_get_or_create(
            "sensor",
            "other",
            "shared-device-sensor",
            config_entry=other,
            device_id=device_id,
        )

        await tuya_ble.async_setup(self.hass, {})

        self.assert_device_moved(account, legacy, device_id, entity_id)
        self.assertIn(other.entry_id, self.devices.async_get(device_id).config_entries)
        self.assertEqual(
            self.entities.async_get(other_entity.entity_id).config_entry_id, other.entry_id
        )

    async def test_interruption_before_conversion_reuses_staged_subentry(self) -> None:
        entry, device_id, entity_id = self.make_legacy(1)
        with patch.object(
            self.hass.config_entries,
            "async_update_entry",
            side_effect=InterruptedMigration,
        ):
            with self.assertRaises(InterruptedMigration):
                await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(entry.version, 1)
        self.assertEqual(entry.options[CONF_LOCAL_KEY], "local-secret-1")
        staged = next(iter(entry.subentries.values()))
        self.assertEqual(staged.data[CONF_LOCAL_KEY], "local-secret-1")
        self.assertIsNotNone(self.entities.async_get(entity_id))

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(list(entry.subentries), [staged.subentry_id])
        self.assert_device_moved(entry, entry, device_id, entity_id)

    async def test_interruption_after_conversion_reconciles_without_a_v1_entry(self) -> None:
        entry, device_id, entity_id = self.make_legacy(1)
        orphan = self.entities.async_get_or_create(
            "sensor",
            DOMAIN,
            "device-1-orphan",
            config_entry=entry,
            suggested_object_id="orphan_1",
        )
        update_entry = self.hass.config_entries.async_update_entry

        def interrupt_after_conversion(entry, **kwargs):
            update_entry(entry, **kwargs)
            raise InterruptedMigration

        with patch.object(
            self.hass.config_entries,
            "async_update_entry",
            side_effect=interrupt_after_conversion,
        ):
            with self.assertRaises(InterruptedMigration):
                await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(entry.version, 2)
        self.assertEqual(entry.options, {})
        staged = next(iter(entry.subentries.values()))
        self.assertEqual(staged.data[CONF_LOCAL_KEY], "local-secret-1")
        self.assertEqual(self.devices.async_get(device_id).config_entries_subentries[entry.entry_id], {None})
        self.assertIsNotNone(self.entities.async_get(entity_id))

        await tuya_ble.async_setup(self.hass, {})

        self.assert_device_moved(entry, entry, device_id, entity_id)
        orphan = self.entities.async_get(orphan.entity_id)
        self.assertEqual(orphan.config_subentry_id, staged.subentry_id)
        with patch.object(tuya_ble._LOGGER, "debug") as debug:
            await tuya_ble.async_setup(self.hass, {})
        debug.assert_not_called()

    async def test_recovers_registry_owners_after_old_entry_was_persistently_removed(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        subentry = self.add_subentry(account, legacy)
        # Config entries can be saved before the independently persisted registries.
        old_devices = orjson.loads(orjson.dumps(self.devices._data_to_save()))
        old_entities = orjson.loads(orjson.dumps(self.entities._data_to_save()))
        await tuya_ble.async_setup(self.hass, {})
        saved_entries = {
            "entries": [
                entry.as_dict() for entry in self.hass.config_entries.async_entries()
            ]
        }
        account_id = account.entry_id
        await self.reset_hass(saved_entries, old_devices, old_entities)
        account = self.hass.config_entries.async_get_entry(account_id)

        await tuya_ble.async_setup(self.hass, {})

        self.assertEqual(list(account.subentries), [subentry.subentry_id])
        self.assert_device_moved(account, legacy, device_id, entity_id)
        device = self.devices.async_get(device_id)
        entity = self.entities.async_get(entity_id)
        self.assertEqual(device.name_by_user, "Custom device 1")
        self.assertEqual(device.area_id, "area-1")
        self.assertEqual(entity.name, "Custom entity 1")
        self.assertEqual(entity.area_id, "area-1")

    async def test_recovers_missing_subentry_ids_after_uneven_store_saves(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        old_subentry = self.add_subentry(account, legacy)
        self.devices.async_update_device(
            device_id,
            add_config_entry_id=account.entry_id,
            add_config_subentry_id=old_subentry.subentry_id,
        )
        self.entities.async_update_entity(
            entity_id,
            config_entry_id=account.entry_id,
            config_subentry_id=old_subentry.subentry_id,
        )
        self.devices.async_update_device(device_id, remove_config_entry_id=legacy.entry_id)
        del self.hass.config_entries._entries[legacy.entry_id]
        object.__setattr__(account, "subentries", MappingProxyType({}))
        replacement = self.add_subentry(account, legacy)

        await tuya_ble.async_setup(self.hass, {})

        self.assertNotEqual(old_subentry.subentry_id, replacement.subentry_id)
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_recovers_orphan_entity_when_device_store_was_saved_first(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        subentry = self.add_subentry(account, legacy)
        device = self.devices.async_get(device_id)
        # Emulate a newer device-registry snapshot with the older entity snapshot.
        self.devices.devices[device_id] = attr.evolve(
            device,
            config_entries={account.entry_id},
            config_entries_subentries={account.entry_id: {subentry.subentry_id}},
        )
        del self.hass.config_entries._entries[legacy.entry_id]

        await tuya_ble.async_setup(self.hass, {})

        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_interruption_between_legacy_add_and_remove_keeps_entities(self) -> None:
        account, _, _ = self.make_legacy(1)
        legacy, device_id, entity_id = self.make_legacy(2)
        update_device = self.devices.async_update_device

        def interrupt_after_add(device_id_arg, **kwargs):
            result = update_device(device_id_arg, **kwargs)
            if device_id_arg == device_id and "add_config_entry_id" in kwargs:
                raise InterruptedMigration
            return result

        with patch.object(self.devices, "async_update_device", side_effect=interrupt_after_add):
            with self.assertRaises(InterruptedMigration):
                await tuya_ble.async_setup(self.hass, {})

        device = self.devices.async_get(device_id)
        self.assertEqual(device.config_entries, {account.entry_id, legacy.entry_id})
        self.assertIsNotNone(self.entities.async_get(entity_id))
        self.assertIsNotNone(self.hass.config_entries.async_get_entry(legacy.entry_id))

        await tuya_ble.async_setup(self.hass, {})

        self.assertIsNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assert_device_moved(account, legacy, device_id, entity_id)
        self.assertEqual(len(account.subentries), 2)

    async def test_interruption_after_moves_before_removal_is_idempotent(self) -> None:
        account, _, _ = self.make_legacy(1)
        legacy, device_id, entity_id = self.make_legacy(2)
        with patch.object(
            self.hass.config_entries, "async_remove", AsyncMock(side_effect=InterruptedMigration)
        ):
            with self.assertRaises(InterruptedMigration):
                await tuya_ble.async_setup(self.hass, {})

        self.assert_device_moved(account, legacy, device_id, entity_id)
        self.assertIsNotNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        await tuya_ble.async_setup(self.hass, {})
        self.assertIsNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assert_device_moved(account, legacy, device_id, entity_id)
        self.assertEqual(len(account.subentries), 2)

    async def test_new_registry_api_moves_entities_before_single_owner_device(self) -> None:
        account, _, _ = self.make_legacy(1)
        legacy, device_id, entity_id = self.make_legacy(2)
        old_update = self.devices.async_update_device
        moves = []

        def new_update(
            device_id_arg, *, new_config_entry_id, new_config_subentry_id
        ):
            device = self.devices.async_get(device_id_arg)
            old_entry_id = next(iter(device.config_entries))
            for entity in er.async_entries_for_device(
                self.entities, device_id_arg, include_disabled_entities=True
            ):
                self.assertEqual(entity.config_entry_id, new_config_entry_id)
                self.assertEqual(entity.config_subentry_id, new_config_subentry_id)
            old_update(
                device_id_arg,
                add_config_entry_id=new_config_entry_id,
                add_config_subentry_id=new_config_subentry_id,
            )
            if old_entry_id == new_config_entry_id:
                result = old_update(
                    device_id_arg,
                    remove_config_entry_id=old_entry_id,
                    remove_config_subentry_id=None,
                )
            else:
                result = old_update(device_id_arg, remove_config_entry_id=old_entry_id)
            moves.append((device_id_arg, new_config_entry_id, new_config_subentry_id))
            return result

        with patch.object(self.devices, "async_update_device", new_update):
            await tuya_ble.async_setup(self.hass, {})
            self.assertEqual(len(moves), 2)
            await tuya_ble.async_setup(self.hass, {})
            self.assertEqual(len(moves), 2)

        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_new_registry_collision_defers_group_and_retries(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        original = self.devices.async_get(device_id)
        duplicate = attr.evolve(
            original,
            id="existing-account-device",
            config_entries={account.entry_id},
            config_entries_subentries={account.entry_id: {None}},
            name_by_user="Existing account device",
            area_id="existing-area",
        )
        self.devices.devices[duplicate.id] = duplicate
        account_before = account.as_dict()
        legacy_before = legacy.as_dict()
        entity_before = self.entities.async_get(entity_id)
        old_update = self.devices.async_update_device

        def find_devices(*, identifiers=None, connections=None, config_entry_id=None):
            return [
                device
                for device in self.devices.devices.values()
                if (config_entry_id is None or config_entry_id in device.config_entries)
                and (
                    bool(device.identifiers & (identifiers or set()))
                    or bool(device.connections & (connections or set()))
                )
            ]

        def new_update(device_id_arg, *, new_config_entry_id, new_config_subentry_id):
            device = self.devices.async_get(device_id_arg)
            source_id = next(iter(device.config_entries))
            old_update(
                device_id_arg,
                add_config_entry_id=new_config_entry_id,
                add_config_subentry_id=new_config_subentry_id,
            )
            return old_update(device_id_arg, remove_config_entry_id=source_id)

        with (
            patch.object(self.devices, "async_get_devices", find_devices, create=True),
            patch.object(self.devices, "async_update_device", new_update),
        ):
            with self.assertLogs(tuya_ble.__name__, level="ERROR") as messages:
                self.assertTrue(await tuya_ble.async_setup(self.hass, {}))

            self.assertEqual(account.as_dict(), account_before)
            self.assertEqual(legacy.as_dict(), legacy_before)
            self.assertEqual(self.devices.async_get(device_id), original)
            self.assertEqual(self.devices.async_get(duplicate.id), duplicate)
            self.assertEqual(self.entities.async_get(entity_id), entity_before)
            self.assertTrue(await tuya_ble.async_migrate_entry(self.hass, legacy))
            self.assertIn(device_id, "\n".join(messages.output))
            self.assertIn(duplicate.id, "\n".join(messages.output))
            self.assertNotIn("local-secret-1", "\n".join(messages.output))

            self.devices.async_remove_device(duplicate.id)
            saved_entries = {
                "entries": [
                    entry.as_dict() for entry in self.hass.config_entries.async_entries()
                ]
            }
            saved_devices = orjson.loads(orjson.dumps(self.devices._data_to_save()))
            saved_entities = orjson.loads(orjson.dumps(self.entities._data_to_save()))

        await self.reset_hass(saved_entries, saved_devices, saved_entities)
        account = self.hass.config_entries.async_get_entry(account.entry_id)
        old_update = self.devices.async_update_device
        with (
            patch.object(self.devices, "async_get_devices", find_devices, create=True),
            patch.object(self.devices, "async_update_device", new_update),
        ):
            await tuya_ble.async_setup(self.hass, {})

        self.assertIsNone(self.hass.config_entries.async_get_entry(legacy.entry_id))
        self.assert_device_moved(account, legacy, device_id, entity_id)

    async def test_collision_between_sources_does_not_change_group(self) -> None:
        first, first_device_id, first_entity_id = self.make_legacy(1)
        second, second_device_id, second_entity_id = self.make_legacy(2)
        other, other_device_id, other_entity_id = self.make_legacy(
            3, {**LOGIN, CONF_ACCESS_ID: "other-account"}
        )
        first_device = self.devices.async_get(first_device_id)
        second_device = attr.evolve(
            self.devices.async_get(second_device_id),
            identifiers=first_device.identifiers,
            connections=first_device.connections,
        )
        self.devices.devices[second_device_id] = second_device
        first_before, second_before = first.as_dict(), second.as_dict()

        with self.assertLogs(tuya_ble.__name__, level="ERROR"):
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))

        self.assertEqual(first.as_dict(), first_before)
        self.assertEqual(second.as_dict(), second_before)
        self.assertEqual(self.devices.async_get(first_device_id), first_device)
        self.assertEqual(self.devices.async_get(second_device_id), second_device)
        self.assertEqual(self.entities.async_get(first_entity_id).config_entry_id, first.entry_id)
        self.assertEqual(self.entities.async_get(second_entity_id).config_entry_id, second.entry_id)
        self.assertEqual(other.version, 2)
        self.assert_device_moved(other, other, other_device_id, other_entity_id)

    async def test_collision_with_orphan_is_not_reconciled(self) -> None:
        legacy, device_id, entity_id = self.make_legacy(1)
        account = self.make_account()
        subentry = self.add_subentry(account, legacy)
        original = self.devices.async_get(device_id)
        duplicate = attr.evolve(
            original,
            id="existing-account-device",
            config_entries={account.entry_id},
            config_entries_subentries={account.entry_id: {subentry.subentry_id}},
        )
        self.devices.devices[duplicate.id] = duplicate
        del self.hass.config_entries._entries[legacy.entry_id]
        entity_before = self.entities.async_get(entity_id)

        def find_devices(*, identifiers=None, connections=None, config_entry_id=None):
            return [
                device
                for device in self.devices.devices.values()
                if (config_entry_id is None or config_entry_id in device.config_entries)
                and (
                    bool(device.identifiers & (identifiers or set()))
                    or bool(device.connections & (connections or set()))
                )
            ]

        def new_update(device_id_arg, *, new_config_entry_id, new_config_subentry_id):
            self.fail("Conflicting orphan records must not be changed")

        with (
            patch.object(self.devices, "async_get_devices", find_devices, create=True),
            patch.object(self.devices, "async_update_device", new_update),
            self.assertLogs(tuya_ble.__name__, level="ERROR"),
        ):
            self.assertTrue(await tuya_ble.async_setup(self.hass, {}))

        self.assertEqual(self.devices.async_get(device_id), original)
        self.assertEqual(self.devices.async_get(duplicate.id), duplicate)
        self.assertEqual(self.entities.async_get(entity_id), entity_before)
