"""AIVDM/AIVDO framing and multi-fragment reassembly.

A type 5 static/voyage message is 424 bits and arrives as two sentences, so
reassembly is not optional for the data the requester actually wants (ship
name, callsign, destination).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from aivdm.nmea import MalformedSentence, Sentence

__all__ = ["AisFragment", "AssembledPayload", "FragmentAssembler", "parse_aivdm"]

_AIS_FORMATTERS: Final = ("VDM", "VDO")
_VALID_CHANNELS: Final = ("", "A", "B", "1", "2")

_MAX_FILL_BITS: Final = 5

# ponytail: fixed ceiling on in-flight reassembly state, oldest evicted first.
# A truncated or malicious stream would otherwise grow this map forever. Sized
# for file/pipe workloads; a long-running daemon would want a TTL instead.
_DEFAULT_MAX_PENDING: Final = 64


@dataclass(frozen=True, slots=True)
class AisFragment:
    """One AIVDM/AIVDO sentence, validated and typed."""

    talker: str
    formatter: str
    channel: str
    seq_id: str | None
    fragment_count: int
    fragment_number: int
    payload: str
    fill_bits: int
    is_own_ship: bool


@dataclass(frozen=True, slots=True)
class AssembledPayload:
    """A complete AIS payload, possibly spanning several sentences."""

    payload: str
    fill_bits: int
    fragment_count: int


def parse_aivdm(sentence: Sentence) -> AisFragment:
    """Validate and extract the AIVDM/AIVDO fields of a sentence.

    Raises :class:`MalformedSentence` for anything that would otherwise produce
    a wrong reading downstream.
    """
    if sentence.formatter not in _AIS_FORMATTERS:
        raise MalformedSentence(f"not an AIVDM/AIVDO sentence: {sentence.raw!r}")

    fields = sentence.fields
    if len(fields) < 5:
        raise MalformedSentence(
            f"AIVDM needs at least 5 fields, got {len(fields)}: {sentence.raw!r}"
        )

    try:
        fragment_count = int(fields[0])
        fragment_number = int(fields[1])
    except ValueError as error:
        raise MalformedSentence(f"non-numeric fragment counter: {sentence.raw!r}") from error

    if fragment_count < 1:
        raise MalformedSentence(f"fragment count {fragment_count} < 1: {sentence.raw!r}")
    if not 1 <= fragment_number <= fragment_count:
        raise MalformedSentence(
            f"fragment {fragment_number} of {fragment_count} is out of range: {sentence.raw!r}"
        )

    channel = fields[3].strip().upper()
    if channel not in _VALID_CHANNELS:
        raise MalformedSentence(f"unexpected radio channel {channel!r}: {sentence.raw!r}")

    payload = fields[4]
    if not payload:
        raise MalformedSentence(f"empty AIVDM payload: {sentence.raw!r}")

    # Fill bits are technically required, but feeds do omit them; 0 is the only
    # safe default because it reads the whole payload.
    fill_bits = 0
    if len(fields) >= 6 and fields[5].strip():
        try:
            fill_bits = int(fields[5])
        except ValueError as error:
            raise MalformedSentence(f"non-numeric fill bits: {sentence.raw!r}") from error
        if not 0 <= fill_bits <= _MAX_FILL_BITS:
            raise MalformedSentence(f"fill bits {fill_bits} outside 0..{_MAX_FILL_BITS}")

    return AisFragment(
        talker=sentence.talker,
        formatter=sentence.formatter,
        channel=channel,
        seq_id=fields[2].strip() or None,
        fragment_count=fragment_count,
        fragment_number=fragment_number,
        payload=payload,
        fill_bits=fill_bits,
        is_own_ship=sentence.formatter == "VDO",
    )


@dataclass(slots=True)
class _Pending:
    """In-flight fragments of one multi-sentence message."""

    parts: dict[int, str]
    count: int
    fill_bits: int


class FragmentAssembler:
    """Collects AIVDM fragments and emits a payload once a message is complete.

    Fragments are correlated on
    ``(talker, formatter, channel, seq_id, fragment_count)``.

    The spec does not name a key. The formatter is part of it because ``VDM``
    and ``VDO`` are two independent message streams that share a talker, a
    channel, and usually a sequential id. Omitting it lets an own-ship fragment
    be absorbed into a received-traffic message, producing a *spliced* payload
    that decodes to confidently wrong data.

    When ``seq_id`` is absent, two interleaved multi-fragment messages on the
    same channel and formatter share a key. There the outcome is a dropped
    message, never a spliced one: a fragment number already present is ignored,
    and parts are concatenated in numeric order.
    """

    def __init__(self, *, max_pending: int = _DEFAULT_MAX_PENDING) -> None:
        if max_pending < 1:
            raise ValueError(f"max_pending must be >= 1, got {max_pending}")
        self._pending: dict[tuple[str, str, str, str | None, int], _Pending] = {}
        self._max_pending = max_pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def push(self, fragment: AisFragment) -> AssembledPayload | None:
        """Add a fragment, returning the payload once every part has arrived."""
        if fragment.fragment_count == 1:
            return AssembledPayload(
                payload=fragment.payload,
                fill_bits=fragment.fill_bits,
                fragment_count=1,
            )

        key = (
            fragment.talker,
            fragment.formatter,
            fragment.channel,
            fragment.seq_id,
            fragment.fragment_count,
        )
        entry = self._pending.get(key)
        if entry is None:
            if len(self._pending) >= self._max_pending:
                del self._pending[next(iter(self._pending))]
            entry = _Pending(parts={}, count=fragment.fragment_count, fill_bits=0)
            self._pending[key] = entry

        if fragment.fragment_number in entry.parts:
            # Duplicate fragment; ignore rather than restart the message.
            return None

        entry.parts[fragment.fragment_number] = fragment.payload
        if fragment.fragment_number == entry.count:
            # Padding sits at the end of the concatenated bit string, so the
            # final fragment's fill-bit count governs the whole message.
            entry.fill_bits = fragment.fill_bits

        if len(entry.parts) < entry.count:
            return None

        del self._pending[key]
        return AssembledPayload(
            payload="".join(entry.parts[number] for number in sorted(entry.parts)),
            fill_bits=entry.fill_bits,
            fragment_count=entry.count,
        )
