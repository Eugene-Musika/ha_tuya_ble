"""Verify failed datapoint commands do not overwrite known device state."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakError

from custom_components.tuya_ble.tuya_ble.const import (
    TuyaBLECode,
    TuyaBLEDataPointType,
)
from custom_components.tuya_ble.tuya_ble.tuya_ble import TuyaBLEDevice


class DatapointCommandStateTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = TuyaBLEDevice(
            None, SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="device")
        )
        self.device._protocol_version = 3

    def reported_switch(self):
        self.device.datapoints._update_from_device(
            1, 0, 0, TuyaBLEDataPointType.DT_BOOL, False
        )
        return self.device.datapoints[1]

    async def test_failed_write_preserves_reported_value(self) -> None:
        datapoint = self.reported_switch()
        with patch.object(
            self.device, "_send_packet", AsyncMock(side_effect=BleakError("offline"))
        ):
            with self.assertRaises(BleakError):
                await datapoint.set_value(True)
        self.assertFalse(datapoint.value)

    async def test_successful_write_uses_requested_value_and_updates_state(self) -> None:
        datapoint = self.reported_switch()
        with patch.object(self.device, "_send_packet", AsyncMock()) as send:
            await datapoint.set_value(True)
        send.assert_awaited_once_with(
            TuyaBLECode.FUN_SENDER_DPS, bytearray([1, 1, 1, 1])
        )
        self.assertTrue(datapoint.value)

    async def test_failed_new_datapoint_is_removed(self) -> None:
        datapoint = self.device.datapoints.get_or_create(
            1, TuyaBLEDataPointType.DT_BOOL, True
        )
        with patch.object(
            self.device, "_send_packet", AsyncMock(side_effect=BleakError("offline"))
        ):
            with self.assertRaises(BleakError):
                await datapoint.set_value(True)
        self.assertIsNone(self.device.datapoints[1])

    async def test_device_report_during_write_remains_authoritative(self) -> None:
        datapoint = self.reported_switch()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def send(*args):
            started.set()
            await finish.wait()

        with patch.object(self.device, "_send_packet", AsyncMock(side_effect=send)):
            command = asyncio.create_task(datapoint.set_value(True))
            await started.wait()
            self.device.datapoints._update_from_device(
                1, 1, 0, TuyaBLEDataPointType.DT_BOOL, False
            )
            finish.set()
            await command
        self.assertFalse(datapoint.value)

    async def test_new_datapoint_remains_hidden_until_write_succeeds(self) -> None:
        datapoint = self.device.datapoints.get_or_create(
            1, TuyaBLEDataPointType.DT_BOOL, True
        )
        started = asyncio.Event()
        finish = asyncio.Event()

        async def send(*args):
            started.set()
            await finish.wait()

        with patch.object(self.device, "_send_packet", AsyncMock(side_effect=send)):
            command = asyncio.create_task(datapoint.set_value(True))
            await started.wait()
            self.assertIsNone(self.device.datapoints[1])
            finish.set()
            await command
        self.assertIs(self.device.datapoints[1], datapoint)
        self.assertTrue(datapoint.value)

    async def test_out_of_order_command_responses_keep_latest_success(self) -> None:
        self.device.datapoints._update_from_device(
            9, 0, 0, TuyaBLEDataPointType.DT_VALUE, 0
        )
        datapoint = self.device.datapoints[9]
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        finish_first = asyncio.Event()
        finish_second = asyncio.Event()
        sent = []

        async def send(code, payload):
            sent.append(bytes(payload))
            if len(sent) == 1:
                first_started.set()
                await finish_first.wait()
            else:
                second_started.set()
                await finish_second.wait()

        with patch.object(self.device, "_send_packet", AsyncMock(side_effect=send)):
            first = asyncio.create_task(datapoint.set_value(10))
            await first_started.wait()
            second = asyncio.create_task(datapoint.set_value(20))
            await second_started.wait()
            finish_second.set()
            await second
            finish_first.set()
            await first
        self.assertEqual(datapoint.value, 20)
        self.assertEqual(sent, [b"\x09\x02\x04\x00\x00\x00\x0a", b"\x09\x02\x04\x00\x00\x00\x14"])
