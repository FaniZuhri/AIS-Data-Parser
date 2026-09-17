"""Tests for the bit-level primitives."""

from __future__ import annotations

import pytest

from aivdm.bits import BitReader, ShortPayload, armor, sixbit_text


class TestArmor:
    @pytest.mark.parametrize(
        ("char", "expected"),
        [
            ("0", 0),  # start of the low range
            ("W", 39),  # end of the low range
            ("`", 40),  # start of the high range
            ("w", 63),  # end of the high range
            ("1", 1),
            ("P", 32),
        ],
    )
    def test_boundaries_and_samples(self, char: str, expected: int) -> None:
        assert armor(char) == expected

    def test_whole_valid_alphabet_is_a_bijection(self) -> None:
        valid = [chr(c) for c in range(48, 88)] + [chr(c) for c in range(96, 120)]
        assert len(valid) == 64
        assert sorted(armor(c) for c in valid) == list(range(64))

    @pytest.mark.parametrize("char", ["X", "_", " ", "x", "z", "!", "\x00"])
    def test_rejects_reserved_gap_and_junk(self, char: str) -> None:
        with pytest.raises(ValueError):
            armor(char)


class TestSixbitText:
    def test_decodes_letters_and_digits(self) -> None:
        # "ABC 12" -> A=1, B=2, C=3, space=32, 1=49, 2=50
        assert sixbit_text([1, 2, 3, 32, 49, 50]) == "ABC 12"

    def test_stops_at_at_sign_padding(self) -> None:
        assert sixbit_text([1, 2, 0, 5, 6, 7]) == "AB"

    def test_strips_trailing_spaces(self) -> None:
        assert sixbit_text([1, 2, 32, 32, 32]) == "AB"

    def test_empty_and_all_padding(self) -> None:
        assert sixbit_text([]) == ""
        assert sixbit_text([0, 0, 0]) == ""


class TestBitReaderUnsigned:
    def test_msb_first(self) -> None:
        # '1' -> 1 -> 0b000001
        reader = BitReader("1")
        assert reader.u(6) == 1

    def test_max_value(self) -> None:
        # 'w' -> 63 -> 0b111111
        assert BitReader("w").u(6) == 63

    def test_split_reads_across_character_boundaries(self) -> None:
        # "38" -> 3, 8 -> 000011 001000
        reader = BitReader("38")
        assert reader.u(6) == 3
        assert reader.u(2) == 0
        assert reader.u(4) == 8

    def test_reads_a_30_bit_mmsi(self) -> None:
        # Sample sentence payload starts "3815GkhOib7a7O9t`1qqi`?H00gP".
        # message type 3, repeat 0, then MMSI 538007503.
        reader = BitReader("3815GkhOib7a7O9t`1qqi`?H00gP")
        assert reader.u(6) == 3
        assert reader.u(2) == 0
        assert reader.u(30) == 538007503


class TestBitReaderSigned:
    def test_positive(self) -> None:
        # 'P' -> 32 -> 0b100000, but as a 6-bit signed value the top bit is 0 only
        # for values < 32, so read 6 bits of value 16 instead: 'H' -> 24? use '0'+16='@'? explicit:
        # 'H' -> 24 -> 0b011000 -> +24
        assert BitReader("H").i(6) == 24

    def test_negative(self) -> None:
        # 'P' -> 32 -> 0b100000 -> -32
        assert BitReader("P").i(6) == -32

    def test_all_ones_is_minus_one(self) -> None:
        # 'w' -> 63 -> 0b111111 -> -1
        assert BitReader("w").i(6) == -1

    def test_requires_at_least_one_bit(self) -> None:
        with pytest.raises(ValueError):
            BitReader("1").i(0)


class TestBitReaderFillBits:
    def test_fill_bits_shrink_the_readable_window(self) -> None:
        # '1' -> 0b000001, drop 2 fill bits -> 0b0000 readable
        reader = BitReader("1", fill_bits=2)
        assert reader.size == 4
        assert reader.remaining() == 4
        assert reader.u(4) == 0
        with pytest.raises(ShortPayload):
            reader.u(1)

    def test_rejects_out_of_range_fill_bits(self) -> None:
        with pytest.raises(ValueError):
            BitReader("1", fill_bits=6)
        with pytest.raises(ValueError):
            BitReader("1", fill_bits=-1)

    def test_full_payload_is_168_bits_for_a_position_report(self) -> None:
        assert BitReader("3815GkhOib7a7O9t`1qqi`?H00gP").size == 168


class TestBitReaderErrors:
    def test_over_read_raises_short_payload(self) -> None:
        reader = BitReader("1")
        with pytest.raises(ShortPayload):
            reader.u(7)

    def test_over_read_does_not_advance(self) -> None:
        reader = BitReader("1")
        with pytest.raises(ShortPayload):
            reader.u(7)
        assert reader.remaining() == 6
        assert reader.u(6) == 1

    def test_invalid_armor_character_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            BitReader("1X2")


class TestBitReaderFlagSkip:
    def test_flag(self) -> None:
        # 'P' -> 0b100000 -> flag True then five zero bits
        reader = BitReader("P")
        assert reader.flag() is True
        assert reader.u(5) == 0

    def test_skip_advances_without_reading(self) -> None:
        reader = BitReader("38")
        reader.skip(8)
        assert reader.remaining() == 4
        assert reader.u(4) == 8

    def test_skip_past_end_raises(self) -> None:
        with pytest.raises(ShortPayload):
            BitReader("1").skip(7)
