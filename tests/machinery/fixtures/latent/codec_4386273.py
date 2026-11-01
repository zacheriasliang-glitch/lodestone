"""Bounded exact wire codec for a learned three-coordinate bottleneck.

This module learns nothing. Encoders supply three signed four-bit coordinates;
the deployed decoder is a complete, charged 32 KiB reconstruction table. A
residual restores every byte. Exact recovery is correctness, not compression
benefit. Callers must establish decoder/model provenance separately.
"""
from __future__ import annotations

import hashlib
import re
import struct

BLOCK_BYTES = 8
TABLE_BYTES = 4096 * BLOCK_BYTES
MAX_INPUT = 65536
MAX_PACKET = 67000
MAX_LEVEL = 1
HEADER = struct.Struct(">4sBBI32s32s")
MAGIC = b"LTC1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("expected lowercase SHA-256")
    return value


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid " + name)
    return value


def _table(raw):
    if type(raw) is not bytes or len(raw) != TABLE_BYTES:
        raise ValueError("decoder table must contain exactly 4096 eight-byte rows")
    return raw


def code_index(coordinates):
    if type(coordinates) not in (list, tuple) or len(coordinates) != 3:
        raise ValueError("exactly three coordinates required")
    a, b, c = [_integer(x, -8, 7, "coordinate") + 8 for x in coordinates]
    return (a << 8) | (b << 4) | c


def coordinates(code):
    _integer(code, 0, 4095, "code")
    return ((code >> 8) - 8, ((code >> 4) & 15) - 8, (code & 15) - 8)


def _nibbles(raw):
    return [part for byte in raw for part in (byte >> 4, byte & 15)]


def _bytes(values):
    return bytes((values[i] << 4) | values[i + 1] for i in range(0, len(values), 2))


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.buffer = self.width = self.bits = 0

    def put(self, value, width):
        self.buffer = (self.buffer << width) | value
        self.width += width
        self.bits += width
        while self.width >= 8:
            self.width -= 8
            self.data.append((self.buffer >> self.width) & 255)
        self.buffer &= (1 << self.width) - 1

    def finish(self):
        if self.width:
            self.data.append(self.buffer << (8 - self.width))
        return bytes(self.data)


class _Reader:
    def __init__(self, raw):
        self.raw, self.position = raw, 0

    def get(self, width):
        if self.position + width > len(self.raw) * 8:
            raise ValueError("truncated codec payload")
        value = 0
        for _ in range(width):
            value = (value << 1) | ((self.raw[self.position // 8] >>
                                    (7 - self.position % 8)) & 1)
            self.position += 1
        return value

    def finish(self):
        padding = (-self.position) % 8
        if self.position + padding != len(self.raw) * 8:
            raise ValueError("trailing codec bytes")
        if padding and self.get(padding):
            raise ValueError("nonzero padding")


def fold(raw, codes, decoder, *, level=0):
    """Encode one bounded source; literal ties win. No original-file access."""
    if type(raw) is not bytes or len(raw) > MAX_INPUT:
        raise ValueError("source must be bounded bytes")
    _integer(level, 0, MAX_LEVEL, "level")
    decoder = _table(decoder)
    count = len(raw) // BLOCK_BYTES
    if type(codes) not in (list, tuple) or len(codes) != count:
        raise ValueError("one coordinate triple per complete block required")
    indices = [code_index(q) for q in codes]
    out = _Writer()
    stats = dict(full_blocks=count, learned_blocks=0, literal_blocks=0,
                 mode_bits=count, latent_bits=0, residual_mask_bits=0,
                 residual_value_bits=0, literal_bits=0,
                 tail_bits=(len(raw) % BLOCK_BYTES) * 8)
    for i, index in enumerate(indices):
        block = raw[8 * i:8 * (i + 1)]
        actual = _nibbles(block)
        predicted = _nibbles(decoder[index * 8:(index + 1) * 8])
        mismatches = [j for j in range(16) if actual[j] != predicted[j]]
        if len(mismatches) <= 8:
            out.put(1, 1)
            out.put(index, 12)
            out.put(sum(1 << (15 - j) for j in mismatches), 16)
            for j in mismatches:
                out.put(actual[j], 4)
            stats["learned_blocks"] += 1
            stats["latent_bits"] += 12
            stats["residual_mask_bits"] += 16
            stats["residual_value_bits"] += 4 * len(mismatches)
        else:
            out.put(0, 1)
            for byte in block:
                out.put(byte, 8)
            stats["literal_blocks"] += 1
            stats["literal_bits"] += 64
    for byte in raw[count * 8:]:
        out.put(byte, 8)
    meaningful_bits = out.bits
    payload = out.finish()
    packet = HEADER.pack(MAGIC, level, 0, len(raw), bytes.fromhex(_sha(decoder)),
                         bytes.fromhex(_sha(raw))) + payload
    assert len(packet) <= MAX_PACKET
    stats.update(header_bytes=HEADER.size, payload_bits=meaningful_bits,
                 padding_bits=len(payload) * 8 - meaningful_bits,
                 packet_bytes=len(packet), decoder_bytes=TABLE_BYTES,
                 original_bytes=len(raw), level=level)
    return {"packet": packet, "accounting": stats}


def unfold(packet, decoder, *, expected_sha256, expected_size, expected_level=0):
    """Recover using packet and table only, checked against external source pins."""
    _hash(expected_sha256)
    _integer(expected_size, 0, MAX_INPUT, "expected size")
    _integer(expected_level, 0, MAX_LEVEL, "expected level")
    if type(packet) is not bytes or not HEADER.size <= len(packet) <= MAX_PACKET:
        raise ValueError("invalid packet length")
    decoder = _table(decoder)
    magic, level, reserved, size, table_hash, source_hash = HEADER.unpack_from(packet)
    if (magic != MAGIC or reserved != 0 or level != expected_level or
            size != expected_size or source_hash.hex() != expected_sha256 or
            table_hash.hex() != _sha(decoder)):
        raise ValueError("packet identity or decoder mismatch")
    reader, raw = _Reader(packet[HEADER.size:]), bytearray()
    for _ in range(size // 8):
        if reader.get(1) == 0:
            raw.extend(reader.get(8) for _ in range(8))
        else:
            index, mask = reader.get(12), reader.get(16)
            if mask.bit_count() > 8:
                raise ValueError("noncanonical learned block: literal should win")
            values = _nibbles(decoder[index * 8:(index + 1) * 8])
            for j in range(16):
                if mask & (1 << (15 - j)):
                    value = reader.get(4)
                    if value == values[j]:
                        raise ValueError("redundant residual replacement")
                    values[j] = value
            raw.extend(_bytes(values))
    raw.extend(reader.get(8) for _ in range(size % 8))
    reader.finish()
    result = bytes(raw)
    if _sha(result) != expected_sha256:
        raise ValueError("reconstructed source hash mismatch")
    return result


def fold_layers(raw, encoders, decoders):
    """Transform bytes, then optionally transform the first stored representation.

    Encoders receive only eight source bytes at a time. This is a mechanism for
    composition, not evidence that a learned parent model has been trained.
    """
    if (type(raw) is not bytes or len(raw) > 32768 or
            type(encoders) not in (list, tuple) or type(decoders) not in (list, tuple) or
            not 1 <= len(encoders) <= 2 or len(encoders) != len(decoders)):
        raise ValueError("invalid source or layer configuration")
    for encoder, decoder in zip(encoders, decoders):
        if not callable(encoder):
            raise ValueError("encoder must be callable")
        _table(decoder)
    current, layers = raw, []
    for level, (encoder, decoder) in enumerate(zip(encoders, decoders)):
        codes = [encoder(current[i:i + 8]) for i in range(0, len(current) - 7, 8)]
        encoded = fold(current, codes, decoder, level=level)
        layers.append({"level": level, "input_sha256": _sha(current),
                       "input_bytes": len(current), "decoder_sha256": _sha(decoder),
                       "packet_sha256": _sha(encoded["packet"]),
                       "packet_bytes": len(encoded["packet"]),
                       "encoder_calls": len(codes), "accounting": encoded["accounting"]})
        current = encoded["packet"]
    return {"packet": current, "layers": layers}


def unfold_layers(packet, decoders, layers):
    if type(packet) is not bytes or not HEADER.size <= len(packet) <= MAX_PACKET:
        raise ValueError("invalid packet length")
    if (type(layers) is not list or not 1 <= len(layers) <= 2 or
            type(decoders) not in (list, tuple) or len(decoders) != len(layers)):
        raise ValueError("invalid layer manifest")
    current = packet
    for level in reversed(range(len(layers))):
        entry = layers[level]
        required = {"level", "packet_sha256", "packet_bytes", "decoder_sha256",
                    "input_sha256", "input_bytes"}
        if type(entry) is not dict or not required <= set(entry):
            raise ValueError("incomplete layer manifest")
        _integer(entry["level"], 0, MAX_LEVEL, "level")
        _integer(entry["packet_bytes"], HEADER.size, MAX_PACKET, "packet bytes")
        _hash(entry["packet_sha256"])
        _hash(entry["decoder_sha256"])
        if (entry["level"] != level or entry["packet_sha256"] != _sha(current) or
                entry["packet_bytes"] != len(current) or
                entry["decoder_sha256"] != _sha(_table(decoders[level]))):
            raise ValueError("layer identity mismatch")
        current = unfold(current, decoders[level], expected_sha256=entry["input_sha256"],
                         expected_size=entry["input_bytes"], expected_level=level)
    return current
