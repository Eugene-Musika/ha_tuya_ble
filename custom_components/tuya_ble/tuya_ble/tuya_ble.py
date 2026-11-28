from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from struct import pack, unpack

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)
from Crypto.Cipher import AES

from .const import (
    CHARACTERISTIC_NOTIFY,
    CHARACTERISTIC_WRITE,
    GATT_MTU,
    MANUFACTURER_DATA_ID,
    RESPONSE_WAIT_TIMEOUT,
    SERVICE_UUID,
    TuyaBLECode,
    TuyaBLEDataPointType,
)
from .exceptions import (
    TuyaBLEDataCRCError,
    TuyaBLEDataFormatError,
    TuyaBLEDataLengthError,
    TuyaBLEDeviceError,
    TuyaBLEEnumValueError,
)
from .manager import AbstaractTuyaBLEDeviceManager, TuyaBLEDeviceCredentials

_LOGGER = logging.getLogger(__name__)


BLEAK_EXCEPTIONS = (*BLEAK_RETRY_EXCEPTIONS, OSError)


def decode_advertisement_uuid(
    service_data: dict[str, bytes] | None,
    manufacturer_data: dict[int, bytes] | None,
) -> str | None:
    """Decode the device uuid from Tuya BLE advertisement data."""
    raw_product_id: bytes | None = None
    if service_data:
        data = service_data.get(SERVICE_UUID)
        if data and len(data) > 1 and data[0] == 0:
            raw_product_id = data[1:]
    if not raw_product_id or not manufacturer_data:
        return None
    data = manufacturer_data.get(MANUFACTURER_DATA_ID)
    if not data or len(data) <= 6:
        return None
    key = hashlib.md5(raw_product_id).digest()
    cipher = AES.new(key, AES.MODE_CBC, key)
    try:
        return cipher.decrypt(data[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


class TuyaBLEDataPoint:
    def __init__(
        self,
        owner: TuyaBLEDataPoints,
        id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
        provisional: bool = False,
    ) -> None:
        self._owner = owner
        self._id = id
        self._value = value
        self._changed_by_device = False
        self._device_revision = 0
        self._write_sequence = 0
        self._confirmed_write_sequence = 0
        self._pending_writes = 0
        self._update_from_device(timestamp, flags, type, value)
        self._provisional = provisional

    def _update_from_device(
        self,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        self._timestamp = timestamp
        self._flags = flags
        self._type = type
        self._changed_by_device = self._value != value
        self._value = value
        self._device_revision += 1
        self._provisional = False

    def _get_value(self, value: bytes | bool | int | str | None = None) -> bytes:
        if value is None:
            value = self._value
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                return value
            case TuyaBLEDataPointType.DT_BOOL:
                return pack(">B", 1 if value else 0)
            case TuyaBLEDataPointType.DT_VALUE:
                return pack(">i", value)
            case TuyaBLEDataPointType.DT_ENUM:
                if value > 0xFFFF:
                    return pack(">I", value)
                elif value > 0xFF:
                    return pack(">H", value)
                else:
                    return pack(">B", value)
            case TuyaBLEDataPointType.DT_STRING:
                return value.encode()

    @property
    def id(self) -> int:
        return self._id

    @property
    def timestamp(self) -> float:
        return self._timestamp

    @property
    def flags(self) -> int:
        return self._flags

    @property
    def type(self) -> TuyaBLEDataPointType:
        return self._type

    @property
    def value(self) -> bytes | bool | int | str:
        return self._value

    @property
    def changed_by_device(self) -> bool:
        return self._changed_by_device

    async def set_value(self, value: bytes | bool | int | str) -> None:
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                value = bytes(value)
            case TuyaBLEDataPointType.DT_BOOL:
                value = bool(value)
            case TuyaBLEDataPointType.DT_VALUE:
                value = int(value)
            case TuyaBLEDataPointType.DT_ENUM:
                value = int(value)
                if value < 0:
                    raise TuyaBLEEnumValueError()
            case TuyaBLEDataPointType.DT_STRING:
                value = str(value)

        if self._owner._update_started:
            self._value = value
            self._changed_by_device = False
            await self._owner._update_from_user(self._id)
            return

        if self._owner._datapoints.get(self._id) is not self:
            raise BleakError("Datapoint is no longer current")
        self._write_sequence += 1
        write_sequence = self._write_sequence
        device_revision = self._device_revision
        self._pending_writes += 1
        try:
            await self._owner._update_from_user(self._id, value)
            self._provisional = False
            if (
                self._device_revision == device_revision
                and write_sequence > self._confirmed_write_sequence
            ):
                self._value = value
                self._changed_by_device = False
                self._confirmed_write_sequence = write_sequence
        finally:
            self._pending_writes -= 1
            if (
                self._provisional
                and not self._pending_writes
                and self._owner._datapoints.get(self._id) is self
            ):
                del self._owner._datapoints[self._id]


class TuyaBLEDataPoints:
    def __init__(self, owner: TuyaBLEDevice) -> None:
        self._owner = owner
        self._datapoints: dict[int, TuyaBLEDataPoint] = {}
        self._update_started: int = 0
        self._updated_datapoints: list[int] = []

    def __len__(self) -> int:
        return len(self._datapoints)

    def __getitem__(self, key: int) -> TuyaBLEDataPoint | None:
        datapoint = self._datapoints.get(key)
        return datapoint if datapoint and not datapoint._provisional else None

    def has_id(self, id: int, type: TuyaBLEDataPointType | None = None) -> bool:
        datapoint = self[id]
        return datapoint is not None and (type is None or datapoint.type == type)

    def get_or_create(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        datapoint = self._datapoints.get(id)
        if datapoint:
            return datapoint
        datapoint = TuyaBLEDataPoint(
            self, id, time.time(), 0, type, value, provisional=True
        )
        self._datapoints[id] = datapoint
        return datapoint

    def begin_update(self) -> None:
        self._update_started += 1

    async def end_update(self) -> None:
        if self._update_started > 0:
            self._update_started -= 1
            if self._update_started == 0 and len(self._updated_datapoints) > 0:
                await self._owner._send_datapoints(self._updated_datapoints)
                for dp_id in self._updated_datapoints:
                    self._datapoints[dp_id]._provisional = False
                self._updated_datapoints = []

    def _update_from_device(
        self,
        dp_id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        dp = self._datapoints.get(dp_id)
        if dp:
            dp._update_from_device(timestamp, flags, type, value)
        else:
            self._datapoints[dp_id] = TuyaBLEDataPoint(
                self, dp_id, timestamp, flags, type, value
            )

    async def _update_from_user(
        self, dp_id: int, value: bytes | bool | int | str | None = None
    ) -> None:
        if self._update_started > 0:
            if dp_id in self._updated_datapoints:
                self._updated_datapoints.remove(dp_id)
            self._updated_datapoints.append(dp_id)
        else:
            await self._owner._send_datapoints(
                [dp_id], {dp_id: value} if value is not None else None
            )


global_connect_lock = asyncio.Lock()


class TuyaBLEDevice:
    def __init__(
        self,
        device_manager: AbstaractTuyaBLEDeviceManager,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Init the TuyaBLE."""
        self._device_manager = device_manager
        self._device_info: TuyaBLEDeviceCredentials | None = None
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._operation_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._expected_disconnect = False
        self._stopped = False
        self._connection_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._write_cleanup_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._retry_delay = 1.0
        self._current_retry_delay = 0.0
        self._retry_not_before = 0.0
        self._last_connection_error: tuple[str, str] | None = None
        self._consecutive_connection_failures = 0
        self._connected_callbacks: list[Callable[[], None]] = []
        self._callbacks: list[Callable[[list[TuyaBLEDataPoint]], None]] = []
        self._disconnected_callbacks: list[Callable[[], None]] = []
        self._current_seq_num = 1
        self._seq_num_lock = asyncio.Lock()

        self._is_bound = False
        self._flags = 0
        self._protocol_version = 2

        self._device_version: str = ""
        self._protocol_version_str: str = ""
        self._hardware_version: str = ""

        self._device_info: TuyaBLEDeviceCredentials | None = None

        self._auth_key: bytes | None = None
        self._local_key: bytes | None = None
        self._login_key: bytes | None = None
        self._session_key: bytes | None = None

        self._is_paired = False

        self._input_buffer: bytearray | None = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0
        self._input_expected_responses: dict[int,
                                             asyncio.Future[int] | None] = {}
        # self._input_future: asyncio.Future[int] | None = None

        self._datapoints = TuyaBLEDataPoints(self)

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    async def initialize(self) -> None:
        _LOGGER.debug("%s: Initializing", self.address)
        if await self._update_device_info():
            self._decode_advertisement_data()

    def _build_pairing_request(self) -> bytes:
        result = bytearray()

        result += self._device_info.uuid.encode()
        result += self._local_key
        result += self._device_info.device_id.encode()
        for _ in range(44 - len(result)):
            result += b"\x00"

        return result

    async def pair(self) -> None:
        """
        _LOGGER.debug("%s: Sending pairing request: %s",
            self.address, data.hex()
        )
        """
        await self._send_packet(
            TuyaBLECode.FUN_SENDER_PAIR, self._build_pairing_request()
        )

    async def update(self) -> None:
        _LOGGER.debug("%s: Updating", self.address)
        await self._send_packet(TuyaBLECode.FUN_SENDER_DEVICE_STATUS, bytes())

    async def _update_device_info(self) -> bool:
        if self._device_info is None:
            if self._device_manager:
                self._device_info = await self._device_manager.get_device_credentials(
                    self._ble_device.address, False, uuid=getattr(self, "_uuid", None)
                )
            if self._device_info:
                self._local_key = self._device_info.local_key[:6].encode()
                self._login_key = hashlib.md5(self._local_key).digest()

        return self._device_info is not None

    def _decode_advertisement_data(self) -> None:
        raw_product_id: bytes | None = None
        # raw_product_key: bytes | None = None
        raw_uuid: bytes | None = None
        if self._advertisement_data:
            if self._advertisement_data.service_data:
                service_data = self._advertisement_data.service_data.get(
                    SERVICE_UUID)
                if service_data and len(service_data) > 1:
                    match service_data[0]:
                        case 0:
                            raw_product_id = service_data[1:]
                        # case 1:
                        #    raw_product_key = service_data[1:]

            if self._advertisement_data.manufacturer_data:
                manufacturer_data = self._advertisement_data.manufacturer_data.get(
                    MANUFACTURER_DATA_ID
                )
                if manufacturer_data and len(manufacturer_data) > 6:
                    self._is_bound = (manufacturer_data[0] & 0x80) != 0
                    self._protocol_version = manufacturer_data[1]
                    raw_uuid = manufacturer_data[6:]
                    if raw_product_id:
                        key = hashlib.md5(raw_product_id).digest()
                        cipher = AES.new(key, AES.MODE_CBC, key)
                        raw_uuid = cipher.decrypt(raw_uuid)
                        self._uuid = raw_uuid.decode("utf-8")

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        if self._device_info:
            return self._device_info.device_name
        else:
            return self._ble_device.name or self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def uuid(self) -> str:
        if self._device_info is not None:
            return self._device_info.uuid
        else:
            return ""

    @property
    def local_key(self) -> str:
        if self._device_info is not None:
            return self._device_info.local_key
        else:
            return ""

    @property
    def category(self) -> str:
        if self._device_info is not None:
            return self._device_info.category
        else:
            return ""

    @property
    def device_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.device_id
        else:
            return ""

    @property
    def product_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_id
        else:
            return ""

    @property
    def product_model(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_model
        else:
            return ""

    @property
    def product_name(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_name
        else:
            return ""

    @property
    def device_version(self) -> str:
        return self._device_version

    @property
    def hardware_version(self) -> str:
        return self._hardware_version

    @property
    def protocol_version(self) -> str:
        return self._protocol_version_str

    @property
    def datapoints(self) -> TuyaBLEDataPoints:
        """Get datapoints exposed by device."""
        return self._datapoints

    def get_or_create_datapoint(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        """Get datapoints exposed by device."""

    def _fire_connected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._connected_callbacks:
            callback()

    def register_connected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._connected_callbacks.remove(callback)

        self._connected_callbacks.append(callback)
        return unregister_callback

    def _fire_callbacks(self, datapoints: list[TuyaBLEDataPoint]) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(datapoints)

    def register_callback(
        self,
        callback: Callable[[list[TuyaBLEDataPoint]], None],
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def _fire_disconnected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._disconnected_callbacks:
            callback()

    def register_disconnected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._disconnected_callbacks.remove(callback)

        self._disconnected_callbacks.append(callback)
        return unregister_callback

    async def start(self):
        """Start the TuyaBLE."""
        _LOGGER.debug("%s: Starting...", self.address)
        # await self._send_packet()

    async def stop(self) -> None:
        """Stop the TuyaBLE."""
        _LOGGER.debug("%s: Stop", self.address)
        self._stopped = True
        self._expected_disconnect = True
        current_task = asyncio.current_task()
        tasks = [
            task for task in self._background_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            if task is not self._write_cleanup_task:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._execute_disconnect()

    def _disconnected(
        self, client: BleakClientWithServiceCache, *, schedule_recovery: bool = True
    ) -> None:
        """Disconnected callback."""
        if client is not self._client or self._stopped:
            return
        was_paired = self._is_paired
        self._is_paired = False
        self._current_seq_num = 1
        self._clean_input()
        self._fire_disconnected_callbacks()
        if self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s",
                self.address,
                self.rssi,
            )
            return
        self._client = None
        _LOGGER.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.address,
            self.rssi,
        )
        if was_paired and self._connection_task is None and schedule_recovery:
            _LOGGER.debug(
                "%s: Scheduling reconnect; RSSI: %s",
                self.address,
                self.rssi,
            )
            self._queue_recovery(BleakError("unexpected disconnect"))

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._expected_disconnect = True
        for task in (self._connection_task, self._recovery_task):
            if task is not None and not task.done():
                task.cancel()
        self._create_background_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting",
            self.address,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._client = None
            self._is_paired = False
            if client:
                await self._cleanup_client(client)
            for future in self._input_expected_responses.values():
                if future is not None and not future.done():
                    future.cancel()
            self._input_expected_responses.clear()
        async with self._seq_num_lock:
            self._current_seq_num = 1

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._stopped or self._expected_disconnect:
            raise BleakError("Device is stopped")
        if self._write_cleanup_task is not None and not self._write_cleanup_task.done():
            raise BleakError("BLE client cleanup is in progress")
        if self._is_connected_and_paired():
            return
        task = self._connection_task
        if task is not None and not task.done():
            await asyncio.shield(task)
            if not self._is_connected_and_paired():
                raise BleakError("Connection attempt did not pair the device")
            return
        if self._retry_not_before > asyncio.get_running_loop().time():
            self._ensure_recovery_task()
            raise BleakError("Connection recovery is waiting for backoff")

        if task is None or task.done():
            task = self._start_connection_attempt()
        await asyncio.shield(task)
        if not self._is_connected_and_paired():
            raise BleakError("Connection attempt did not pair the device")

    def _is_connected_and_paired(self) -> bool:
        return bool(
            self._client and self._client.is_connected and self._is_paired
        )

    def _create_background_task(self, coroutine, report_exception: bool = True):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def task_done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            if not done_task.cancelled():
                error = done_task.exception()
                if error is not None and report_exception:
                    _LOGGER.warning(
                        "%s: Background BLE operation failed: %s",
                        self.address,
                        error,
                    )

        task.add_done_callback(task_done)
        return task

    def _start_connection_attempt(self) -> asyncio.Task[None]:
        task = self._create_background_task(
            self._run_connection_attempt(), report_exception=False
        )
        self._connection_task = task

        def clear_attempt(done_task: asyncio.Task) -> None:
            if self._connection_task is done_task:
                self._connection_task = None

        task.add_done_callback(clear_attempt)
        return task

    async def _run_connection_attempt(self) -> None:
        try:
            await self._connect_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._queue_recovery(error)
            raise
        else:
            self._record_connection_success()

    async def _connect_once(self) -> None:
        """Perform one connection, notification, info, and pairing cycle."""
        global global_connect_lock
        async with self._connect_lock:
            if self._stopped or self._expected_disconnect:
                raise BleakError("Device is stopped")
            if self._is_connected_and_paired():
                return

            old_client = self._client
            if old_client is not None:
                self._client = None
                self._is_paired = False
                await self._cleanup_client(old_client)

            self._is_paired = False
            async with self._seq_num_lock:
                self._current_seq_num = 1
            client: BleakClientWithServiceCache | None = None
            stage = "establish connection"
            try:
                async with global_connect_lock:
                    _LOGGER.debug(
                        "%s: Connecting; RSSI: %s", self.address, self.rssi
                    )
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        self._ble_device,
                        self.address,
                        self._disconnected,
                        use_services_cache=True,
                        ble_device_callback=lambda: self._ble_device,
                        max_attempts=1,
                    )
                if self._stopped or self._expected_disconnect:
                    raise asyncio.CancelledError
                if client is None or not client.is_connected:
                    raise BleakNotFoundError()

                self._client = client
                _LOGGER.debug("%s: Connected; RSSI: %s", self.address, self.rssi)

                stage = "start notifications"

                def notification_handler(sender: int, data: bytearray) -> None:
                    if (
                        self._client is client
                        and not self._stopped
                        and not self._expected_disconnect
                    ):
                        self._notification_handler(sender, data)

                await client.start_notify(
                    CHARACTERISTIC_NOTIFY, notification_handler
                )
                if self._stopped or self._expected_disconnect:
                    raise asyncio.CancelledError

                stage = "request device info"
                if not await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_DEVICE_INFO,
                    bytes(0),
                    0,
                    True,
                ):
                    raise BleakError("device info response timed out")
                if self._stopped or self._expected_disconnect:
                    raise asyncio.CancelledError

                stage = "pair device"
                if not await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_PAIR,
                    self._build_pairing_request(),
                    0,
                    True,
                ):
                    raise BleakError("pairing response timed out")
                if not self._is_connected_and_paired():
                    raise BleakError("pairing did not complete")
            except asyncio.CancelledError:
                if client is not None:
                    self._client = None
                    self._is_paired = False
                    await self._cleanup_client(client)
                raise
            except Exception as error:
                if client is not None:
                    self._client = None
                    self._is_paired = False
                    await self._cleanup_client(client)
                raise BleakError(f"{stage} failed: {error}") from error

        _LOGGER.debug("%s: Successfully connected", self.address)
        self._fire_connected_callbacks()

    async def _cleanup_client(self, client: BleakClientWithServiceCache) -> None:
        """Release a client, disconnecting even if notification cleanup fails."""
        try:
            if client.is_connected:
                await client.stop_notify(CHARACTERISTIC_NOTIFY)
        except Exception as error:
            _LOGGER.warning(
                "%s: Failed to stop notifications during BLE cleanup: %s",
                self.address,
                error,
            )
        try:
            await client.disconnect()
        except Exception as error:
            _LOGGER.warning(
                "%s: Failed to disconnect BLE client during cleanup: %s",
                self.address,
                error,
            )

    def _queue_recovery(self, error: Exception) -> None:
        if self._stopped or self._expected_disconnect:
            return
        loop = asyncio.get_running_loop()
        if self._retry_not_before > loop.time():
            self._ensure_recovery_task()
            return

        delay = self._retry_delay
        self._current_retry_delay = delay
        self._retry_not_before = loop.time() + delay
        self._retry_delay = min(delay * 2, 60.0)
        self._consecutive_connection_failures += 1
        failure = (type(error).__name__, str(error))
        if failure != self._last_connection_error:
            _LOGGER.warning(
                "%s: BLE connection cycle failed (%s): %s; retrying in %.0fs",
                self.address,
                failure[0],
                failure[1],
                delay,
            )
            self._last_connection_error = failure
        else:
            _LOGGER.debug(
                "%s: BLE connection still failing (%s), failure %s; "
                "retrying in %.0fs",
                self.address,
                failure[0],
                self._consecutive_connection_failures,
                delay,
            )
        self._ensure_recovery_task()

    def _record_connection_success(self) -> None:
        if self._last_connection_error is not None:
            _LOGGER.info(
                "%s: BLE connection recovered after %s failed cycle(s)",
                self.address,
                self._consecutive_connection_failures,
            )
        self._last_connection_error = None
        self._consecutive_connection_failures = 0
        self._retry_delay = 1.0
        self._current_retry_delay = 0.0
        self._retry_not_before = 0.0

    def _ensure_recovery_task(self) -> None:
        if self._stopped or self._expected_disconnect:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        task = self._create_background_task(self._run_recovery())
        self._recovery_task = task

        def clear_recovery(done_task: asyncio.Task) -> None:
            if self._recovery_task is done_task:
                self._recovery_task = None

        task.add_done_callback(clear_recovery)

    async def _run_recovery(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopped and not self._expected_disconnect:
            cleanup = self._write_cleanup_task
            if cleanup is not None and not cleanup.done():
                await asyncio.shield(cleanup)
                continue
            delay = max(0.0, self._retry_not_before - loop.time())
            if delay:
                await asyncio.sleep(delay)
            if self._stopped or self._expected_disconnect:
                return
            if self._write_cleanup_task is not None and not self._write_cleanup_task.done():
                continue
            if self._retry_not_before > loop.time():
                continue
            if self._is_connected_and_paired():
                self._record_connection_success()
                return

            task = self._connection_task
            if task is None or task.done():
                task = self._start_connection_attempt()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            return

    async def _reconnect(self) -> None:
        """Request recovery without starting an independent retry loop."""
        if self._stopped or self._expected_disconnect:
            return
        try:
            await self._ensure_connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    @staticmethod
    def _calc_crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte & 255
            for _ in range(8):
                tmp = crc & 1
                crc >>= 1
                if tmp != 0:
                    crc ^= 0xA001
        return crc

    @staticmethod
    def _pack_int(value: int) -> bytearray:
        curr_byte: int
        result = bytearray()
        while True:
            curr_byte = value & 0x7F
            value >>= 7
            if value != 0:
                curr_byte |= 0x80
            result += pack(">B", curr_byte)
            if value == 0:
                break
        return result

    @staticmethod
    def _unpack_int(data: bytes, start_pos: int) -> tuple(int, int):
        result: int = 0
        offset: int = 0
        while offset < 5:
            pos: int = start_pos + offset
            if pos >= len(data):
                raise TuyaBLEDataFormatError()
            curr_byte: int = data[pos]
            result |= (curr_byte & 0x7F) << (offset * 7)
            offset += 1
            if (curr_byte & 0x80) == 0:
                break
        if offset > 4:
            raise TuyaBLEDataFormatError()
        else:
            return (result, start_pos + offset)

    def _build_packets(
        self,
        seq_num: int,
        code: TuyaBLECode,
        data: bytes,
        response_to: int = 0,
    ) -> list[bytes]:
        key: bytes
        iv = secrets.token_bytes(16)
        security_flag: bytes
        if code == TuyaBLECode.FUN_SENDER_DEVICE_INFO:
            key = self._login_key
            security_flag = b"\x04"
        else:
            key = self._session_key
            security_flag = b"\x05"

        raw = bytearray()
        raw += pack(">IIHH", seq_num, response_to, code.value, len(data))
        raw += data
        crc = self._calc_crc16(raw)
        raw += pack(">H", crc)
        while len(raw) % 16 != 0:
            raw += b"\x00"

        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = security_flag + iv + cipher.encrypt(raw)

        command = []
        packet_num = 0
        pos = 0
        length = len(encrypted)
        while pos < length:
            packet = bytearray()
            packet += self._pack_int(packet_num)

            if packet_num == 0:
                packet += self._pack_int(length)
                packet += pack(">B", self._protocol_version << 4)

            data_part = encrypted[
                pos:pos + GATT_MTU - len(packet)  # fmt: skip
            ]
            packet += data_part
            command.append(packet)

            pos += len(data_part)
            packet_num += 1

        return command

    async def _get_seq_num(self) -> int:
        async with self._seq_num_lock:
            result = self._current_seq_num
            self._current_seq_num += 1
        return result

    async def _send_packet(
        self,
        code: TuyaBLECode,
        data: bytes,
        wait_for_response: bool = True,
        # retry: int | None = None,
    ) -> None:
        """Send packet to device and optional read response."""
        if self._stopped or self._expected_disconnect:
            raise BleakError("Device is stopped")
        await self._ensure_connected()
        if self._stopped or self._expected_disconnect:
            raise BleakError("Device is stopped")
        if not await self._send_packet_while_connected(
            code, data, 0, wait_for_response
        ):
            raise BleakError(f"No response received for {code.name}")

    async def _send_response(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
    ) -> None:
        """Send response to received packet."""
        if (
            not self._stopped
            and not self._expected_disconnect
            and self._client
            and self._client.is_connected
        ):
            await self._send_packet_while_connected(code, data, response_to, False)

    async def _send_packet_while_connected(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
        wait_for_response: bool,
        # retry: int | None = None
    ) -> bool:
        """Send packet to device and optional read response."""
        result = True
        future: asyncio.Future | None = None
        seq_num = await self._get_seq_num()
        if wait_for_response:
            future = asyncio.Future()
            self._input_expected_responses[seq_num] = future

        if response_to > 0:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s in response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s",
                self.address,
                seq_num,
                code.name,
            )
        packets: list[bytes] = self._build_packets(
            seq_num, code, data, response_to)
        try:
            await self._int_send_packet_while_connected(packets)
            if future:
                await asyncio.wait_for(future, RESPONSE_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.error(
                "%s: timeout receiving response, RSSI: %s",
                self.address,
                self.rssi,
            )
            result = False
        finally:
            self._input_expected_responses.pop(seq_num, None)
            if future is not None and not future.done():
                future.cancel()

        return result

    async def _int_send_packet_while_connected(
        self,
        packets: list[bytes],
    ) -> None:
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, "
                "waiting for it to complete; RSSI: %s",
                self.address,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._send_packets_locked(packets)
            except asyncio.CancelledError:
                raise
            except BLEAK_EXCEPTIONS as error:
                _LOGGER.warning(
                    "%s: BLE communication failed: %s",
                    self.address,
                    error,
                )
                raise

    async def _send_packets_locked(self, packets: list[bytes]) -> None:
        """Send command to device and read response."""
        await self._int_send_packets_locked(packets)

    async def _cleanup_after_write_failure(
        self, client: BleakClientWithServiceCache, error: Exception
    ) -> None:
        try:
            await self._cleanup_client(client)
        finally:
            if not self._stopped and not self._expected_disconnect:
                if self._retry_not_before:
                    self._retry_not_before = max(
                        self._retry_not_before,
                        asyncio.get_running_loop().time() + self._current_retry_delay,
                    )
                    self._ensure_recovery_task()
                else:
                    self._queue_recovery(error)

    def _start_write_failure_cleanup(
        self, client: BleakClientWithServiceCache, error: Exception
    ) -> asyncio.Task[None]:
        task = self._write_cleanup_task
        if task is None or task.done():
            task = self._create_background_task(
                self._cleanup_after_write_failure(client, error)
            )
            self._write_cleanup_task = task

            def clear_cleanup(done_task: asyncio.Task) -> None:
                if self._write_cleanup_task is done_task:
                    self._write_cleanup_task = None

            task.add_done_callback(clear_cleanup)
        return task

    async def _int_send_packets_locked(self, packets: list[bytes]) -> None:
        """Execute command and read response."""
        for packet in packets:
            client = self._client
            if client:
                try:
                    await client.write_gatt_char(
                        CHARACTERISTIC_WRITE,
                        packet,
                        False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning(
                        "%s: Error during BLE packet write: %s",
                        self.address,
                        error,
                    )
                    if asyncio.current_task() is not self._connection_task:
                        cleanup = self._start_write_failure_cleanup(client, error)
                        if self._client is client:
                            self._disconnected(client, schedule_recovery=False)
                        await asyncio.shield(cleanup)
                    raise BleakError("BLE packet write failed") from error
            else:
                error = BleakError("Client disconnected during packet write")
                if self._connection_task is None or self._connection_task.done():
                    self._queue_recovery(error)
                raise error

    def _get_key(self, security_flag: int) -> bytes:
        if security_flag == 1:
            return self._auth_key
        if security_flag == 4:
            return self._login_key
        elif security_flag == 5:
            return self._session_key
        else:
            pass

    def _parse_timestamp(self, data: bytes, start_pos: int) -> tuple(float, int):
        timestamp: float
        pos = start_pos
        if pos >= len(data):
            raise TuyaBLEDataLengthError()
        time_type = data[pos]
        pos += 1
        end_pos = pos
        match time_type:
            case 0:
                end_pos += 13
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int(data[pos:end_pos].decode()) / 1000
                pass
            case 1:
                end_pos += 4
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int.from_bytes(data[pos:end_pos], "big") * 1.0
                pass
            case _:
                raise TuyaBLEDataFormatError()

        _LOGGER.debug(
            "%s: Received timestamp: %s",
            self.address,
            time.ctime(timestamp),
        )
        return (timestamp, end_pos)

    def _parse_datapoints_v3(
        self, timestamp: float, flags: int, data: bytes, start_pos: int
    ) -> int:
        datapoints: list[TuyaBLEDataPoint] = []

        pos = start_pos
        while len(data) - pos >= 4:
            id: int = data[pos]
            pos += 1
            _type: int = data[pos]
            if _type > TuyaBLEDataPointType.DT_BITMAP.value:
                raise TuyaBLEDataFormatError()
            type: TuyaBLEDataPointType = TuyaBLEDataPointType(_type)
            pos += 1
            data_len: int = data[pos]
            pos += 1
            next_pos = pos + data_len
            if next_pos > len(data):
                raise TuyaBLEDataLengthError()
            raw_value = data[pos:next_pos]
            match type:
                case (TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP):
                    value = raw_value
                case TuyaBLEDataPointType.DT_BOOL:
                    value = int.from_bytes(raw_value, "big") != 0
                case (TuyaBLEDataPointType.DT_VALUE | TuyaBLEDataPointType.DT_ENUM):
                    value = int.from_bytes(raw_value, "big", signed=True)
                case TuyaBLEDataPointType.DT_STRING:
                    value = raw_value.decode()

            _LOGGER.debug(
                "%s: Received datapoint update, id: %s, type: %s: value: %s",
                self.address,
                id,
                type.name,
                value,
            )
            self._datapoints._update_from_device(
                id, timestamp, flags, type, value)
            datapoints.append(self._datapoints[id])
            pos = next_pos

        self._fire_callbacks(datapoints)

    def _handle_command_or_response(
        self, seq_num: int, response_to: int, code: TuyaBLECode, data: bytes
    ) -> None:
        result: int = 0

        match code:
            case TuyaBLECode.FUN_SENDER_DEVICE_INFO:
                if len(data) < 46:
                    raise TuyaBLEDataLengthError()

                self._device_version = ("%s.%s") % (data[0], data[1])
                self._protocol_version_str = ("%s.%s") % (data[2], data[3])
                self._hardware_version = ("%s.%s") % (data[12], data[13])

                self._protocol_version = data[2]
                self._flags = data[4]
                self._is_bound = data[5] != 0

                srand = data[6:12]
                self._session_key = hashlib.md5(
                    self._local_key + srand).digest()
                self._auth_key = data[14:46]

            case TuyaBLECode.FUN_SENDER_PAIR:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]
                if result == 2:
                    _LOGGER.debug(
                        "%s: Device is already paired",
                        self.address,
                    )
                    result = 0
                self._is_paired = result == 0

            case TuyaBLECode.FUN_SENDER_DEVICE_STATUS:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]

            case TuyaBLECode.FUN_RECEIVE_TIME1_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                timestamp = int(time.time_ns() / 1000000)
                timezone = -int(time.timezone / 36)
                data = str(timestamp).encode() + pack(">h", timezone)
                self._create_background_task(
                    self._send_response(code, data, seq_num)
                )

            case TuyaBLECode.FUN_RECEIVE_TIME2_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                time_str: time.struct_time = time.localtime()
                timezone = -int(time.timezone / 36)
                data = pack(
                    ">BBBBBBBh",
                    time_str.tm_year % 100,
                    time_str.tm_mon,
                    time_str.tm_mday,
                    time_str.tm_hour,
                    time_str.tm_min,
                    time_str.tm_sec,
                    time_str.tm_wday,
                    timezone,
                )
                self._create_background_task(
                    self._send_response(code, data, seq_num)
                )

            case TuyaBLECode.FUN_RECEIVE_DP:
                self._parse_datapoints_v3(time.time(), 0, data, 0)
                self._create_background_task(
                    self._send_response(code, bytes(0), seq_num)
                )

            case TuyaBLECode.FUN_RECEIVE_SIGN_DP:
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                self._parse_datapoints_v3(time.time(), flags, data, 2)
                data = pack(">HBB", dp_seq_num, flags, 0)
                self._create_background_task(
                    self._send_response(code, data, seq_num)
                )

            case TuyaBLECode.FUN_RECEIVE_TIME_DP:
                timestamp: float
                pos: int
                timestamp, pos = self._parse_timestamp(data, 0)
                self._parse_datapoints_v3(timestamp, 0, data, pos)
                self._create_background_task(
                    self._send_response(code, bytes(0), seq_num)
                )

            case TuyaBLECode.FUN_RECEIVE_SIGN_TIME_DP:
                timestamp: float
                pos: int
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                timestamp, pos = self._parse_timestamp(data, 3)
                self._parse_datapoints_v3(time.time(), flags, data, pos)
                data = pack(">HBB", dp_seq_num, flags, 0)
                self._create_background_task(
                    self._send_response(code, data, seq_num)
                )

            case _:
                if response_to == 0:
                    _LOGGER.debug(
                        "%s: Unhandled message %s, data %s",
                        self.address,
                        code.name,
                        data.hex(),
                    )

        if response_to != 0:
            future = self._input_expected_responses.pop(response_to, None)
            if future and not future.done():
                _LOGGER.debug(
                    "%s: Received expected response to #%s, result: %s",
                    self.address,
                    response_to,
                    result,
                )
                if result == 0:
                    future.set_result(result)
                else:
                    future.set_exception(TuyaBLEDeviceError(result))

    def _clean_input(self) -> None:
        self._input_buffer = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0

    def _parse_input(self) -> None:
        security_flag = self._input_buffer[0]
        key = self._get_key(security_flag)
        iv = self._input_buffer[1:17]
        encrypted = self._input_buffer[17:]

        self._clean_input()

        cipher = AES.new(key, AES.MODE_CBC, iv)
        raw = cipher.decrypt(encrypted)

        seq_num: int
        response_to: int
        _code: int
        length: int
        seq_num, response_to, _code, length = unpack(">IIHH", raw[:12])

        data_end_pos = length + 12
        raw_length = len(raw)
        if raw_length < data_end_pos:
            raise TuyaBLEDataLengthError()
        if raw_length > data_end_pos:
            calc_crc = self._calc_crc16(raw[:data_end_pos])
            (data_crc,) = unpack(
                ">H",
                raw[data_end_pos:data_end_pos + 2]  # fmt: skip
            )
            if calc_crc != data_crc:
                raise TuyaBLEDataCRCError()
        data = raw[12:data_end_pos]

        code: TuyaBLECode
        try:
            code = TuyaBLECode(_code)
        except ValueError:
            _LOGGER.debug(
                "%s: Received unknown message: #%s %x, response to #%s, data %s",
                self.address,
                seq_num,
                _code,
                response_to,
                data.hex(),
            )
            return

        if response_to != 0:
            _LOGGER.debug(
                "%s: Received: #%s %s, response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Received: #%s %s",
                self.address,
                seq_num,
                code.name,
            )

        self._handle_command_or_response(seq_num, response_to, code, data)

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        _LOGGER.debug("%s: Packet received: %s", self.address, data.hex())

        pos: int = 0
        packet_num: int

        packet_num, pos = self._unpack_int(data, pos)

        if packet_num < self._input_expected_packet_num:
            _LOGGER.error(
                "%s: Unexpcted packet (number %s) in notifications, " "expected %s",
                self.address,
                packet_num,
                self._input_expected_packet_num,
            )
            self._clean_input()

        if packet_num == self._input_expected_packet_num:
            if packet_num == 0:
                self._input_buffer = bytearray()
                self._input_expected_length, pos = self._unpack_int(data, pos)
                pos += 1
            self._input_buffer += data[pos:]
            self._input_expected_packet_num += 1
        else:
            _LOGGER.error(
                "%s: Missing packet (number %s) in notifications, received %s",
                self.address,
                self._input_expected_packet_num,
                packet_num,
            )
            self._clean_input()
            return

        if len(self._input_buffer) > self._input_expected_length:
            _LOGGER.error(
                "%s: Unexpcted length of data in notifications, "
                "received %s expected %s",
                self.address,
                len(self._input_buffer),
                self._input_expected_length,
            )
            self._clean_input()
            return
        elif len(self._input_buffer) == self._input_expected_length:
            self._parse_input()

    async def _send_datapoints_v3(
        self,
        datapoint_ids: list[int],
        values: dict[int, bytes | bool | int | str] | None = None,
    ) -> None:
        """Send new values of datapoints to the device."""
        data = bytearray()
        for dp_id in datapoint_ids:
            dp = self._datapoints._datapoints[dp_id]
            requested_value = values.get(dp_id, dp.value) if values else dp.value
            value = dp._get_value(requested_value)
            _LOGGER.debug(
                "%s: Sending datapoint update, id: %s, type: %s: value: %s",
                self.address,
                dp.id,
                dp.type.name,
                requested_value,
            )
            data += pack(">BBB", dp.id, int(dp.type.value), len(value))
            data += value

        await self._send_packet(TuyaBLECode.FUN_SENDER_DPS, data)

    async def _send_datapoints(
        self,
        datapoint_ids: list[int],
        values: dict[int, bytes | bool | int | str] | None = None,
    ) -> None:
        """Send new values of datapoints to the device."""
        if self._protocol_version == 3:
            await self._send_datapoints_v3(datapoint_ids, values)
        else:
            raise TuyaBLEDeviceError(0)
