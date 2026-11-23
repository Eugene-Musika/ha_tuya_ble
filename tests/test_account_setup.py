"""Tests for independently loading and unloading account devices."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import tuya_ble
from custom_components.tuya_ble.config_flow import TuyaBLEOptionsFlow
from custom_components.tuya_ble.const import (
    CONF_ACCESS_ID,
    CONF_ENDPOINT,
    DOMAIN,
)
from custom_components.tuya_ble.tuya_ble import TuyaBLEDeviceCredentials


class AccountSetupTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_reload=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        self.hass = SimpleNamespace(
            data={},
            config_entries=self.config_entries,
            bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
            async_create_task=asyncio.create_task,
        )
        self.entry = SimpleNamespace(
            entry_id="account",
            version=2,
            data={"username": "user"},
            options={},
            title="user",
            subentries={
                "relay": SimpleNamespace(
                    data={CONF_ADDRESS: "AA:BB:CC:DD:EE:01"}, title="relay"
                ),
                "lock": SimpleNamespace(
                    data={CONF_ADDRESS: "AA:BB:CC:DD:EE:02"}, title="lock"
                ),
            },
            async_on_unload=Mock(),
            add_update_listener=Mock(return_value=Mock()),
        )

    async def test_missing_device_does_not_block_and_reloads_once_when_seen(self) -> None:
        relay = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        found = SimpleNamespace(address="AA:BB:CC:DD:EE:01")

        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: (
                    found if address == found.address else None
                ),
            ),
            patch.object(
                tuya_ble.bluetooth, "async_register_callback", return_value=Mock()
            ) as register_callback,
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=relay)),
        ):
            self.assertTrue(await tuya_ble.async_setup_entry(self.hass, self.entry))

            account = self.hass.data[DOMAIN][self.entry.entry_id]
            self.assertEqual(account.devices, {"relay": relay})
            self.config_entries.async_forward_entry_setups.assert_awaited_once()

            callback = register_callback.call_args.args[1]
            self.assertEqual(
                register_callback.call_args.args[2]["address"], "AA:BB:CC:DD:EE:02"
            )
            callback(None, None)
            callback(None, None)
            await asyncio.sleep(0)
            self.config_entries.async_reload.assert_awaited_once_with("account")

            self.assertTrue(await tuya_ble.async_unload_entry(self.hass, self.entry))
            relay.device.stop.assert_awaited_once()

    async def test_missing_device_does_not_reload_on_an_unrelated_update(self) -> None:
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: (
                    SimpleNamespace(address=address) if address.endswith("01") else None
                ),
            ),
            patch.object(tuya_ble.bluetooth, "async_register_callback", return_value=Mock()),
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            await tuya_ble._async_update_listener(self.hass, self.entry)

        self.config_entries.async_reload.assert_not_awaited()

    async def test_missing_device_can_retry_a_failed_reload(self) -> None:
        self.config_entries.async_reload = AsyncMock(
            side_effect=[False, RuntimeError("reload"), True]
        )
        callbacks = []
        tasks = []

        def register(hass, callback, matcher, mode):
            callbacks.append(callback)
            return Mock()

        def create_task(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        self.hass.async_create_task = create_task
        with (
            patch.object(tuya_ble.bluetooth, "async_ble_device_from_address", return_value=None),
            patch.object(tuya_ble.bluetooth, "async_register_callback", side_effect=register),
            self.assertLogs(tuya_ble.__name__, level="ERROR"),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            callbacks[0](None, None)
            await tasks[-1]
            callbacks[0](None, None)
            with self.assertRaisesRegex(RuntimeError, "reload"):
                await tasks[-1]
            callbacks[0](None, None)
            await tasks[-1]

        self.assertEqual(self.config_entries.async_reload.await_count, 3)

    async def test_removing_an_unavailable_subentry_reloads_the_account(self) -> None:
        relay = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        found = SimpleNamespace(address="AA:BB:CC:DD:EE:01")

        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: (
                    found if address == found.address else None
                ),
            ),
            patch.object(tuya_ble.bluetooth, "async_register_callback", return_value=Mock()),
            patch.object(tuya_ble.bluetooth, "async_rediscover_address") as rediscover,
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=relay)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            self.entry.subentries.pop("lock")

            await tuya_ble._async_update_listener(self.hass, self.entry)

        self.config_entries.async_reload.assert_awaited_once_with("account")
        rediscover.assert_called_once_with(self.hass, "AA:BB:CC:DD:EE:02")

    async def test_removing_a_device_triggers_bluetooth_rediscovery(self) -> None:
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: SimpleNamespace(address=address),
            ),
            patch.object(tuya_ble.bluetooth, "async_rediscover_address") as rediscover,
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            self.entry.subentries.pop("relay")
            await tuya_ble._async_update_listener(self.hass, self.entry)

        rediscover.assert_called_once_with(self.hass, "AA:BB:CC:DD:EE:01")
        self.config_entries.async_reload.assert_awaited_once_with("account")

    async def test_login_change_reloads_account(self) -> None:
        self.entry.subentries = {}
        await tuya_ble.async_setup_entry(self.hass, self.entry)
        self.entry.data = {"username": "updated"}

        await tuya_ble._async_update_listener(self.hass, self.entry)

        self.config_entries.async_reload.assert_awaited_once_with("account")

    async def test_simultaneous_account_updates_reload_once(self) -> None:
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        started = asyncio.Event()
        release = asyncio.Event()

        async def reload(entry_id):
            started.set()
            await release.wait()
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            return True

        self.config_entries.async_reload = AsyncMock(side_effect=reload)
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: SimpleNamespace(address=address),
            ),
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            self.entry.data = {"username": "updated"}
            first = asyncio.create_task(tuya_ble._async_update_listener(self.hass, self.entry))
            await started.wait()
            second = asyncio.create_task(tuya_ble._async_update_listener(self.hass, self.entry))
            await asyncio.sleep(0)
            try:
                self.assertEqual(self.config_entries.async_reload.await_count, 1)
            finally:
                release.set()
                await asyncio.gather(first, second)

        self.config_entries.async_reload.assert_awaited_once_with("account")

    async def test_refreshing_multiple_devices_reloads_once(self) -> None:
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        credentials = TuyaBLEDeviceCredentials(
            uuid="uuid",
            local_key="new-key",
            device_id="device-id",
            category="tdq",
            product_id="product",
            device_name=None,
            product_model=None,
            product_name=None,
        )
        self.entry.data = {
            CONF_ENDPOINT: "https://example.invalid",
            CONF_ACCESS_ID: "access",
            "username": "user",
            "password": "old",
        }
        for subentry_id, subentry in self.entry.subentries.items():
            subentry.subentry_id = subentry_id
        listeners = []

        def notify():
            listeners.append(
                asyncio.create_task(tuya_ble._async_update_listener(self.hass, self.entry))
            )

        def update_subentry(entry, subentry, *, data):
            subentry.data = data
            notify()

        def update_entry(entry, *, data, unique_id, title):
            entry.data = data
            notify()

        async def reload(entry_id):
            await asyncio.sleep(0)
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            return True

        self.config_entries.async_entry_for_domain_unique_id = Mock(return_value=self.entry)
        self.config_entries.async_get_known_entry = Mock(return_value=self.entry)
        self.config_entries.async_update_subentry = Mock(side_effect=update_subentry)
        self.config_entries.async_update_entry = Mock(side_effect=update_entry)
        self.config_entries.async_reload = AsyncMock(side_effect=reload)
        manager = SimpleNamespace(
            get_device_credentials=AsyncMock(return_value=credentials)
        )
        flow = TuyaBLEOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        flow.context = {"source": "user"}

        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: SimpleNamespace(address=address),
            ),
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            login = {**self.entry.data, "password": "new"}
            result = await flow._async_save_login(manager, login, {})
            await asyncio.gather(*listeners)

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.entry.data["password"], "new")
        self.config_entries.async_reload.assert_awaited_once_with("account")

    async def test_updates_during_setup_do_not_overlap_reloads(self) -> None:
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        first_setup_done = asyncio.Event()
        release_first = asyncio.Event()
        reloads = 0

        async def reload(entry_id):
            nonlocal reloads
            reloads += 1
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            if reloads == 1:
                first_setup_done.set()
                await release_first.wait()
            return True

        self.config_entries.async_reload = AsyncMock(side_effect=reload)
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: SimpleNamespace(address=address),
            ),
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            self.entry.data = {"username": "first update"}
            first = asyncio.create_task(tuya_ble._async_update_listener(self.hass, self.entry))
            await first_setup_done.wait()
            self.entry.data = {"username": "second update"}
            second = asyncio.create_task(tuya_ble._async_update_listener(self.hass, self.entry))
            await asyncio.sleep(0)
            try:
                self.assertEqual(reloads, 1)
            finally:
                release_first.set()
                await asyncio.gather(first, second)

        self.assertEqual(reloads, 2)

    async def test_unload_stops_every_device(self) -> None:
        relay = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        lock = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))

        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable: SimpleNamespace(address=address),
            ),
            patch.object(
                tuya_ble, "_async_setup_device", AsyncMock(side_effect=[relay, lock])
            ),
        ):
            await tuya_ble.async_setup_entry(self.hass, self.entry)
            self.assertTrue(await tuya_ble.async_unload_entry(self.hass, self.entry))

        relay.device.stop.assert_awaited_once()
        lock.device.stop.assert_awaited_once()

    async def test_legacy_entry_without_login_loads_as_one_device(self) -> None:
        self.entry.version = 1
        self.entry.data = {CONF_ADDRESS: "AA:BB:CC:DD:EE:01"}
        self.entry.subentries = {}
        device = SimpleNamespace(device=SimpleNamespace(stop=AsyncMock()))
        with (
            patch.object(
                tuya_ble.bluetooth,
                "async_ble_device_from_address",
                return_value=SimpleNamespace(address="AA:BB:CC:DD:EE:01"),
            ),
            patch.object(tuya_ble, "_async_setup_device", AsyncMock(return_value=device)),
        ):
            self.assertTrue(await tuya_ble.async_setup_entry(self.hass, self.entry))

        account = self.hass.data[DOMAIN][self.entry.entry_id]
        self.assertEqual(account.devices, {None: device})
        self.assertEqual(account.manager.data, {})

    async def test_legacy_entry_still_waits_for_missing_device(self) -> None:
        self.entry.version = 1
        self.entry.data = {CONF_ADDRESS: "AA:BB:CC:DD:EE:01"}
        self.entry.subentries = {}
        with (
            patch.object(tuya_ble.bluetooth, "async_ble_device_from_address", return_value=None),
            patch.object(tuya_ble, "get_device", AsyncMock(return_value=None)),
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await tuya_ble.async_setup_entry(self.hass, self.entry)
