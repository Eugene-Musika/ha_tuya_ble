"""Tests for account and device configuration flows."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol

from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)

from custom_components.tuya_ble import config_flow
from custom_components.tuya_ble.cloud import HASSTuyaBLEDeviceManager
from custom_components.tuya_ble.config_flow import (
    DeviceSubentryFlow,
    TuyaBLEConfigFlow,
    TuyaBLEOptionsFlow,
    _account_unique_id,
    _address_is_configured,
    _build_device_subentry,
)
from custom_components.tuya_ble.const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_CATEGORY,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_UUID,
    DOMAIN,
    TUYA_COUNTRIES,
)
from custom_components.tuya_ble.tuya_ble import SERVICE_UUID, TuyaBLEDeviceCredentials


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
USER_INPUT = {
    CONF_COUNTRY_CODE: TUYA_COUNTRIES[0].name,
    CONF_ACCESS_ID: "access",
    CONF_ACCESS_SECRET: "secret",
    CONF_USERNAME: "user",
    CONF_PASSWORD: "new-password",
}
ADDRESS = "AA:BB:CC:DD:EE:01"
CREDENTIALS = TuyaBLEDeviceCredentials(
    uuid="uuid",
    local_key="local-key",
    device_id="device-id",
    category="tdq",
    product_id="product",
    device_name="Relay",
    product_model=None,
    product_name=None,
)


class FlowHelpersTests(TestCase):
    def test_device_addresses_are_checked_across_legacy_and_account_entries(self) -> None:
        entries = [
            SimpleNamespace(
                version=1, data={CONF_ADDRESS: "aa:bb:cc:dd:ee:01"}, subentries={}
            ),
            SimpleNamespace(
                version=2,
                data={},
                subentries={
                    "device": SimpleNamespace(data={CONF_ADDRESS: "AA:BB:CC:DD:EE:02"})
                },
            ),
        ]
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: entries)
        )

        self.assertTrue(_address_is_configured(hass, "AA:BB:CC:DD:EE:01"))
        self.assertTrue(_address_is_configured(hass, "aa:bb:cc:dd:ee:02"))
        self.assertFalse(_address_is_configured(hass, "AA:BB:CC:DD:EE:03"))

    def test_subentry_contains_device_credentials_not_login(self) -> None:
        credentials = {
            CONF_UUID: "uuid",
            CONF_LOCAL_KEY: "local-key",
            CONF_DEVICE_ID: "device-id",
            CONF_CATEGORY: "tdq",
            CONF_PRODUCT_ID: "product",
            CONF_USERNAME: "not-device-data",
        }

        subentry = _build_device_subentry("aa:bb:cc:dd:ee:01", "relay", credentials)

        self.assertEqual(subentry.unique_id, "AA:BB:CC:DD:EE:01")
        self.assertEqual(subentry.data[CONF_ADDRESS], subentry.unique_id)
        self.assertEqual(subentry.data[CONF_DEVICE_ID], "device-id")
        self.assertNotIn(CONF_USERNAME, subentry.data)

    def test_account_identity_ignores_username_case(self) -> None:
        login = {
            CONF_ENDPOINT: "https://example.invalid",
            CONF_ACCESS_ID: "access",
            CONF_USERNAME: "User@Example.com",
        }

        self.assertEqual(
            _account_unique_id(login),
            _account_unique_id({**login, CONF_USERNAME: "user@example.com"}),
        )
        self.assertNotEqual(
            _account_unique_id(login),
            _account_unique_id({**login, CONF_ACCESS_ID: "another"}),
        )


class AccountFlowTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entries = []
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: self.entries if domain == DOMAIN else [],
            async_entry_for_domain_unique_id=lambda domain, unique_id: next(
                (entry for entry in self.entries if entry.unique_id == unique_id), None
            ),
            async_get_entry=lambda entry_id: next(
                (entry for entry in self.entries if entry.entry_id == entry_id), None
            ),
            async_get_known_entry=lambda entry_id: next(
                entry for entry in self.entries if entry.entry_id == entry_id
            ),
            async_add_subentry=Mock(side_effect=self._add_subentry),
            async_update_subentry=Mock(),
            async_update_entry=Mock(),
            async_reload=AsyncMock(return_value=True),
            flow=SimpleNamespace(async_progress_by_handler=Mock(return_value=[])),
        )
        self.hass = SimpleNamespace(
            data={},
            config=SimpleNamespace(country="US"),
            config_entries=self.config_entries,
        )
        self.manager = HASSTuyaBLEDeviceManager(self.hass, {})
        self.manager.build_cache = AsyncMock()
        self.manager.get_device_credentials = AsyncMock(return_value=CREDENTIALS)
        self.manager._login = AsyncMock(return_value={"success": True})
        self.discovery = SimpleNamespace(
            address=ADDRESS,
            service_data={SERVICE_UUID: b""},
        )
        patches = (
            patch.object(config_flow, "HASSTuyaBLEDeviceManager", return_value=self.manager),
            patch.object(config_flow, "get_advertised_uuid", return_value="uuid"),
            patch.object(
                config_flow, "get_device_readable_name", AsyncMock(return_value="Relay")
            ),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def _add_subentry(entry, subentry) -> None:
        entry.subentries[subentry.subentry_id] = subentry

    def account(self, subentries=None):
        entry = SimpleNamespace(
            entry_id="account",
            version=2,
            data=dict(LOGIN),
            options={},
            subentries=subentries or {},
            title=LOGIN[CONF_USERNAME],
            unique_id=_account_unique_id(LOGIN),
        )
        self.entries.append(entry)
        return entry

    def flow(self, source="user"):
        flow = TuyaBLEConfigFlow()
        flow.hass = self.hass
        flow.handler = DOMAIN
        flow.flow_id = "flow"
        flow.context = {"source": source}
        return flow

    def subentry_flow(self):
        flow = DeviceSubentryFlow()
        flow.hass = self.hass
        flow.handler = ("account", "device")
        flow.flow_id = "flow"
        flow.context = {"source": "user"}
        return flow

    def options_flow(self, entry):
        flow = TuyaBLEOptionsFlow()
        flow.hass = self.hass
        flow.handler = entry.entry_id
        flow.flow_id = "flow"
        flow.context = {"source": "user"}
        return flow

    async def test_discovery_confirms_and_adds_device_without_login(self) -> None:
        account = self.account()
        flow = self.flow("bluetooth")

        result = await flow.async_step_bluetooth(self.discovery)

        self.assertEqual(result["step_id"], "discovery_confirm")
        self.assertEqual(result["description_placeholders"], {"name": "Relay"})
        self.assertTrue(flow.context["confirm_only"])
        self.manager.build_cache.assert_not_awaited()

        result = await flow.async_step_discovery_confirm({})

        self.assertEqual(result["reason"], "device_added")
        subentry = next(iter(account.subentries.values()))
        self.assertEqual(subentry.data[CONF_ADDRESS], ADDRESS)
        self.assertEqual(subentry.data[CONF_LOCAL_KEY], CREDENTIALS.local_key)
        self.assertEqual(len(self.entries), 1)

        result = await self.flow("bluetooth").async_step_bluetooth(self.discovery)
        self.assertEqual(result["reason"], "already_configured")

    async def test_discovery_falls_back_to_login_then_reuses_account(self) -> None:
        account = self.account()
        self.manager.get_device_credentials.side_effect = [None, None, CREDENTIALS]
        flow = self.flow("bluetooth")

        result = await flow.async_step_bluetooth(self.discovery)
        self.assertEqual(result["step_id"], "login")
        with patch.object(config_flow, "_try_login", AsyncMock(return_value=dict(LOGIN))):
            result = await flow.async_step_login(dict(USER_INPUT))

        self.assertEqual(result["reason"], "device_added")
        self.assertEqual(len(account.subentries), 1)
        self.assertEqual(len(self.entries), 1)

    async def test_discovery_refreshes_stale_cloud_device_list(self) -> None:
        self.account()
        self.manager.get_device_credentials.side_effect = [None, CREDENTIALS]

        result = await self.flow("bluetooth").async_step_bluetooth(self.discovery)

        self.assertEqual(result["step_id"], "discovery_confirm")
        self.assertTrue(
            self.manager.get_device_credentials.await_args_list[1].kwargs["force_update"]
        )

    async def test_discovery_adds_device_to_matching_account(self) -> None:
        first = self.account()
        first.entry_id = "first"
        second = self.account()
        second.entry_id = "second"
        second.unique_id = "second-account"
        other_manager = SimpleNamespace(
            get_device_credentials=AsyncMock(return_value=None)
        )
        flow = self.flow("bluetooth")

        with patch.object(
            config_flow,
            "HASSTuyaBLEDeviceManager",
            side_effect=[other_manager, self.manager],
        ):
            result = await flow.async_step_bluetooth(self.discovery)
            self.assertEqual(result["step_id"], "discovery_confirm")
            result = await flow.async_step_discovery_confirm({})

        self.assertEqual(result["reason"], "device_added")
        self.assertFalse(first.subentries)
        self.assertEqual(len(second.subentries), 1)

    async def test_first_discovered_device_creates_account_and_subentry(self) -> None:
        flow = self.flow("bluetooth")
        result = await flow.async_step_bluetooth(self.discovery)
        self.assertEqual(result["step_id"], "login")

        with patch.object(config_flow, "_try_login", AsyncMock(return_value=dict(LOGIN))):
            result = await flow.async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], LOGIN[CONF_USERNAME])
        self.assertEqual(result["data"], LOGIN)
        self.assertEqual(len(result["subentries"]), 1)
        self.assertEqual(result["subentries"][0]["data"][CONF_ADDRESS], ADDRESS)

    async def test_user_login_reuses_account_and_adds_selected_device(self) -> None:
        account = self.account()
        flow = self.flow()
        with (
            patch.object(config_flow, "_try_login", AsyncMock(return_value=dict(LOGIN))),
            patch.object(
                config_flow, "async_discovered_service_info", return_value=[self.discovery]
            ),
        ):
            result = await flow.async_step_user()
            self.assertEqual(result["step_id"], "login")
            result = await flow.async_step_login(dict(USER_INPUT))
            self.assertEqual(result["step_id"], "device")
            result = await flow.async_step_device({CONF_ADDRESS: ADDRESS.lower()})

        self.assertEqual(result["reason"], "device_added")
        self.assertEqual(len(self.entries), 1)
        self.assertEqual(len(account.subentries), 1)

    async def test_user_can_create_account_with_no_devices(self) -> None:
        flow = self.flow()
        with (
            patch.object(config_flow, "_try_login", AsyncMock(return_value=dict(LOGIN))),
            patch.object(config_flow, "async_discovered_service_info", return_value=[]),
        ):
            result = await flow.async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], LOGIN)
        self.assertFalse(result["subentries"])

    async def test_subentry_flow_filters_configured_devices_and_retries(self) -> None:
        account = self.account()
        configured = SimpleNamespace(
            version=1,
            data={CONF_ADDRESS: "AA:BB:CC:DD:EE:02"},
            unique_id="AA:BB:CC:DD:EE:02",
        )
        self.entries.append(configured)
        configured_discovery = SimpleNamespace(
            address=configured.unique_id, service_data={SERVICE_UUID: b""}
        )
        flow = self.subentry_flow()
        self.manager.get_device_credentials.side_effect = [None, None, CREDENTIALS]

        with patch.object(
            config_flow,
            "async_discovered_service_info",
            return_value=[configured_discovery, self.discovery],
        ):
            result = await flow.async_step_user()
            with self.assertRaises(vol.Invalid):
                result["data_schema"]({CONF_ADDRESS: configured.unique_id})
            result = await flow.async_step_user({CONF_ADDRESS: ADDRESS})
            self.assertEqual(result["errors"]["base"], "device_not_registered")
            result = await flow.async_step_user({CONF_ADDRESS: ADDRESS})

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["unique_id"], ADDRESS)
        self.assertEqual(result["data"][CONF_DEVICE_ID], CREDENTIALS.device_id)
        self.assertTrue(self.manager.get_device_credentials.await_args.kwargs["force_update"])
        self.assertFalse(account.subentries)

    async def test_subentry_flow_aborts_when_no_devices_are_available(self) -> None:
        self.account()
        with patch.object(config_flow, "async_discovered_service_info", return_value=[]):
            result = await self.subentry_flow().async_step_user()
        self.assertEqual(result["reason"], "no_unconfigured_devices")

    async def test_subentry_flow_rechecks_duplicates_after_lookup(self) -> None:
        account = self.account()

        async def name_after_other_flow_adds_device(*args):
            subentry = _build_device_subentry(ADDRESS, "Relay", CREDENTIALS.__dict__)
            self._add_subentry(account, subentry)
            return "Relay"

        with (
            patch.object(
                config_flow, "async_discovered_service_info", return_value=[self.discovery]
            ),
            patch.object(
                config_flow, "get_device_readable_name", name_after_other_flow_adds_device
            ),
        ):
            result = await self.subentry_flow().async_step_user({CONF_ADDRESS: ADDRESS})

        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(len(account.subentries), 1)

    async def test_failed_options_login_keeps_account_and_devices_unchanged(self) -> None:
        subentry = _build_device_subentry(ADDRESS, "Relay", CREDENTIALS.__dict__)
        account = self.account({subentry.subentry_id: subentry})
        self.manager._login.return_value = {
            "success": False,
            "code": 1106,
            "msg": "Login failed",
        }

        result = await self.options_flow(account).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["errors"]["base"], "login_error")
        self.assertEqual(account.data, LOGIN)
        self.assertEqual(subentry.data[CONF_LOCAL_KEY], CREDENTIALS.local_key)
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_update_subentry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()

    async def test_options_refreshes_all_devices_before_updating_login(self) -> None:
        relay = _build_device_subentry(ADDRESS, "Relay", CREDENTIALS.__dict__)
        lock = _build_device_subentry(
            "AA:BB:CC:DD:EE:02", "Lock", CREDENTIALS.__dict__
        )
        account = self.account(
            {relay.subentry_id: relay, lock.subentry_id: lock}
        )

        result = await self.options_flow(account).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.config_entries.async_update_subentry.call_count, 2)
        self.assertTrue(
            self.manager.get_device_credentials.await_args_list[0].kwargs["force_update"]
        )
        self.assertFalse(
            self.manager.get_device_credentials.await_args_list[1].kwargs["force_update"]
        )
        self.assertEqual(
            self.config_entries.async_update_entry.call_args.kwargs["data"][CONF_PASSWORD],
            USER_INPUT[CONF_PASSWORD],
        )
        self.config_entries.async_reload.assert_awaited_once_with(account.entry_id)

    async def test_options_keeps_old_login_when_device_refresh_fails(self) -> None:
        relay = _build_device_subentry(ADDRESS, "Relay", CREDENTIALS.__dict__)
        lock = _build_device_subentry(
            "AA:BB:CC:DD:EE:02", "Lock", CREDENTIALS.__dict__
        )
        account = self.account({relay.subentry_id: relay, lock.subentry_id: lock})
        self.manager.get_device_credentials.side_effect = [CREDENTIALS, None]

        result = await self.options_flow(account).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["errors"]["base"], "device_not_registered")
        self.assertEqual(account.data, LOGIN)
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_update_subentry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()

    async def test_options_reports_a_failed_reload_after_saving(self) -> None:
        account = self.account()
        self.config_entries.async_reload.return_value = False

        with self.assertLogs("custom_components.tuya_ble", level="ERROR"):
            result = await self.options_flow(account).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "reload_failed")
        self.assertEqual(
            self.config_entries.async_update_entry.call_args.kwargs["data"][CONF_PASSWORD],
            USER_INPUT[CONF_PASSWORD],
        )

    async def test_legacy_options_persist_before_reloading(self) -> None:
        entry = self.account()
        entry.version = 1
        entry.data = {CONF_ADDRESS: ADDRESS}
        entry.options = dict(CREDENTIALS.__dict__)
        entry.unique_id = ADDRESS

        result = await self.options_flow(entry).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "create_entry")
        saved = self.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(saved[CONF_PASSWORD], USER_INPUT[CONF_PASSWORD])
        self.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    async def test_legacy_options_reports_a_failed_reload_after_saving(self) -> None:
        entry = self.account()
        entry.version = 1
        entry.data = {CONF_ADDRESS: ADDRESS}
        entry.options = dict(CREDENTIALS.__dict__)
        entry.unique_id = ADDRESS
        self.config_entries.async_reload.return_value = False

        with self.assertLogs("custom_components.tuya_ble", level="ERROR"):
            result = await self.options_flow(entry).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "reload_failed")
        saved = self.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(saved[CONF_PASSWORD], USER_INPUT[CONF_PASSWORD])

    async def test_options_do_not_save_if_devices_change_during_refresh(self) -> None:
        relay = _build_device_subentry(ADDRESS, "Relay", CREDENTIALS.__dict__)
        lock = _build_device_subentry(
            "AA:BB:CC:DD:EE:02", "Lock", CREDENTIALS.__dict__
        )
        account = self.account({relay.subentry_id: relay, lock.subentry_id: lock})

        async def remove_device(*args, **kwargs):
            account.subentries.pop(lock.subentry_id, None)
            return CREDENTIALS

        self.manager.get_device_credentials.side_effect = remove_device
        result = await self.options_flow(account).async_step_login(dict(USER_INPUT))

        self.assertEqual(result["errors"]["base"], "devices_changed")
        self.assertEqual(account.data, LOGIN)
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_update_subentry.assert_not_called()
