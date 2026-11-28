"""Verify entity commands await datapoint writes and report failures."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from homeassistant.components.button import ButtonEntityDescription
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble.button import TuyaBLEButton, TuyaBLEButtonMapping
from custom_components.tuya_ble.switch import (
    TuyaBLESwitch,
    TuyaBLESwitchMapping,
    lock_switch_setter,
    set_fingerbot_program_repeat_forever,
)
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType


class CommandTests(IsolatedAsyncioTestCase):
    def switch(self, datapoint, **mapping_kwargs):
        entity = TuyaBLESwitch.__new__(TuyaBLESwitch)
        entity._device = SimpleNamespace(
            datapoints=Mock(get_or_create=Mock(return_value=datapoint))
        )
        entity._product = SimpleNamespace(fingerbot=None)
        entity._mapping = TuyaBLESwitchMapping(
            dp_id=1,
            description=SwitchEntityDescription(key="switch"),
            **mapping_kwargs,
        )
        return entity

    def button(self, datapoint, **mapping_kwargs):
        entity = TuyaBLEButton.__new__(TuyaBLEButton)
        entity._device = SimpleNamespace(
            datapoints=Mock(get_or_create=Mock(return_value=datapoint))
        )
        entity._mapping = TuyaBLEButtonMapping(
            dp_id=2,
            description=ButtonEntityDescription(key="button"),
            **mapping_kwargs,
        )
        return entity

    async def test_switch_commands_await_write_and_propagate_failure(self):
        for command, value in (("async_turn_on", True), ("async_turn_off", False)):
            with self.subTest(command=command):
                datapoint = SimpleNamespace(value=False, set_value=AsyncMock())
                entity = self.switch(datapoint)
                await getattr(entity, command)()
                datapoint.set_value.assert_awaited_once_with(value)
                failure = RuntimeError("write failed")
                datapoint.set_value.side_effect = failure
                with self.assertRaises(RuntimeError) as caught:
                    await getattr(entity, command)()
                self.assertIs(caught.exception, failure)

    async def test_bitmap_switch_preserves_other_bits(self):
        datapoint = SimpleNamespace(value=b"\xa5", set_value=AsyncMock())
        entity = self.switch(datapoint, bitmap_mask=b"\x03")
        await entity.async_turn_on()
        await entity.async_turn_off()
        self.assertEqual(
            [call.args[0] for call in datapoint.set_value.await_args_list],
            [b"\xa7", b"\xa4"],
        )

    async def test_lock_setter_awaits_both_commands(self):
        for command, dp_id, dp_type, initial, value in (
            ("async_turn_on", 6, TuyaBLEDataPointType.DT_RAW, b"\x01\x01", b"\x01\x01"),
            ("async_turn_off", 46, TuyaBLEDataPointType.DT_BOOL, False, True),
        ):
            with self.subTest(command=command):
                datapoint = SimpleNamespace(set_value=AsyncMock())
                entity = self.switch(datapoint, setter=lock_switch_setter)
                await getattr(entity, command)()
                entity._device.datapoints.get_or_create.assert_called_with(
                    dp_id, dp_type, initial
                )
                datapoint.set_value.assert_awaited_once_with(value)
                datapoint.set_value.side_effect = RuntimeError("lock failed")
                with self.assertRaisesRegex(RuntimeError, "lock failed"):
                    await getattr(entity, command)()

    async def test_fingerbot_setter_preserves_program_and_propagates_errors(self):
        datapoint = SimpleNamespace(value=b"\x00\x03\x12\x34", set_value=AsyncMock())
        entity = self.switch(datapoint, setter=set_fingerbot_program_repeat_forever)
        entity._product.fingerbot = SimpleNamespace(program=121)
        entity._device.datapoints = {121: datapoint}
        await entity.async_turn_on()
        await entity.async_turn_off()
        self.assertEqual(
            [call.args[0] for call in datapoint.set_value.await_args_list],
            [b"\xff\xff\x12\x34", b"\x00\x01\x12\x34"],
        )
        datapoint.set_value.side_effect = RuntimeError("program failed")
        with self.assertRaisesRegex(RuntimeError, "program failed"):
            await entity.async_turn_on()

    async def test_invalid_fingerbot_program_fails(self):
        datapoint = SimpleNamespace(value=b"\x01", set_value=AsyncMock())
        entity = self.switch(datapoint, setter=set_fingerbot_program_repeat_forever)
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()
        entity._product.fingerbot = SimpleNamespace(program=121)
        entity._device.datapoints = {121: datapoint}
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()
        datapoint.set_value.assert_not_awaited()

    async def test_button_commands_await_write_and_propagate_failure(self):
        for current, expected in ((False, True), (True, False)):
            with self.subTest(current=current):
                datapoint = SimpleNamespace(value=current, set_value=AsyncMock())
                entity = self.button(datapoint)
                await entity.async_press()
                datapoint.set_value.assert_awaited_once_with(expected)
                datapoint.set_value.side_effect = RuntimeError("button failed")
                with self.assertRaisesRegex(RuntimeError, "button failed"):
                    await entity.async_press()

    async def test_raw_button_and_invalid_values(self):
        datapoint = SimpleNamespace(set_value=AsyncMock())
        entity = self.button(
            datapoint, dp_type=TuyaBLEDataPointType.DT_RAW, value="AQE="
        )
        await entity.async_press()
        datapoint.set_value.assert_awaited_once_with(b"\x01\x01")
        datapoint.set_value.side_effect = RuntimeError("raw failed")
        with self.assertRaisesRegex(RuntimeError, "raw failed"):
            await entity.async_press()
        for invalid in (None, "bad!", "%%%"):
            with self.subTest(value=invalid):
                entity._mapping.value = invalid
                with self.assertRaises(HomeAssistantError):
                    await entity.async_press()
        self.assertEqual(datapoint.set_value.await_count, 2)

    async def test_missing_datapoints_fail(self):
        switch = self.switch(None)
        button = self.button(None)
        bitmap = self.switch(None, bitmap_mask=b"\x01")
        lock = self.switch(None, setter=lock_switch_setter)
        raw_button = self.button(
            None, dp_type=TuyaBLEDataPointType.DT_RAW, value="AQE="
        )
        for command in (
            switch.async_turn_on,
            switch.async_turn_off,
            bitmap.async_turn_on,
            bitmap.async_turn_off,
            lock.async_turn_on,
            lock.async_turn_off,
            button.async_press,
            raw_button.async_press,
        ):
            with self.subTest(command=command):
                with self.assertRaises(HomeAssistantError):
                    await command()

    async def test_unsupported_button_type_fails(self):
        datapoint = SimpleNamespace(value=0, set_value=AsyncMock())
        entity = self.button(datapoint, dp_type=TuyaBLEDataPointType.DT_VALUE)
        with self.assertRaises(HomeAssistantError):
            await entity.async_press()
        datapoint.set_value.assert_not_awaited()
