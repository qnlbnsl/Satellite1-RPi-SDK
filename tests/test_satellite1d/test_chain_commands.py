import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from satellite1_hw.sat1_hat import XMOS
from satellite1d.adapters.unix_socket import UnixSocketAdapter
from satellite1d.commands import DaemonCommands
from satellite1d.contracts.xmos import XmosMaintenanceError
from satellite1d.services.xmos import XmosService

PAYLOAD_AVAILABLE = 23
BAD_RESOURCE = 5
CHAIN_RESOURCE = 233


class FakeSpi:
    """A control-protocol 0x11 device behind spidev's `xfer2`.

    Each transfer clocks out the frame staged by the previous one, then stages
    the reply to this one: a NOP or a write stages a status-only frame, a read
    stages the payload-available frame from `reads`.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.reads: dict[tuple[int, int], bytes] = {
            (240, 88): bytes([0, 1, 2, 3, 0, 0]),  # DFU get-version
        }
        self.write_status: dict[int, int] = {}
        self._staged = self._status_frame(0)

    def xfer2(self, tx: list[int]) -> list[int]:
        self.sent.append(bytes(tx))
        rx = (self._staged + bytes(len(tx)))[: len(tx)]
        resource_id, command, length = tx[0], tx[1], tx[2]
        if (resource_id, command, length) == (0, 0, 0):
            self._staged = self._status_frame(0)
        elif command & 0x80:
            reply = self.reads.get((resource_id, command & 0x7F))
            self._staged = (
                self._status_frame(BAD_RESOURCE)
                if reply is None
                else bytes([PAYLOAD_AVAILABLE]) + reply
            )
        else:
            self._staged = self._status_frame(self.write_status.get(resource_id, 0))
        return list(rx)

    def close(self) -> None:
        pass

    def chain_requests(self) -> list[bytes]:
        return [tx for tx in self.sent if tx[0] == CHAIN_RESOURCE]

    @staticmethod
    def _status_frame(status: int) -> bytes:
        return bytes([1, status, 1, 0x0F, 0, 0, 0, 0])


class Reset:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.flashing = asyncio.Event()

    async def reset_xmos(self) -> bool:
        return True

    async def set_flash_mode(self) -> bool:
        self.flashing.set()
        await self.release.wait()
        return False

    async def unset_flash_mode(self) -> bool:
        return True


class Events:
    def publish(self, event) -> None:
        pass


def _xmos_with(spi: FakeSpi) -> XMOS:
    driver = XMOS()
    driver._cntrl._spi = spi
    driver._cntrl.open = lambda: setattr(driver._cntrl, "_spi", spi)
    return driver


async def _running(spi: FakeSpi, reset: Reset | None = None):
    service = XmosService(_xmos_with(spi), reset or Reset(), Events())
    await service.start()
    return service, DaemonCommands(object(), object(), object(), service)


def test_chain_raw_write_forwards_the_payload_byte_exactly():
    async def run() -> None:
        spi = FakeSpi()
        service, commands = await _running(spi)
        payload = bytes(range(253))
        try:
            result = await commands.dispatch(
                "chain.raw",
                {
                    "resource_id": CHAIN_RESOURCE,
                    "command_id": 4,
                    "payload": payload.hex(),
                },
            )
            assert result == {"status": 0}
            # One request: resource id, command byte, payload length, payload.
            assert spi.chain_requests() == [bytes([CHAIN_RESOURCE, 4, 253]) + payload]

            empty = await commands.dispatch(
                "chain.raw",
                {"resource_id": CHAIN_RESOURCE, "command_id": 5, "payload": ""},
            )
            assert empty == {"status": 0}
            assert spi.chain_requests()[-1] == bytes([CHAIN_RESOURCE, 5, 0, 0, 0, 0])
        finally:
            await service.close()

    asyncio.run(run())


def test_chain_raw_write_returns_the_device_status_unjudged():
    async def run() -> None:
        spi = FakeSpi()
        spi.write_status[CHAIN_RESOURCE] = BAD_RESOURCE
        service, commands = await _running(spi)
        try:
            result = await commands.dispatch(
                "chain.raw",
                {"resource_id": CHAIN_RESOURCE, "command_id": 1, "payload": "0102"},
            )
            # A button-poll NOP between the write and its status read would have
            # consumed this status and left a success frame in its place.
            assert result == {"status": BAD_RESOURCE}
            # Short writes are zero-padded so the status register clocks in full.
            assert spi.chain_requests() == [bytes([CHAIN_RESOURCE, 1, 2, 1, 2, 0])]
        finally:
            await service.close()

    asyncio.run(run())


def test_chain_raw_read_returns_the_reply_byte_exactly():
    async def run() -> None:
        spi = FakeSpi()
        reply = bytes((i * 7) & 0xFF for i in range(252))
        spi.reads[(CHAIN_RESOURCE, 2)] = bytes([0]) + reply
        service, commands = await _running(spi)
        try:
            result = await commands.dispatch(
                "chain.raw",
                {"resource_id": CHAIN_RESOURCE, "command_id": 2, "read_length": 252},
            )
            assert result == {"data": reply.hex()}
            request = spi.chain_requests()[0]
            assert request[:3] == bytes([CHAIN_RESOURCE, 0x80 | 2, 253])
            assert set(request[3:]) == {0}
        finally:
            await service.close()

    asyncio.run(run())


def test_chain_raw_read_failure_is_a_hardware_error():
    async def run() -> None:
        spi = FakeSpi()
        spi.reads[(CHAIN_RESOURCE, 3)] = bytes([65, 0, 0, 0])  # servicer status
        service, commands = await _running(spi)
        try:
            with pytest.raises(RuntimeError, match="control read failed"):
                await commands.dispatch(
                    "chain.raw",
                    {"resource_id": CHAIN_RESOURCE, "command_id": 3, "read_length": 3},
                )
        finally:
            await service.close()

    asyncio.run(run())


def test_chain_raw_validates_params_before_touching_spi():
    async def run() -> None:
        spi = FakeSpi()
        service, commands = await _running(spi)
        cases = [
            ({"resource_id": 0, "command_id": 1, "payload": ""}, "must be integers"),
            ({"resource_id": 256, "command_id": 1, "payload": ""}, "must be integers"),
            ({"resource_id": True, "command_id": 1, "payload": ""}, "must be integers"),
            (
                {"resource_id": 233, "command_id": 0x81, "payload": ""},
                "must be integers",
            ),
            (
                {"resource_id": 233, "command_id": "1", "payload": ""},
                "must be integers",
            ),
            ({"resource_id": 233, "command_id": 1}, "exactly one"),
            (
                {"resource_id": 233, "command_id": 1, "payload": "", "read_length": 1},
                "exactly one",
            ),
            ({"resource_id": 233, "command_id": 1, "payload": "abc"}, "hex string"),
            ({"resource_id": 233, "command_id": 1, "payload": "zz"}, "hex string"),
            ({"resource_id": 233, "command_id": 1, "payload": "01 02"}, "hex string"),
            ({"resource_id": 233, "command_id": 1, "payload": 12}, "hex string"),
            ({"resource_id": 233, "command_id": 1, "payload": "00" * 254}, "0-253"),
            ({"resource_id": 233, "command_id": 1, "read_length": 0}, "1 to 252"),
            ({"resource_id": 233, "command_id": 1, "read_length": 253}, "1 to 252"),
            ({"resource_id": 233, "command_id": 1, "read_length": False}, "1 to 252"),
        ]
        try:
            for params, message in cases:
                with pytest.raises(ValueError, match=message):
                    await commands.dispatch("chain.raw", params)
            assert spi.chain_requests() == []
        finally:
            await service.close()

    asyncio.run(run())


def test_chain_raw_refuses_with_the_reason_while_a_flash_holds_the_bus():
    async def run() -> None:
        spi = FakeSpi()
        reset = Reset()
        service, commands = await _running(spi, reset)
        flash = asyncio.create_task(service.flash_xmos_firmware(Path("fw.bin")))
        try:
            await reset.flashing.wait()
            for params in (
                {"resource_id": CHAIN_RESOURCE, "command_id": 1, "payload": "01"},
                {"resource_id": CHAIN_RESOURCE, "command_id": 1, "read_length": 1},
            ):
                with pytest.raises(
                    XmosMaintenanceError, match="busy: XMOS firmware flash in progress"
                ):
                    await asyncio.wait_for(commands.dispatch("chain.raw", params), 1)
            assert spi.chain_requests() == []
        finally:
            reset.release.set()
            with pytest.raises(RuntimeError, match="failed to enter"):
                await flash

        result = await commands.dispatch(
            "chain.raw",
            {"resource_id": CHAIN_RESOURCE, "command_id": 1, "payload": "01"},
        )
        assert result == {"status": 0}
        await service.close()

    asyncio.run(run())


def test_chain_raw_refuses_while_a_reset_is_waiting_for_the_bus():
    async def run() -> None:
        spi = FakeSpi()
        service, commands = await _running(spi)
        try:
            async with service._lock:  # a transfer already in flight
                reset = asyncio.create_task(service.reset_xmos())
                await asyncio.sleep(0)
                with pytest.raises(XmosMaintenanceError, match="reset in progress"):
                    await asyncio.wait_for(
                        commands.dispatch(
                            "chain.raw",
                            {
                                "resource_id": CHAIN_RESOURCE,
                                "command_id": 1,
                                "payload": "",
                            },
                        ),
                        1,
                    )
            assert await reset is True
        finally:
            await service.close()

    asyncio.run(run())


def test_server_reports_the_busy_reason_to_the_client():
    class Commands:
        async def dispatch(self, method, params):
            raise XmosMaintenanceError("XMOS busy: XMOS firmware flash in progress")

    async def run() -> None:
        socket_path = Path("/tmp") / f"s1d-{uuid4().hex}.sock"
        server = UnixSocketAdapter(Commands(), socket_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            request = {"id": 1, "method": "chain.raw", "params": {}}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
        assert response == {
            "id": 1,
            "error": {
                "code": "xmos_busy",
                "message": "XMOS busy: XMOS firmware flash in progress",
            },
        }

    asyncio.run(run())
