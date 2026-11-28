"""Behavioral tests for coordinated Tuya BLE connection recovery."""

import asyncio
import logging
from collections import deque
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakError

from custom_components.tuya_ble.tuya_ble import tuya_ble
from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode
from custom_components.tuya_ble.tuya_ble.manager import TuyaBLEDeviceCredentials


class _FakeClock:
    """A manually advanced event-loop clock and sleep implementation."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.now = 100.0
        self.delays: list[float] = []
        self._sleepers: deque[tuple[float, asyncio.Future[None]]] = deque()

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = self.loop.create_future()
        self.delays.append(delay)
        self._sleepers.append((delay, future))
        await future

    def advance_next_sleep(self) -> float:
        while self._sleepers:
            delay, future = self._sleepers.popleft()
            if not future.done():
                self.now += delay
                future.set_result(None)
                return delay
        raise AssertionError("No pending recovery sleep")


class _Client:
    def __init__(self) -> None:
        self.is_connected = True
        self.start_notify = AsyncMock()
        self.stop_notify = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.write_gatt_char = AsyncMock()
        self.disconnected_callback = None

    async def _disconnect(self) -> None:
        self.is_connected = False


class BLERecoveryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.devices: list[tuya_ble.TuyaBLEDevice] = []
        self.loop = asyncio.get_running_loop()
        self.loop.set_debug(False)
        self._real_sleep = asyncio.sleep
        self.clock = _FakeClock(self.loop)
        self.enterContext(patch.object(self.loop, "time", self.clock.time))
        self.enterContext(patch.object(tuya_ble.asyncio, "sleep", self.clock.sleep))

    async def asyncTearDown(self) -> None:
        for device in self.devices:
            await device.stop()

    async def _device(self, address: str = "AA:BB:CC:DD:EE:FF"):
        credentials = TuyaBLEDeviceCredentials(
            uuid="device-uuid",
            local_key="local-key",
            device_id=f"id-{address}",
            category="tdq",
            product_id="product",
            device_name="test device",
            product_model=None,
            product_name=None,
        )
        manager = SimpleNamespace(
            get_device_credentials=AsyncMock(return_value=credentials)
        )
        device = tuya_ble.TuyaBLEDevice(
            manager,
            SimpleNamespace(address=address, name="test device"),
        )
        await device.initialize()
        self.devices.append(device)
        return device

    def _successful_handshake(self, device):
        sent_codes: list[TuyaBLECode] = []

        async def send(code, _data, *_args):
            sent_codes.append(code)
            if code == TuyaBLECode.FUN_SENDER_PAIR:
                device._is_paired = True
            return True

        self.enterContext(
            patch.object(
                device,
                "_send_packet_while_connected",
                AsyncMock(side_effect=send),
            )
        )
        return sent_codes

    async def _spin_until(self, predicate, message: str) -> None:
        for _ in range(500):
            if predicate():
                return
            await self._real_sleep(0)
        self.fail(message)

    async def _wait_for_delay(self, count: int) -> None:
        await self._spin_until(
            lambda: len(self.clock.delays) >= count,
            f"recovery did not schedule sleep #{count}",
        )

    async def test_backoff_caps_and_resets_after_successful_pairing(self) -> None:
        device = await self._device()
        self._successful_handshake(device)
        clients: list[_Client] = []
        calls = 0

        async def establish(
            _client_type, _ble_device, _address, disconnected, **_kwargs
        ):
            nonlocal calls
            calls += 1
            if calls <= 8 or calls == 10:
                raise BleakError("radio unavailable")
            client = _Client()
            client.disconnected_callback = disconnected
            clients.append(client)
            return client

        establish_mock = AsyncMock(side_effect=establish)
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", establish_mock)
        )

        with self.assertLogs(tuya_ble._LOGGER.name, level=logging.DEBUG) as logs:
            with self.assertRaises(BleakError):
                await device._ensure_connected()

            expected_delays = [1, 2, 4, 8, 16, 32, 60, 60]
            for index, expected in enumerate(expected_delays, start=1):
                await self._wait_for_delay(index)
                self.assertEqual(self.clock.delays[index - 1], expected)
                self.clock.advance_next_sleep()
                await self._spin_until(
                    lambda: establish_mock.await_count >= index + 1,
                    f"connection cycle #{index + 1} did not start",
                )

            await self._spin_until(
                device._is_connected_and_paired,
                "device did not recover after the scheduled retries",
            )
            self.assertEqual(establish_mock.await_count, 9)

            await self._spin_until(
                lambda: (
                    device._connection_task is None
                    and device._recovery_task is None
                ),
                "successful connection task was not released",
            )

        warnings = [
            record for record in logs.records if record.levelno >= logging.WARNING
        ]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertTrue(
            any("recovered" in record.getMessage() for record in logs.records)
        )
        self.assertIn(device.address, warnings[0].getMessage())

        device._disconnected(clients[-1])
        await self._wait_for_delay(len(expected_delays) + 1)
        self.assertEqual(self.clock.delays[-1], 1)

    async def test_failed_initialization_stages_clean_clients_before_retry(
        self,
    ) -> None:
        for failure_stage in ("notifications", "device info", "pairing"):
            with self.subTest(stage=failure_stage):
                device = await self._device(f"AA:BB:CC:DD:EE:{len(self.devices):02X}")
                client = _Client()
                sent_codes: list[TuyaBLECode] = []

                async def establish(
                    _client_type, _ble_device, _address, disconnected, **_kwargs
                ):
                    client.disconnected_callback = disconnected
                    return client

                async def send(code, _data, *_args):
                    sent_codes.append(code)
                    if (
                        failure_stage == "device info"
                        and code == TuyaBLECode.FUN_SENDER_DEVICE_INFO
                    ):
                        return False
                    if (
                        failure_stage == "pairing"
                        and code == TuyaBLECode.FUN_SENDER_PAIR
                    ):
                        return False
                    if code == TuyaBLECode.FUN_SENDER_PAIR:
                        device._is_paired = True
                    return True

                if failure_stage == "notifications":
                    client.start_notify.side_effect = BleakError(
                        "missing characteristic"
                    )

                previous_delays = len(self.clock.delays)
                with (
                    patch.object(
                        tuya_ble,
                        "establish_connection",
                        AsyncMock(side_effect=establish),
                    ),
                    patch.object(
                        device,
                        "_send_packet_while_connected",
                        AsyncMock(side_effect=send),
                    ),
                ):
                    with self.assertRaises(BleakError):
                        await device._ensure_connected()

                    client.disconnect.assert_awaited_once()
                    await self._wait_for_delay(previous_delays + 1)
                    self.assertEqual(self.clock.delays[-1], 1)
                self.assertFalse(device._is_paired)
                if failure_stage == "device info":
                    self.assertIn(TuyaBLECode.FUN_SENDER_DEVICE_INFO, sent_codes)
                if failure_stage == "pairing":
                    self.assertIn(TuyaBLECode.FUN_SENDER_PAIR, sent_codes)

    async def test_cleanup_errors_are_logged_and_still_back_off(self) -> None:
        device = await self._device()
        client = _Client()
        client.start_notify.side_effect = BleakError("missing characteristic")
        client.disconnect.side_effect = BleakError("disconnect failed")

        async def establish(
            _client_type, _ble_device, _address, _disconnected, **_kwargs
        ):
            return client

        self.enterContext(
            patch.object(
                tuya_ble,
                "establish_connection",
                AsyncMock(side_effect=establish),
            )
        )
        with self.assertLogs(tuya_ble._LOGGER.name, level=logging.WARNING) as logs:
            with self.assertRaises(BleakError):
                await device._ensure_connected()
            await self._wait_for_delay(1)

        self.assertTrue(
            any(
                "disconnect" in record.getMessage().lower()
                for record in logs.records
            )
        )
        self.assertEqual(self.clock.delays, [1])
        client.disconnect.assert_awaited_once()

    async def test_concurrent_triggers_share_one_connection_attempt(self) -> None:
        device = await self._device()
        sent_codes = self._successful_handshake(device)
        client = _Client()
        started = asyncio.Event()
        release = asyncio.Event()

        async def establish(
            _client_type, _ble_device, _address, disconnected, **_kwargs
        ):
            started.set()
            await release.wait()
            client.disconnected_callback = disconnected
            return client

        establish_mock = AsyncMock(side_effect=establish)
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", establish_mock)
        )

        first_command = asyncio.create_task(device.update())
        await started.wait()
        reconnect = asyncio.create_task(device._reconnect())
        ensure = asyncio.create_task(device._ensure_connected())
        second_command = asyncio.create_task(device.update())
        await self._real_sleep(0)
        release.set()
        await asyncio.gather(first_command, reconnect, ensure, second_command)

        self.assertEqual(establish_mock.await_count, 1)
        self.assertEqual(establish_mock.await_args.kwargs["max_attempts"], 1)
        self.assertEqual(
            sent_codes.count(TuyaBLECode.FUN_SENDER_DEVICE_STATUS),
            2,
        )
        self.assertTrue(device._is_connected_and_paired())

    async def test_backoff_command_failure_and_other_device_progress(
        self,
    ):
        first = await self._device()
        second = await self._device("11:22:33:44:55:66")
        first_sent = self._successful_handshake(first)
        second_sent = self._successful_handshake(second)
        first_client = _Client()
        second_client = _Client()
        calls: dict[str, int] = {first.address: 0, second.address: 0}

        async def establish(
            _client_type, ble_device, _address, disconnected, **_kwargs
        ):
            calls[ble_device.address] += 1
            if ble_device.address == first.address and calls[first.address] == 1:
                raise BleakError("radio unavailable")
            client = (
                first_client
                if ble_device.address == first.address
                else second_client
            )
            client.disconnected_callback = disconnected
            return client

        establish_mock = AsyncMock(side_effect=establish)
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", establish_mock)
        )

        with self.assertRaises(BleakError):
            await first._ensure_connected()
        await self._wait_for_delay(1)

        with self.assertRaises(BleakError):
            await first.update()
        with self.assertRaises(BleakError):
            await first._ensure_connected()
        await first._reconnect()
        self.assertEqual(calls[first.address], 1)
        self.assertNotIn(TuyaBLECode.FUN_SENDER_DEVICE_STATUS, first_sent)

        await second._ensure_connected()
        self.assertTrue(second._is_connected_and_paired())
        self.assertEqual(calls[second.address], 1)

        self.clock.advance_next_sleep()
        await self._spin_until(
            first._is_connected_and_paired, "first device did not recover"
        )
        self.assertNotIn(TuyaBLECode.FUN_SENDER_DEVICE_STATUS, first_sent)

        await first.update()
        self.assertEqual(first_sent.count(TuyaBLECode.FUN_SENDER_DEVICE_STATUS), 1)
        self.assertEqual(second_sent.count(TuyaBLECode.FUN_SENDER_PAIR), 1)

    async def test_stop_during_backoff_prevents_another_attempt(self) -> None:
        device = await self._device()
        establish_mock = AsyncMock(side_effect=BleakError("radio unavailable"))
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", establish_mock)
        )

        with self.assertRaises(BleakError):
            await device._ensure_connected()
        await self._wait_for_delay(1)
        await device.stop()
        for _ in range(5):
            await self._real_sleep(0)

        self.assertEqual(establish_mock.await_count, 1)
        self.assertTrue(device._stopped)

    async def test_stop_during_connection_cleans_client(self) -> None:
        device = await self._device()
        client = _Client()
        notify_started = asyncio.Event()
        never_finish_notify = asyncio.Event()

        async def start_notify(*_args):
            notify_started.set()
            await never_finish_notify.wait()

        client.start_notify.side_effect = start_notify

        async def establish(
            _client_type, _ble_device, _address, disconnected, **_kwargs
        ):
            client.disconnected_callback = disconnected
            return client

        establish_mock = AsyncMock(side_effect=establish)
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", establish_mock)
        )
        connection = asyncio.create_task(device._ensure_connected())
        await notify_started.wait()
        await device.stop()
        await asyncio.gather(connection, return_exceptions=True)

        self.assertEqual(establish_mock.await_count, 1)
        client.disconnect.assert_awaited_once()
        self.assertTrue(device._stopped)
        self.assertEqual(self.clock.delays, [])

    async def test_stale_disconnect_cannot_disrupt_new_client(self) -> None:
        device = await self._device()
        self._successful_handshake(device)
        old_client = _Client()
        new_client = _Client()
        establish = AsyncMock(side_effect=[old_client, new_client])
        self.enterContext(patch.object(tuya_ble, "establish_connection", establish))
        await device._ensure_connected()
        device._disconnected(old_client)
        await self._wait_for_delay(1)
        self.clock.advance_next_sleep()
        await self._spin_until(
            lambda: device._client is new_client, "new client did not connect"
        )
        device._disconnected(old_client)
        self.assertIs(device._client, new_client)
        self.assertTrue(device._is_paired)
        await device.stop()
        device._disconnected(new_client)
        self.assertEqual(establish.await_count, 2)

    async def test_write_failure_closes_client_and_schedules_one_recovery(self) -> None:
        device = await self._device()
        self._successful_handshake(device)
        client = _Client()
        client.write_gatt_char.side_effect = BleakError("write failed")

        async def disconnect():
            client.is_connected = False
            device._disconnected(client)

        client.disconnect.side_effect = disconnect
        self.enterContext(
            patch.object(tuya_ble, "establish_connection", AsyncMock(return_value=client))
        )
        await device._ensure_connected()
        with self.assertRaises(BleakError):
            await device._int_send_packet_while_connected([b"packet"])
        client.disconnect.assert_awaited_once()
        await self._wait_for_delay(1)
        self.assertEqual(self.clock.delays, [1])
