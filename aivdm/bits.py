"""Bit-level primitives for AIVDM payloads.

Deliberately mechanical: no I/O, no globals, no reflection, no dynamic field
lookup. This module is the direct port target for a Rust ``BitReader`` over a
byte cursor, so every operation here maps to one obvious Rust expression.

The authoritative armoring rule (gpsd, AIVDM/AIVDO):

    "subtract 48 from the ASCII character value; if the result is greater
    than 40 subtract 8."
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = ["BitReader", "ShortPayload", "armor", "sixbit_text"]

# AIS 6-bit ASCII table: nibbles 0-31 render as '@'(64)..'_'(95),
# nibbles 32-63 render as ' '(32)..'?'(63).
_SIXBIT: Final = "".join(chr(n + 64) if n < 32 else chr(n) for n in range(64))

_MIN_FILL_BITS: Final = 0
_MAX_FILL_BITS: Final = 5


class ShortPayload(ValueError):
    """A read would have run past the end of the payload.

    Raised rather than zero-padding: silently short reads are how decoders
    fabricate plausible-but-wrong vessel positions.
    """


def armor(char: str) -> int:
    """Map one AIVDM payload character to its 6-bit value.

    Valid ranges are ``'0'``(48)..``'W'``(87) -> 0..39 and
    ``'`'``(96)..``'w'``(119) -> 40..63. The gap ``'X'``(88)..``'_'``(95) is
    reserved and rejected.
    """
    code = ord(char)
    if 48 <= code <= 87:
        return code - 48
    if 96 <= code <= 119:
        return code - 56
    raise ValueError(f"character {char!r} (U+{code:04X}) is not valid AIVDM armor")


def sixbit_text(nibbles: Iterable[int]) -> str:
    """Decode 6-bit character values to text.

    Stops at the first ``'@'`` (the padding character, value 0), discarding
    anything after it, then strips trailing spaces. Real encoders routinely
    leave garbage after the padding, and some space-fill instead of using
    ``'@'``, so both are handled.
    """
    out: list[str] = []
    for nibble in nibbles:
        if nibble == 0:
            break
        out.append(_SIXBIT[nibble])
    return "".join(out).rstrip(" ")


class BitReader:
    """Cursor over an AIVDM payload's bits, most-significant bit first.

    ``fill_bits`` is the number of padding bits at the end of the payload that
    must not be read (0-5).
    """

    __slots__ = ("_bits", "_pos", "_size")

    def __init__(self, payload: str, fill_bits: int = 0) -> None:
        if not _MIN_FILL_BITS <= fill_bits <= _MAX_FILL_BITS:
            raise ValueError(
                f"fill_bits must be {_MIN_FILL_BITS}..{_MAX_FILL_BITS}, got {fill_bits}"
            )

        bits: list[int] = []
        for char in payload:
            value = armor(char)
            bits.extend((value >> shift) & 1 for shift in (5, 4, 3, 2, 1, 0))

        size = len(bits) - fill_bits
        if size < 0:
            raise ShortPayload(f"payload holds {len(bits)} bits, cannot drop {fill_bits} fill bits")

        self._bits = bits
        self._pos = 0
        self._size = size

    @property
    def size(self) -> int:
        """Total readable bits, excluding fill bits."""
        return self._size

    def remaining(self) -> int:
        """Bits not yet read."""
        return self._size - self._pos

    def u(self, n: int) -> int:
        """Read ``n`` bits as an unsigned integer."""
        if n < 0:
            raise ValueError(f"bit count must be >= 0, got {n}")
        end = self._pos + n
        if end > self._size:
            raise ShortPayload(f"need {n} bits at offset {self._pos}, only {self.remaining()} left")

        value = 0
        bits = self._bits
        for index in range(self._pos, end):
            value = (value << 1) | bits[index]
        self._pos = end
        return value

    def i(self, n: int) -> int:
        """Read ``n`` bits as a two's-complement signed integer."""
        if n < 1:
            raise ValueError(f"signed read needs n >= 1, got {n}")
        value = self.u(n)
        if value & (1 << (n - 1)):
            value -= 1 << n
        return value

    def flag(self) -> bool:
        """Read a single boolean bit."""
        return self.u(1) == 1

    def text(self, n: int) -> str:
        """Read ``n`` bits (a multiple of 6) as 6-bit ASCII text."""
        if n % 6 != 0:
            raise ValueError(f"text width must be a multiple of 6, got {n}")
        return sixbit_text([self.u(6) for _ in range(n // 6)])

    def skip(self, n: int) -> None:
        """Advance ``n`` bits without reading them."""
        if n < 0:
            raise ValueError(f"skip count must be >= 0, got {n}")
        end = self._pos + n
        if end > self._size:
            raise ShortPayload(f"cannot skip {n} bits, only {self.remaining()} left")
        self._pos = end
