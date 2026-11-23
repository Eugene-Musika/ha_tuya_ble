"""Focused tests for account runtime data and device credentials."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, call, patch

from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)

from custom_components.tuya_ble import cloud
from custom_components.tuya_ble.cloud import (
    HASSTuyaBLEDeviceManager,
    TuyaBLEDeviceManagerView,
    TuyaCloudCacheItem,
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
from custom_components.tuya_ble.devices import (
    TuyaBLEAccountData,
    TuyaBLEData,
    async_add_device_entities,
)


LOGIN = {
    CONF_ENDPOINT: "https://example.invalid",
    CONF_ACCESS_ID: "access",
    CONF_ACCESS_SECRET: "secret",
    CONF_AUTH_TYPE: 0,
    CONF_USERNAME: "user",
    CONF_PASSWORD: "password",
    CONF_COUNTRY_CODE: "1",
    CONF_APP_TYPE: "",
}

CREDENTIALS = {
    CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
    CONF_UUID: "uuid",
    CONF_LOCAL_KEY: "local-key",
    CONF_DEVICE_ID: "device-id",
    CONF_CATEGORY: "tdq",
    CONF_PRODUCT_ID: "product",
    CONF_DEVICE_NAME: None,
    CONF_PRODUCT_MODEL: None,
    CONF_PRODUCT_NAME: None,
}


class AccountEntityTests(TestCase):
    def test_entities_are_associated_with_each_subentry(self) -> None:
        legacy = Mock(spec=TuyaBLEData)
        relay = Mock(spec=TuyaBLEData)
        lock = Mock(spec=TuyaBLEData)
        account = TuyaBLEAccountData(
            Mock(spec=HASSTuyaBLEDeviceManager),
            {None: legacy, "relay": relay, "lock": lock},
        )
        hass = SimpleNamespace(data={DOMAIN: {"entry": account}})
        entry = SimpleNamespace(entry_id="entry")
        add_entities = Mock()

        async_add_device_entities(hass, entry, add_entities, lambda data: [data])

        self.assertEqual(
            add_entities.call_args_list,
            [
                call([legacy]),
                call([relay], config_subentry_id="relay"),
                call([lock], config_subentry_id="lock"),
            ],
        )


class DeviceManagerViewTests(IsolatedAsyncioTestCase):
    async def test_saved_credentials_work_offline(self) -> None:
        manager = Mock(spec=HASSTuyaBLEDeviceManager)
        view = TuyaBLEDeviceManagerView(manager, CREDENTIALS)

        credentials = await view.get_device_credentials(CREDENTIALS[CONF_ADDRESS])

        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.device_id, CREDENTIALS[CONF_DEVICE_ID])
        self.assertIsNone(credentials.device_name)
        manager.get_device_credentials.assert_not_awaited()

    async def test_forced_refresh_uses_account_manager(self) -> None:
        manager = Mock(spec=HASSTuyaBLEDeviceManager)
        manager.get_device_credentials.return_value = object()
        view = TuyaBLEDeviceManagerView(manager, CREDENTIALS)

        result = await view.get_device_credentials(
            CREDENTIALS[CONF_ADDRESS], force_update=True, save_data=True, uuid="advertised"
        )

        self.assertIs(result, manager.get_device_credentials.return_value)
        manager.get_device_credentials.assert_awaited_once_with(
            CREDENTIALS[CONF_ADDRESS],
            force_update=True,
            save_data=True,
            uuid="advertised",
        )

    async def test_missing_saved_credentials_do_not_trigger_cloud_lookup(self) -> None:
        manager = Mock(spec=HASSTuyaBLEDeviceManager)
        view = TuyaBLEDeviceManagerView(manager, {})

        with self.assertLogs(cloud.__name__, level="ERROR"):
            result = await view.get_device_credentials(CREDENTIALS[CONF_ADDRESS])

        self.assertIsNone(result)
        manager.get_device_credentials.assert_not_awaited()


class AccountCloudTests(IsolatedAsyncioTestCase):
    async def test_cache_uses_v1_options_and_v2_account_data(self) -> None:
        old_login = {**LOGIN, CONF_USERNAME: "old"}
        new_login = {**LOGIN, CONF_USERNAME: "new"}
        entries = [
            SimpleNamespace(version=1, options=old_login, data={}),
            SimpleNamespace(version=2, options={}, data=new_login),
        ]
        config_entries = SimpleNamespace(
            async_entries=lambda domain: entries if domain == DOMAIN else []
        )
        manager = HASSTuyaBLEDeviceManager(SimpleNamespace(config_entries=config_entries), {})

        async def login(data, add_to_cache):
            cloud._cache[manager._get_cache_key(data)] = TuyaCloudCacheItem(
                None, dict(data), {}
            )
            return {"success": True}

        async def fill(item):
            item.credentials["address"] = CREDENTIALS
            return True

        manager._login = AsyncMock(side_effect=login)
        manager._fill_cache_item = AsyncMock(side_effect=fill)
        with patch.dict(cloud._cache, clear=True):
            await manager.build_cache()

        self.assertEqual(
            [call.args[0][CONF_USERNAME] for call in manager._login.await_args_list],
            ["old", "new"],
        )

    async def test_successful_refresh_replaces_stale_devices(self) -> None:
        api = SimpleNamespace(token_info=SimpleNamespace(uid="user"), get=Mock())
        hass = SimpleNamespace(
            async_add_executor_job=AsyncMock(
                side_effect=[
                    {"success": True, "result": [{"id": "new", "uuid": "uuid"}]},
                    {"success": True, "result": [{"mac": "AA:BB:CC:DD:EE:FF"}]},
                ]
            )
        )
        manager = HASSTuyaBLEDeviceManager(hass, LOGIN.copy())
        item = TuyaCloudCacheItem(api, LOGIN, {"stale": CREDENTIALS})

        self.assertTrue(await manager._fill_cache_item(item))
        self.assertEqual(set(item.credentials), {"AA:BB:CC:DD:EE:FF"})

    async def test_failed_refresh_does_not_return_stale_credentials(self) -> None:
        manager = HASSTuyaBLEDeviceManager(SimpleNamespace(), LOGIN.copy())
        item = TuyaCloudCacheItem(None, LOGIN, {CREDENTIALS[CONF_ADDRESS]: CREDENTIALS})
        manager.login = AsyncMock(return_value={"success": True})
        manager._fill_cache_item = AsyncMock(return_value=False)

        with patch.dict(cloud._cache, {manager._get_cache_key(LOGIN): item}, clear=True):
            result = await manager.get_device_credentials(
                CREDENTIALS[CONF_ADDRESS], force_update=True
            )

        self.assertIsNone(result)
