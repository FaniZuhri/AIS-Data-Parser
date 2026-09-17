"""Tests for AIVDM framing and multi-fragment reassembly."""

from __future__ import annotations

import pytest

from aivdm.ais import FragmentAssembler, parse_aivdm
from aivdm.nmea import MalformedSentence, parse_sentence

# A real two-sentence type 5, checksum-verified by two independent decoders.
TYPE_5_FIRST = (
    "!AIVDM,2,1,0,A,55NPOOP2;H;s<HtKP00EHE:0@T4@Dl0000000016L961O5GfNNkQEp6ClRh0,0*71"
)
TYPE_5_SECOND = "!AIVDM,2,2,0,A,00000000000,2*24"
TYPE_1 = "!AIVDM,1,1,,A,13HOI:541swceR8Kqmr4lSkDP0gP,0*5E"

# Real AIVDO sentences, checksum-verified. Own ship (MID 525 = Indonesia).
TYPE_1_VDO = "!AIVDO,1,1,,A,17liTFh010Wa7O9t`1qkTBqH0000,0*0B"
TYPE_5_VDO_FIRST = (
    "!AIVDO,2,1,0,A,57liTFh2Fe3uTC7;?@0uLr1<PU000000000000161P6226HbN6BPBhDU0@00,0*47"
)
TYPE_5_VDO_SECOND = "!AIVDO,2,2,0,A,00000000000,2*26"


def fragment(sentence: str):
    return parse_aivdm(parse_sentence(sentence))


class TestParseAivdm:
    def test_extracts_every_field(self) -> None:
        parsed = fragment("!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0*52")
        assert parsed.talker == "AI"
        assert parsed.formatter == "VDM"
        assert parsed.channel == "A"
        assert parsed.seq_id is None
        assert parsed.fragment_count == 1
        assert parsed.fragment_number == 1
        assert parsed.payload == "3815GkhOib7a7O9t`1qqi`?H00gP"
        assert parsed.fill_bits == 0
        assert parsed.is_own_ship is False

    def test_vdo_is_flagged_as_own_ship(self) -> None:
        parsed = fragment(TYPE_1_VDO)
        assert parsed.is_own_ship is True
        assert parsed.formatter == "VDO"
        # The requester's own sample is received traffic, not own ship.
        assert fragment(TYPE_1).is_own_ship is False

    def test_vdo_checksum_verifies(self) -> None:
        for sentence in (TYPE_1_VDO, TYPE_5_VDO_FIRST, TYPE_5_VDO_SECOND):
            assert parse_sentence(sentence).checksum_ok is True

    def test_seq_id_and_channel_b_are_captured(self) -> None:
        parsed = fragment(TYPE_5_FIRST)
        assert parsed.seq_id == "0"
        assert parsed.fragment_count == 2
        assert parsed.fragment_number == 1
        assert parsed.channel == "A"

    def test_numeric_channel_codes_are_accepted(self) -> None:
        assert fragment(TYPE_1.replace(",A,", ",1,")).channel == "1"

    def test_empty_channel_is_accepted(self) -> None:
        assert fragment(TYPE_1.replace(",,A,", ",,,")).channel == ""

    def test_missing_fill_bits_defaults_to_zero(self) -> None:
        # Some feeds omit the field entirely; reading the whole payload is the
        # only safe default.
        parsed = fragment("!AIVDM,1,1,,A,13HOI:541swceR8Kqmr4lSkDP0gP")
        assert parsed.fill_bits == 0

    @pytest.mark.parametrize(
        "sentence",
        [
            "!AIVDM,1,1,,A,payload",  # only 5 fields is fine, but...
            "!AIVDM,1,1,,A,payload,0",  # ...this is the well-formed minimum
        ],
    )
    def test_minimum_well_formed_sentence(self, sentence: str) -> None:
        assert fragment(sentence).payload == "payload"

    def test_non_ais_formatter_is_rejected(self) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"))

    def test_too_few_fields_is_rejected(self) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence("!AIVDM,1,1,,A"))

    def test_empty_payload_is_rejected(self) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence("!AIVDM,1,1,,A,,0"))

    @pytest.mark.parametrize("fields", ["!AIVDM,x,1,,A,p,0", "!AIVDM,1,y,,A,p,0"])
    def test_non_numeric_fragment_counters_are_rejected(self, fields: str) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence(fields))

    @pytest.mark.parametrize("fields", ["!AIVDM,0,1,,A,p,0", "!AIVDM,2,3,,A,p,0", "!AIVDM,2,0,,A,p,0"])
    def test_out_of_range_fragment_numbers_are_rejected(self, fields: str) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence(fields))

    @pytest.mark.parametrize("fields", ["!AIVDM,1,1,,Z,p,0", "!AIVDM,1,1,,A,p,6", "!AIVDM,1,1,,A,p,-1"])
    def test_bad_channel_or_fill_bits_are_rejected(self, fields: str) -> None:
        with pytest.raises(MalformedSentence):
            parse_aivdm(parse_sentence(fields))


class TestReassembly:
    def test_single_fragment_returns_immediately(self) -> None:
        assembler = FragmentAssembler()
        result = assembler.push(fragment(TYPE_1))
        assert result is not None
        assert result.fragment_count == 1
        assert assembler.pending_count == 0

    def test_two_fragment_message_matches_the_concatenation(self) -> None:
        assembler = FragmentAssembler()
        first = fragment(TYPE_5_FIRST)
        second = fragment(TYPE_5_SECOND)

        assert assembler.push(first) is None
        assert assembler.pending_count == 1

        result = assembler.push(second)
        assert result is not None
        assert result.payload == first.payload + second.payload
        assert assembler.pending_count == 0

    def test_fill_bits_come_from_the_last_fragment(self) -> None:
        assembler = FragmentAssembler()
        assembler.push(fragment(TYPE_5_FIRST))
        result = assembler.push(fragment(TYPE_5_SECOND))
        assert result is not None
        # The final fragment carries fill=2; padding sits at the end of the
        # concatenated bit string, so that is the value that governs.
        assert result.fill_bits == 2
        assert len(result.payload) * 6 - result.fill_bits == 424

    def test_inverted_fragment_order_still_assembles(self) -> None:
        assembler = FragmentAssembler()
        second = fragment(TYPE_5_SECOND)
        first = fragment(TYPE_5_FIRST)

        assert assembler.push(second) is None
        result = assembler.push(first)
        assert result is not None
        assert result.payload == first.payload + second.payload

    def test_incomplete_sequence_never_emits(self) -> None:
        assembler = FragmentAssembler()
        assert assembler.push(fragment(TYPE_5_FIRST)) is None
        assert assembler.pending_count == 1

    def test_duplicate_fragment_is_ignored(self) -> None:
        assembler = FragmentAssembler()
        first = fragment(TYPE_5_FIRST)
        assembler.push(first)
        assert assembler.push(first) is None
        result = assembler.push(fragment(TYPE_5_SECOND))
        assert result is not None
        assert result.payload == first.payload + fragment(TYPE_5_SECOND).payload

    def test_channels_do_not_merge(self) -> None:
        assembler = FragmentAssembler()
        on_a = fragment(TYPE_5_FIRST)
        on_b = fragment(TYPE_5_FIRST.replace(",A,", ",B,"))

        assert assembler.push(on_a) is None
        assert assembler.push(on_b) is None
        assert assembler.pending_count == 2

        # Completing channel B must not consume channel A's fragment.
        completed = assembler.push(fragment(TYPE_5_SECOND.replace(",A,", ",B,")))
        assert completed is not None
        assert assembler.pending_count == 1

    def test_different_sequential_ids_do_not_merge(self) -> None:
        assembler = FragmentAssembler()
        assembler.push(fragment(TYPE_5_FIRST))
        assembler.push(fragment(TYPE_5_FIRST.replace(",0,A,", ",7,A,")))
        assert assembler.pending_count == 2

    def test_vdm_and_vdo_do_not_merge(self) -> None:
        """Regression: a received-traffic fragment must never join an own-ship one.

        VDM and VDO share a talker (AI), a channel, and usually a sequential id
        (0). If the formatter is left out of the reassembly key, the second
        stream's fragment 1 is discarded as a duplicate and the *first* arrival
        of fragment 2 completes a payload spliced from two different messages.
        That decodes to confidently wrong data, so it is worse than a drop.
        """
        assembler = FragmentAssembler()

        assert assembler.push(fragment(TYPE_5_FIRST)) is None
        # Own ship's fragment 1 must occupy its own slot, not be swallowed.
        assert assembler.push(fragment(TYPE_5_VDO_FIRST)) is None
        assert assembler.pending_count == 2

        # Each stream completes with its own payload, unmixed.
        vdm = assembler.push(fragment(TYPE_5_SECOND))
        assert vdm is not None
        assert vdm.payload == fragment(TYPE_5_FIRST).payload + fragment(TYPE_5_SECOND).payload

        vdo = assembler.push(fragment(TYPE_5_VDO_SECOND))
        assert vdo is not None
        assert vdo.payload == (
            fragment(TYPE_5_VDO_FIRST).payload + fragment(TYPE_5_VDO_SECOND).payload
        )
        assert vdm.payload != vdo.payload

    def test_interleaved_own_ship_and_traffic_still_decode_correctly(self) -> None:
        """The end-to-end shape of the bug: two vessels, two streams, one pass."""
        from aivdm.messages import StaticVoyageData, decode_payload

        assembler = FragmentAssembler()
        decoded = []
        for sentence in (TYPE_5_FIRST, TYPE_5_VDO_FIRST, TYPE_5_SECOND, TYPE_5_VDO_SECOND):
            result = assembler.push(fragment(sentence))
            if result is not None:
                decoded.append(decode_payload(result.payload, result.fill_bits))

        assert len(decoded) == 2
        assert all(isinstance(message, StaticVoyageData) for message in decoded)
        names = sorted(message.name for message in decoded)  # type: ignore[union-attr]
        assert names == ["EVER DIADEM", "OWN SHIP"]

    def test_unknown_talker_does_not_merge(self) -> None:
        assembler = FragmentAssembler()
        assembler.push(fragment(TYPE_5_FIRST))
        assembler.push(fragment(TYPE_5_FIRST.replace("!AIVDM", "!BSVDM")))
        assert assembler.pending_count == 2

    def test_oldest_pending_is_evicted_at_the_cap(self) -> None:
        # ponytail: the cap exists so a truncated stream cannot grow state
        # without bound. Eviction loses a message; it never splices one.
        assembler = FragmentAssembler(max_pending=2)
        assembler.push(fragment(TYPE_5_FIRST.replace(",0,A,", ",1,A,")))
        assembler.push(fragment(TYPE_5_FIRST.replace(",0,A,", ",2,A,")))
        assert assembler.pending_count == 2

        assembler.push(fragment(TYPE_5_FIRST.replace(",0,A,", ",3,A,")))
        assert assembler.pending_count == 2

        # seq 1 was evicted, so completing it yields nothing.
        assert assembler.push(fragment(TYPE_5_SECOND.replace(",0,A,", ",1,A,"))) is None
        assert assembler.pending_count == 2

    def test_rejects_a_zero_cap(self) -> None:
        with pytest.raises(ValueError):
            FragmentAssembler(max_pending=0)
