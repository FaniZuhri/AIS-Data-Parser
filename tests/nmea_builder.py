"""Armoring helpers for tests.

Lets a test state a message as a list of ``(value, width)`` pairs and get a
valid AIVDM payload back. Used only for edge cases that no real reference
vector covers -- sentinel values, truncated variants, and a second position
report for an MMSI that already has a paired vector.

This is a test-local inverse of ``aivdm.bits``, kept out of the package so it
never becomes a maintained public API.
"""

from __future__ import annotations

FieldList = list[tuple[int, int]]


def sixbit(char: str) -> int:
    """AIS 6-bit character value: nibbles 0-31 are '@'..'_', 32-63 are ' '..'?'."""
    code = ord(char)
    return code - 64 if code >= 64 else code


def six(text: str, chars: int | None = None) -> FieldList:
    """Encode text as 6-bit fields, optionally zero-padded to a fixed width."""
    values = [sixbit(char) for char in text]
    if chars is not None:
        values = (values + [0] * chars)[:chars]
    return [(value, 6) for value in values]


def build(fields: FieldList, total_bits: int | None = None) -> tuple[str, int]:
    """Armor ``(value, width)`` pairs, returning ``(payload, fill_bits)``.

    Negative values are encoded as two's complement of the given width.
    """
    bits: list[int] = []
    for value, width in fields:
        if value < 0:
            value += 1 << width
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    if total_bits is not None:
        bits = bits[:total_bits]

    fill = (-len(bits)) % 6
    bits.extend([0] * fill)

    chars = []
    for index in range(0, len(bits), 6):
        value = 0
        for bit in bits[index : index + 6]:
            value = (value << 1) | bit
        chars.append(chr(value + 48 if value < 40 else value + 56))
    return "".join(chars), fill


def position_report(
    *,
    message_type: int = 1,
    repeat: int = 0,
    mmsi: int = 1,
    nav_status: int = 0,
    rot: int = 0,
    sog: int = 0,
    accuracy: int = 0,
    lon: int = 0,
    lat: int = 0,
    cog: int = 0,
    heading: int = 0,
    second: int = 0,
    maneuver: int = 0,
    raim: int = 0,
    radio: int = 0,
) -> FieldList:
    """The full 168-bit common navigation block (AIS types 1, 2 and 3)."""
    return [
        (message_type, 6),
        (repeat, 2),
        (mmsi, 30),
        (nav_status, 4),
        (rot, 8),
        (sog, 10),
        (accuracy, 1),
        (lon, 28),
        (lat, 27),
        (cog, 12),
        (heading, 9),
        (second, 6),
        (maneuver, 2),
        (0, 3),
        (raim, 1),
        (radio, 19),
    ]


def base_station(
    *,
    message_type: int = 4,
    mmsi: int = 1,
    year: int = 0,
    month: int = 0,
    day: int = 0,
    hour: int = 24,
    minute: int = 60,
    second: int = 60,
    accuracy: int = 0,
    lon: int = 0,
    lat: int = 0,
    epfd: int = 0,
    raim: int = 0,
    radio: int = 0,
) -> FieldList:
    """The full 168-bit base station block (AIS types 4 and 11)."""
    return [
        (message_type, 6),
        (0, 2),
        (mmsi, 30),
        (year, 14),
        (month, 4),
        (day, 5),
        (hour, 5),
        (minute, 6),
        (second, 6),
        (accuracy, 1),
        (lon, 28),
        (lat, 27),
        (epfd, 4),
        (0, 10),
        (raim, 1),
        (radio, 19),
    ]


def static_data_part_a(*, mmsi: int, name: str) -> FieldList:
    """AIS type 24 Part A: name only."""
    return [(24, 6), (0, 2), (mmsi, 30), (0, 2), *six(name, 20), (0, 7)]


def class_b_position(
    *,
    mmsi: int,
    sog: int = 0,
    lon: int = 0,
    lat: int = 0,
    cog: int = 0,
    heading: int = 0,
    second: int = 0,
) -> FieldList:
    """The full 168-bit type 18 Standard Class B block."""
    return [
        (18, 6),
        (0, 2),
        (mmsi, 30),
        (0, 8),
        (sog, 10),
        (0, 1),
        (lon, 28),
        (lat, 27),
        (cog, 12),
        (heading, 9),
        (second, 6),
        (0, 2),
        (0, 1),
        (0, 1),
        (0, 1),
        (0, 1),
        (0, 1),
        (0, 1),
        (0, 1),
        (0, 20),
    ]
