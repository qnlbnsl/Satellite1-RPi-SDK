# `chain.raw`: raw device-control passthrough

`chain.raw` is a `satellite1d` socket method that forwards one device-control
command, unchanged, to one XMOS resource and returns what the device answered.
It exists for the Hermes chain control resource on the XMOS, whose commands are
defined and versioned by the Pi-side runtime, not by the daemon. The daemon
interprets nothing in the payload or the reply and reports no hash or version.

The transport underneath is the device-control SPI protocol described in
`docs/device-control-spi-protocol.md` in the Satellite1-XMOS repository (protocol
`0x10` and `0x11`). This page covers only what the daemon adds on top.

## Request

```json
{"id": 1, "method": "chain.raw",
 "params": {"resource_id": 233, "command_id": 4, "payload": "0102"}}
{"id": 2, "method": "chain.raw",
 "params": {"resource_id": 233, "command_id": 4, "read_length": 16}}
```

| Param | Type | Range | Meaning |
| --- | --- | --- | --- |
| `resource_id` | integer | 1–255 | Device-control resource. `0` is the core's special resource and is refused |
| `command_id` | integer | 0–127 | Command id without the read bit; the daemon sets bit 7 for a read |
| `payload` | hex string | 0–253 bytes | Present: the call is a **write** of exactly these bytes. `""` is a zero-length write |
| `read_length` | integer | 1–252 | Present: the call is a **read** of this many payload bytes |

Exactly one of `payload` and `read_length` is given. A device-control command
is either a write or a read (bit 7 of the command byte), and a read request
carries no payload on the wire, so one call is one command.

The payload hex must be an even number of hex digits with no separators.

## On the wire

A write is sent as one SPI transfer, then one NOP transfer to clock the status:

```text
write:   [resource_id, command_id, len(payload), payload..., 0 padding]
status:  [00 00 00 00 00 00]  ->  [1, control_ret_t, status register...]
```

Padding appears only when the payload is shorter than three bytes, so the
device can return its whole four-byte status register. The device reads
`len(payload)` bytes and ignores the padding.

A read is sent as the request, then a zero-filled transfer that clocks the reply:

```text
request: [resource_id, 0x80 | command_id, read_length + 1, 0 padding...]
reply:   [23, servicer_status, data[0] .. data[read_length - 1]]   (0x11)
         [x, data[0] .. data[read_length - 1]]                     (0x10)
```

The `+ 1` is the servicer status byte at the head of every read payload, the
same convention `xmos.control_read` uses. The limits follow from the 256-byte
SPI buffer: 3 header bytes leave 253 for a payload, and a read spends one of
them on the status byte, leaving 252.

The write and its status NOP, or the read and its reply transfer, happen under
the daemon's XMOS lock, so no other daemon transfer (button polling, LED
frames) can fall between them.

## Reply

| Call | Result | Meaning |
| --- | --- | --- |
| write | `{"status": n}` | The device's `control_ret_t` for the write, unjudged. `0` is `CONTROL_SUCCESS`; `5` is `CONTROL_BAD_RESOURCE` (no such resource registered) |
| read | `{"data": "<hex>"}` | Exactly `read_length` bytes after the servicer status byte, unchanged |

## Errors

| Error code | Message | When |
| --- | --- | --- |
| `invalid_params` | names the offending param | A param is missing, of the wrong type or out of range, or both or neither of `payload` and `read_length` are given. Nothing is sent |
| `xmos_busy` | `XMOS busy: XMOS firmware flash in progress` or `XMOS busy: XMOS reset in progress` | A flash or reset holds the bus, or is waiting for it. The call is refused at once instead of queueing behind it; nothing is sent |
| `hardware_failure` | `hardware operation failed` | The XMOS is unavailable (for example after a failed flash), the device did not answer or kept ignoring the transfer, a write's status frame did not come back, or (on `0x11`) a read's servicer status byte was non-zero. The daemon log has the cause |

Short transfers the daemon makes itself (button polling every 10 ms, LED
frames) are not "busy": a `chain.raw` call waits for the one in flight and then
runs.

## Known limit

On a `0x11` device, a read addressed to a resource or command the device does
not serve may come back as a status-only frame (`[1, status, ...]`). The
driver's read path accepts that frame as `0x10` framing and returns its bytes as
data, so such a read can answer `{"data": ...}` with bytes that are not a
payload. A write to the same resource reports the problem honestly as
`{"status": 5}`.
