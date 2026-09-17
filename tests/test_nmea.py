"""Tests for NMEA 0183 framing and checksum validation."""

from __future__ import annotations

import pytest

from aivdm.nmea import MalformedSentence, checksum, parse_sentence

# The requester's sample, and the canonical published NMEA 0183 examples. Their
# checksums are known-good, so asserting checksum_ok pins both this module's
# checksum implementation and the exact transcription of each vector.
SAMPLE_AIVDM = "!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0*52"
KNOWN_GOOD = [
    SAMPLE_AIVDM,
    "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
    "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
    "$GPGLL,4916.45,N,12311.12,W,225444,A*31",
    "$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48",
    "$GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39",
    "$GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00*74",
    "$GPZDA,201530.00,04,07,2002,00,00*60",
]


class TestChecksum:
    def test_xor_of_body(self) -> None:
        assert checksum("GPGLL,4916.45,N,12311.12,W,225444,A") == "31"

    def test_uppercase_two_digits(self) -> None:
        result = checksum("GPGSV,3,1,11,03,03,111,00")
        assert len(result) == 2
        assert result == result.upper()

    @pytest.mark.parametrize("sentence", KNOWN_GOOD)
    def test_known_good_vectors_verify(self, sentence: str) -> None:
        assert parse_sentence(sentence).checksum_ok is True


class TestParseAis:
    def test_full_sample(self) -> None:
        parsed = parse_sentence(SAMPLE_AIVDM)
        assert parsed.introducer == "!"
        assert parsed.talker == "AI"
        assert parsed.formatter == "VDM"
        assert parsed.fields == ("1", "1", "", "A", "3815GkhOib7a7O9t`1qqi`?H00gP", "0")
        assert parsed.checksum_declared == "52"
        assert parsed.checksum_computed == "52"
        assert parsed.checksum_ok is True

    def test_payload_keeps_backticks_intact(self) -> None:
        # Backtick is valid armor (value 40); it must survive framing untouched.
        assert "`" in parse_sentence(SAMPLE_AIVDM).fields[4]

    def test_empty_sequential_message_id_survives(self) -> None:
        assert parse_sentence(SAMPLE_AIVDM).fields[2] == ""


class TestParseGps:
    def test_formatter_split_is_talker_agnostic(self) -> None:
        for talker in ("GP", "GN", "GL", "GA", "GB"):
            parsed = parse_sentence(f"${talker}GGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
            assert parsed.talker == talker
            assert parsed.formatter == "GGA"

    def test_proprietary_address(self) -> None:
        parsed = parse_sentence("$PSTT,hello")
        assert parsed.talker == "PS"
        assert parsed.formatter == "TT"

    def test_trailing_fields_are_kept(self) -> None:
        parsed = parse_sentence("$GNGGA,a,b,c,d,e,f,g,h,i,j,k,l,m,n,extra")
        assert parsed.fields[-1] == "extra"

    def test_short_sentence_yields_short_field_tuple(self) -> None:
        assert parse_sentence("$GPGGA,1,2").fields == ("1", "2")


class TestLeniency:
    def test_crlf_is_stripped(self) -> None:
        assert parse_sentence(SAMPLE_AIVDM + "\r\n").checksum_ok is True

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert parse_sentence(f"  {SAMPLE_AIVDM}  ").checksum_ok is True

    def test_utf8_bom_is_stripped(self) -> None:
        parsed = parse_sentence("\ufeff" + SAMPLE_AIVDM)
        assert parsed.talker == "AI"
        assert parsed.checksum_ok is True

    def test_lowercase_checksum_accepted(self) -> None:
        assert parse_sentence("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6a").checksum_ok is True

    def test_single_hex_digit_is_zero_padded(self) -> None:
        assert parse_sentence("$GPXXX,1*05").checksum_declared == "05"

    def test_missing_checksum_is_none_not_false(self) -> None:
        parsed = parse_sentence("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
        assert parsed.checksum_declared is None
        assert parsed.checksum_ok is None

    def test_over_length_vendor_sentence_is_rejected(self) -> None:
        with pytest.raises(MalformedSentence):
            parse_sentence("$PUBX," + "1" * 600)


class TestErrors:
    def test_wrong_checksum_is_false_not_an_exception(self) -> None:
        assert parse_sentence("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*00").checksum_ok is False

    @pytest.mark.parametrize("line", ["", "   ", "no introducer here", "AIVDM,1,1,,A,x,0*00", "#comment"])
    def test_rejects_non_sentences(self, line: str) -> None:
        with pytest.raises(MalformedSentence):
            parse_sentence(line)

    def test_rejects_address_too_short(self) -> None:
        with pytest.raises(MalformedSentence):
            parse_sentence("$AB,1,2")
