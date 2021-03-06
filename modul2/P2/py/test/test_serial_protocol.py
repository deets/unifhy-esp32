# Copyright: 2021, Diez B. Roggisch . All rights reserved.
import serial
import pytest
import operator
import time
import queue
import pathlib

from collections import namedtuple
from functools import reduce, wraps

PORT = "/dev/serial/by-id/usb-FTDI_USB__-__Serial-if00-port0"
BAUD = 1_000_000


RunStats = namedtuple("RunStats", "total_samples avg_timediff max_timediff max_sample")


class ProtocoViolation(Exception):
    pass


def rqads_assert(result, message=None):
    if not result:
        raise ProtocoViolation(message)


def rqads_encode(message):
    if isinstance(message, str):
        message = message.encode("ascii")
    total = b"RQADS" + message
    chksum = reduce(operator.xor, total, 0)
    suffix = f"*{chksum:02x}\r\n".encode("ascii")
    datagram = b"$" + total + suffix
    return datagram


def rqads_decode(line):
    rqads_assert(line.startswith(b"$RQADS"), "Prefix not $RQADS")
    rqads_assert(line.endswith(b"\r\n"), "No CRLF")
    prefix, suffix = line.split(b"*")
    rqads_assert(len(suffix) == 4, "Suffix malformed")  # XX\r\n
    checksum = int(suffix[:2], 16)
    value = reduce(operator.xor, (c for c in prefix[1:]))
    rqads_assert(
        value == checksum,
        f"Checksum Error: {value:02X} != {checksum:02X}"
    )
    return prefix[6:]  # cut off $RQADS


def command(func):
    @wraps(func)
    def _d(self, *a, **k):
        func(self, *a, **k)
        line = self.readline()
        return rqads_decode(line)

    return _d


class SerialConnector:

    SAMPLE_SPEEDS = {
        2.5: 0b00000011,
        5: 0b00010011,
        10: 0b00100011,
        15: 0b00110011,
        25: 0b01000011,
        30: 0b01010011,
        50: 0b01100011,
        60: 0b01110010,
        100: 0b10000010,
        500: 0b10010010,
        1000: 0b10100001,
        2000: 0b10110000,
        3750: 0b11000000,
        7500: 0b11010000,
        15000: 0b11100000,
        30000: 0b11110000,
    }

    def __init__(self, port, baud, read_callback=lambda x: None):
        self._conn = serial.Serial(port, baud)
        self._lines = queue.Queue()
        self._read_callback = read_callback

    @command
    def ping(self):
        self._send("P")

    @command
    def thinning(self, thinning):
        assert thinning in range(0, 256)
        self._send(f"T:{thinning:02X}")

    @command
    def rate(self, samples_per_second):
        value = self.SAMPLE_SPEEDS[samples_per_second]
        self._send(f"R:{value:02X}")

    def start_data_acquisition(self, muxes):
        mux_expression = ":".join(f"{mux:02X}" for mux in muxes)
        self._send(f"C{len(muxes)}:{mux_expression}")

    def stop_data_acquisition(self):
        self._send("S")
        while True:
            # possibly read away buffered lines
            line = rqads_decode(self.readline())
            if line.startswith(b"X"):
                total_samples, avg_timediff, max_timediff, max_sample = [
                    int(p, 16) for p in line.split(b":")[1:]
                ]
                return RunStats(
                    total_samples,
                    avg_timediff,
                    max_timediff,
                    max_sample,
                )

    def listfiles(self):
        res = []
        self._send("L")
        while True:
            answer = rqads_decode(self.readline())
            if answer == b"XXX":
                break
            index, size = [int(part, 16) for part in answer.split(b":")]
            res.append((index, size))
        return res

    def download(self, file_index):
        self._send(f"F:{file_index:03X}")
        file_size = int(rqads_decode(self.readline()).split(b":")[1], 16)
        if file_size:
            print("file_size", file_size)
            # Maybe do this block-wise
            return self._conn.read(file_size)

    def readline(self):
        line = self._conn.readline()
        self._read_callback(line)
        return line

    def _send(self, message):
        self._conn.write(rqads_encode(message))


@pytest.fixture
def conn():
    conn = SerialConnector(PORT, BAUD, print)
    return conn


def test_decoding():
    ping_result = b'$RQADSP*05\r\n'
    assert rqads_decode(ping_result) == b"P"


def test_ping(conn):
    assert conn.ping() == b"P"


def test_thinning(conn):
    assert conn.thinning(64) == b"T"


def test_rate(conn):
    assert conn.rate(7500) == b"R"


def test_file_listing(conn):
    res = conn.listfiles()
    print(res)


def test_download(conn):
    index, _ = conn.listfiles()[1]
    data = conn.download(index)
    assert data is not None
    print(data)


def test_only_cdac(conn):
    duration = 5
    repititions = 1
    conn.thinning(1)
    conn.rate(1000)
    for _ in range(repititions):
        then = time.monotonic()
        conn.start_data_acquisition([0x08])
        while time.monotonic() - then < duration:
            print(conn.readline())
        # for termination and to collect the stats
        print(conn.stop_data_acquisition())


def test_cdac_and_download(conn):
    duration = 10
    old_file_list = set(conn.listfiles())
    conn.thinning(255)
    conn.rate(1000)
    then = time.monotonic()
    conn.start_data_acquisition([0x08])
    while time.monotonic() - then < duration:
        print(conn.readline())
    # for termination and to collect the stats
    print(conn.stop_data_acquisition())
    new_file_list = set(conn.listfiles())
    latest_file = next(iter(new_file_list - old_file_list))
    print(latest_file)
    data = conn.download(latest_file[0])
    pathlib.Path("/tmp/latest-data.log").write_bytes(data)
