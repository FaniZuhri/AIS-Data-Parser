"""Regression tests over a real capture.

``tests/fixtures/AIS_Test_270726.txt`` is an unmodified 127-line capture: 105
`$GPRMC` sentences at 1 Hz plus 16 `!AIVDM` sentences, recorded in Jakarta Bay
on 2026-07-27. It is real-world data rather than something constructed here, so
it is the strongest available guard against over-fitting to hand-built vectors.

Notable properties this file pins:

* Every one of its 121 sentences passes checksum validation.
* It contains the requester's original sample verbatim (line 36).
* It contains a two-fragment type 5 on channel B with ``seq_id=2`` and fill
  bits of 2, which must reassemble to 424 bits.
* The same type 19 sentence is transmitted four times; each occurrence is
  emitted, because nothing is ever silently dropped.
* One vessel name legitimately ends in ``-99%``; the transmitter really sent
  that, so it must survive stripping untouched.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

from aivdm.cli import EXIT_OK, main

CAPTURE = Path(__file__).parent / "fixtures" / "AIS_Test_270726.txt"

# The requester's sample, which appears verbatim at line 36 of the capture.
REQUESTER_SAMPLE = "!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0*52"


def run_capture(*argv: str, capsys: pytest.CaptureFixture[str]):
    code = main([str(CAPTURE), *argv])
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    return code, records, captured.err


class TestWholeCapture:
    def test_every_sentence_decodes_with_no_problems(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, err = run_capture(capsys=capsys)
        assert code == EXIT_OK
        assert len(records) == 121
        assert "0 problem(s)" in err
        assert "5 vessel(s)" in err

    def test_every_checksum_verifies(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Real captured sentences, not constructed ones: this is an independent
        # check on the checksum implementation.
        _, records, _ = run_capture(capsys=capsys)
        assert all(record["checksum"]["ok"] is True for record in records)

    def test_kind_split(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture(capsys=capsys)
        kinds = [record["kind"] for record in records]
        assert kinds.count("gps") == 105
        assert kinds.count("ais") == 16
        assert len(kinds) == 121  # nothing dropped and nothing invented

    def test_message_types_seen(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture(capsys=capsys)
        types = [
            record["message"]["message_type"]
            for record in records
            if record["kind"] == "ais" and record.get("message")
        ]
        assert sorted(types) == [1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 5, 19, 19, 19, 19]


class TestChannelsAndReassembly:
    def test_channel_b_two_fragment_type5_reassembles(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture(capsys=capsys)
        # One fragment is held, then the message completes 424 bits later.
        held = [r for r in records if r.get("awaiting_fragments")]
        assert len(held) == 1
        assert held[0]["fragments"] == {
            "count": 2,
            "number": 1,
            "seq_id": "2",
            "channel": "B",
            "fill_bits": 0,
            "own_ship": False,
        }

        completed = next(
            r for r in records if (r.get("message") or {}).get("message_type") == 5
        )
        assert completed["fragments"]["channel"] == "B"
        assert completed["fragments"]["fill_bits"] == 2  # taken from the last fragment
        assert completed["message"]["name"] == "NUSANTARA REGAS 1"
        assert completed["message"]["call_sign"] == "POIN"
        assert completed["message"]["destination"] == "FIXED MOORED"
        assert completed["message"]["imo_number"] == 7382744
        assert completed["message"]["draught_m"] == pytest.approx(11.0)
        # This transmitter sends all four dimension fields as 0, which is AIS for
        # "not available" — so they must come out null, not zero.
        assert completed["message"]["dimensions"] == {
            "bow": None,
            "stern": None,
            "port": None,
            "starboard": None,
        }
        assert completed["message"]["epfd_text"] == "Internal GNSS"

    def test_both_channels_are_exercised(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture(capsys=capsys)
        channels = {r["fragments"]["channel"] for r in records if r["kind"] == "ais"}
        assert channels == {"A", "B"}


class TestRepeatedSentences:
    def test_a_retransmitted_sentence_is_emitted_every_time(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Lines 67, 69, 71 and 72 are byte-identical. Deduplicating them would be
        # silently dropping data, so all four must appear.
        duplicated = "!AIVDM,1,1,,A,C7lcMI000qr8F`O8Pk`nKTN04bNjWJT:9QSKkk:0000000D2RRR0,0*38"
        _, records, _ = run_capture(capsys=capsys)
        matches = [record for record in records if record.get("raw") == duplicated]
        assert len(matches) == 4
        assert {record["line_no"] for record in matches} == {67, 69, 71, 72}

    def test_transmitted_name_with_a_status_suffix_is_preserved(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The name field decodes to "BUOYS-RED01-99%" + five '@' pads. The "-99%"
        # is what the transmitter actually sent, so stripping must leave it.
        _, records, _ = run_capture(capsys=capsys)
        message = next(
            record["message"]
            for record in records
            if (record.get("message") or {}).get("mmsi") == 525000036
            and record["message"].get("name")
        )
        assert message["name"] == "BUOYS-RED01-99%"
        assert message["message_type"] == 19
        # Type 19 is 312 bits; the name ends at 263, leaving 49.
        assert message["dimensions"] == {"bow": 5, "stern": 5, "port": 5, "starboard": 5}


class TestOwnShipAndRequesterSample:
    def test_the_requester_sample_is_in_this_capture(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture(capsys=capsys)
        sample = next(record for record in records if record.get("raw") == REQUESTER_SAMPLE)
        assert sample["line_no"] == 36
        assert sample["checksum"] == {"declared": "52", "computed": "52", "ok": True}
        message = sample["message"]
        assert message["message_type"] == 3
        assert message["mmsi"] == 538007503
        assert message["sog_knots"] == pytest.approx(10.6)
        assert message["lat_deg"] == pytest.approx(-5.897428)
        assert message["lon_deg"] == pytest.approx(106.849233)

    def test_own_ship_fix_comes_from_the_last_rmc(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture("--vessels", capsys=capsys)
        snapshot = next(record for record in records if record["kind"] == "snapshot")
        own = snapshot["own_ship"]
        assert own["source"] == "RMC"
        # 105 samples at 1 Hz starting 08:00:16 -> the last is 08:02:00.
        assert own["utc_time"] == "080200.000"
        assert own["latitude_deg"] == pytest.approx(-6.096533)
        assert own["longitude_deg"] == pytest.approx(106.738239)
        # No !AIVDO in this capture, so own ship is identified by GPS only.
        assert snapshot["own_mmsi"] is None

    def test_five_vessels_are_joined(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_capture("--vessels", capsys=capsys)
        snapshot = next(record for record in records if record["kind"] == "snapshot")
        vessels = {vessel["mmsi"]: vessel for vessel in snapshot["vessels"]}
        assert sorted(vessels) == [525000036, 525005002, 525015954, 525100325, 538007503]
        # All Indonesian MIDs (525), as expected for a Jakarta Bay capture.
        assert all(vessel["mmsi_info"]["mid"] == 525 for mmsi, vessel in vessels.items() if mmsi != 538007503)

        regas = vessels[525015954]
        assert regas["static"]["name"] == "NUSANTARA REGAS 1"
        assert regas["static"]["destination"] == "FIXED MOORED"
        assert regas["dynamic"]["nav_status_text"] == "Moored"
        assert regas["dynamic"]["sog_knots"] == pytest.approx(0.0)


class TestInvocationPaths:
    """`main.py` and `python -m aivdm` must stay equivalent."""

    def test_module_entry_point_matches_main_py(self, capsys: pytest.CaptureFixture[str]) -> None:
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "aivdm", str(CAPTURE), "--format", "csv"],
            capture_output=True,
            text=True,
            check=True,
        )
        code = main([str(CAPTURE), "--format", "csv"])
        captured = capsys.readouterr()
        assert code == EXIT_OK

        def rows(text: str) -> list[dict[str, str]]:
            parsed = list(csv.DictReader(io.StringIO(text)))
            # rx_time is the wall clock at read time, so it necessarily differs
            # between two runs. Everything else must be identical.
            for row in parsed:
                row["rx_time"] = "<ts>"
            return parsed

        assert rows(captured.out) == rows(result.stdout)
        assert len(rows(captured.out)) == 121
        assert result.stdout.startswith("line_no,rx_time,kind")


def test_capture_is_not_empty_and_is_plain_text() -> None:
    raw = CAPTURE.read_bytes()
    assert raw and b"\x00" not in raw
    text = raw.decode("utf-8")
    assert text.count("\n") >= 120
    with io.StringIO(text) as handle:
        assert sum(1 for line in handle if line.startswith("$GPRMC")) == 105
