"""NMEA 0183 sentence framing and checksum validation.

Handles both ``$``-introduced GPS sentences and ``!``-introduced AIS sentences.
Only framing lives here; interpreting the fields is ``gps.py`` / ``ais.py``.

Deliberately lenient about the things real receivers get wrong (variable field
counts, absent checksums, over-length vendor sentences) and strict about the
things that would produce a silently wrong reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["MalformedSentence", "Sentence", "checksum", "parse_sentence"]

_INTRODUCERS: Final = ("!", "$")

# The NMEA 0183 line limit is 82 characters, but vendor sentences routinely
# exceed it. Warn-free tolerance beats dropping data; see README.
_MAX_SENTENCE_LEN: Final = 512

_BOM: Final = "\ufeff"


class MalformedSentence(ValueError):
    """The line is not a usable NMEA 0183 sentence."""


@dataclass(frozen=True, slots=True)
class Sentence:
    """A tokenised NMEA 0183 sentence.

    ``talker`` and ``formatter`` are split 2/rest, which handles the standard
    address (``GPGGA`` -> ``GP`` + ``GGA``) and proprietary addresses
    (``PSTT`` -> ``PS`` + ``TT``) with one rule.

    ``checksum_ok`` is ``None`` when the sentence carries no checksum at all,
    so "unverifiable" is never confused with "verified".
    """

    raw: str
    introducer: str
    talker: str
    formatter: str
    fields: tuple[str, ...]
    checksum_declared: str | None
    checksum_computed: str
    checksum_ok: bool | None


def checksum(body: str) -> str:
    """XOR every character of ``body``, formatted as two uppercase hex digits.

    ``body`` is the sentence content between (and excluding) the leading
    ``!``/``$`` and the ``*``.
    """
    value = 0
    for char in body:
        value ^= ord(char)
    return f"{value:02X}"


def parse_sentence(line: str) -> Sentence:
    """Tokenise one NMEA 0183 line.

    Accepts a trailing ``\\n``/``\\r\\n``, surrounding whitespace, a UTF-8 BOM,
    a missing checksum, and lowercase hex checksums.
    """
    raw = line.rstrip("\r\n")
    text = raw.strip()
    if text.startswith(_BOM):
        text = text[1:].lstrip()

    if not text:
        raise MalformedSentence("empty line")
    if len(text) > _MAX_SENTENCE_LEN:
        raise MalformedSentence(
            f"sentence of {len(text)} chars exceeds the {_MAX_SENTENCE_LEN} buffer"
        )

    introducer = text[0]
    if introducer not in _INTRODUCERS:
        raise MalformedSentence(f"expected {' or '.join(_INTRODUCERS)}, got {introducer!r}")

    body = text[1:]

    declared: str | None = None
    star = body.find("*")
    if star >= 0:
        tail = body[star + 1 :].strip().upper()
        # Zero-pad a single hex digit ("*5" -> "05") rather than failing it.
        declared = tail[:2].rjust(2, "0") if tail else None
        body = body[:star]

    computed = checksum(body)

    address, separator, rest = body.partition(",")
    if len(address) < 3:
        raise MalformedSentence(f"no usable talker/formatter in {raw!r}")

    return Sentence(
        raw=raw,
        introducer=introducer,
        talker=address[:2].upper(),
        formatter=address[2:].upper(),
        fields=tuple(rest.split(",")) if separator else (),
        checksum_declared=declared,
        checksum_computed=computed,
        checksum_ok=None if declared is None else declared == computed,
    )
