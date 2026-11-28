"""Regression tests for relay state refresh across BLE reconnections."""

import asyncio
from datetime import datetime
import logging
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from custom_components.tuya_ble.devices import TuyaBLEPassiveCoordinator


class _Device:
    def __init__(self, category: str, product_id: str) -> None:
        self.address = "AA:BB:CC:DD:EE:FF"
        self.device_id = "device-id"
        self.category = category
        self.product_id = product_id
        self.datapoints = {
            47: SimpleNamespace(value=1, set_value=AsyncMock())
        }

    def register_connected_callback(self, callback) -> None:
        self.connected_callback = callback

    def register_callback(self, callback) -> None:
        self.update_callback = callback

    def register_disconnected_callback(self, callback) -> None:
        self.disconnected_callback = callback

    def connect(self) -> None:
        self.connected_callback()

    def disconnect(self) -> None:
        self.disconnected_callback()

    def update(self, datapoint_id: int) -> None:
        self.update_callback(
            [SimpleNamespace(id=datapoint_id, changed_by_device=False)]
        )


class RelayRecoveryTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.background_tasks: list[asyncio.Task] = []
        self.hass = SimpleNamespace(
            async_create_task=self._create_task,
            bus=SimpleNamespace(fire=Mock()),
        )
        self.disconnect_timeout = None

    def _create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.append(task)
        return task

    def _coordinator(self, category: str, product_id: str):
        device = _Device(category, product_id)

        def track_disconnect(hass, delay, callback):
            self.disconnect_timeout = callback
            return Mock()

        self.enterContext(
            patch(
                "homeassistant.components.bluetooth.update_coordinator."
                "async_address_present",
                return_value=False,
            )
        )
        self.enterContext(
            patch(
                "custom_components.tuya_ble.devices.async_call_later",
                side_effect=track_disconnect,
            )
        )
        coordinator = TuyaBLEPassiveCoordinator(
            self.hass,
            logging.getLogger(__name__),
            device.address,
            device,
        )
        return device, coordinator

    def _expire_disconnect_timeout(self) -> None:
        assert self.disconnect_timeout is not None
        self.disconnect_timeout(datetime.now())
        self.disconnect_timeout = None

    async def _wait_for_background_tasks(self) -> None:
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks)
            self.background_tasks.clear()

    async def test_relay_requests_full_state_after_reconnect(self) -> None:
        device, coordinator = self._coordinator("tdq", "vs4ksbpax2dwwm6a")
        listener = Mock()
        coordinator.async_add_listener(listener)

        device.connect()
        device.disconnect()
        self._expire_disconnect_timeout()
        self.assertFalse(coordinator.connected)

        device.connect()
        self.assertTrue(coordinator.connected)
        device.update(47)
        await self._wait_for_background_tasks()
        device.update(44)
        await self._wait_for_background_tasks()

        device.datapoints[47].set_value.assert_awaited_once_with(1)
        updates_before = listener.call_count
        device.update(1)
        self.assertEqual(listener.call_count, updates_before + 1)
        device.datapoints[47].set_value.assert_awaited_once_with(1)

    async def test_non_relay_reconnect_does_not_request_full_state(self) -> None:
        device, coordinator = self._coordinator("wsdcg", "ojzlzzsw")
        listener = Mock()
        coordinator.async_add_listener(listener)

        device.connect()
        device.disconnect()
        self._expire_disconnect_timeout()
        device.connect()
        self.assertTrue(coordinator.connected)

        updates_before = listener.call_count
        device.update(2)
        await self._wait_for_background_tasks()

        self.assertEqual(listener.call_count, updates_before + 1)
        device.datapoints[47].set_value.assert_not_awaited()
