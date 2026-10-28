import struct

class BinaryReader:
    """
    Symphony XTS Binary Packet Stream Reader.
    Unpacks little-endian binary data types from a byte buffer.
    """
    def __init__(self, buffer: bytes):
        self.buffer = buffer if isinstance(buffer, (bytes, bytearray)) else bytes(buffer)
        self.offset = 0

    def read_bytes(self, length: int) -> bytes:
        if self.offset + length > len(self.buffer):
            data = self.buffer[self.offset:]
            self.offset = len(self.buffer)
            return data.ljust(length, b'\x00')
        data = self.buffer[self.offset:self.offset + length]
        self.offset += length
        return data

    def read_int8(self) -> int:
        b = self.read_bytes(1)
        return struct.unpack('<b', b)[0]

    def read_uint8(self) -> int:
        b = self.read_bytes(1)
        return struct.unpack('<B', b)[0]

    def read_int16(self) -> int:
        b = self.read_bytes(2)
        return struct.unpack('<h', b)[0]

    def read_uint16(self) -> int:
        b = self.read_bytes(2)
        return struct.unpack('<H', b)[0]

    def read_int32(self) -> int:
        b = self.read_bytes(4)
        return struct.unpack('<i', b)[0]

    def read_uint32(self) -> int:
        b = self.read_bytes(4)
        return struct.unpack('<I', b)[0]

    def read_int64(self) -> int:
        b = self.read_bytes(8)
        return struct.unpack('<q', b)[0]

    def read_uint64(self) -> int:
        b = self.read_bytes(8)
        return struct.unpack('<Q', b)[0]

    def read_float(self) -> float:
        b = self.read_bytes(4)
        return struct.unpack('<f', b)[0]

    def read_double(self) -> float:
        b = self.read_bytes(8)
        return struct.unpack('<d', b)[0]

    def read_string(self, length: int, encoding: str = 'utf-8') -> str:
        b = self.read_bytes(length)
        return b.decode(encoding, errors='ignore').rstrip('\x00')
