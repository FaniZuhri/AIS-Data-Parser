"""Tests for the command-line interface and its output writers."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from aivdm.cli import EXIT_OK, EXIT_STOPPED_ON_ERROR, EXIT_USAGE, _Emitter, main

FIXTURES = Path(__file__).parent / "fixtures"
AIS_FIXTURE = FIXTURES / "ais_example.txt"
GPS_FIXTURE = FIXTURES / "gps_example.txt"
MIXED_FIXTURE = FIXTURES / "mixed_stream.txt"

REQUESTER_SAMPLE = "!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0*52"


def run(*argv: str, stdin: str | None = None, capsys: pytest.CaptureFixture[str]):
    if stdin is not None:
        import sys

        sys.stdin = io.StringIO(stdin)
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_lines(*argv: str, stdin: str | None = None, capsys: pytest.CaptureFixture[str]):
    code, out, err = run(*argv, stdin=stdin, capsys=capsys)
    return code, [json.loads(line) for line in out.splitlines() if line.strip()], err


class TestSingleSample:
    def test_sample_from_stdin_decodes(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(stdin=REQUESTER_SAMPLE, capsys=capsys)
        assert code == EXIT_OK
        assert len(records) == 1
        record = records[0]
        assert record["kind"] == "ais"
        assert record["formatter"] == "VDM"
        assert record["checksum"] == {"declared": "52", "computed": "52", "ok": True}
        assert record["fragments"] == {
            "count": 1,
            "number": 1,
            "seq_id": None,
            "channel": "A",
            "fill_bits": 0,
            "own_ship": False,
        }
        message = record["message"]
        assert message["message_type"] == 3
        assert message["mmsi"] == 538007503
        assert message["sog_knots"] == pytest.approx(10.6)
        assert message["lon_deg"] == pytest.approx(106.849233)
        assert message["lat_deg"] == pytest.approx(-5.897428)

    def test_sentence_carries_the_joined_vessel(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(stdin=REQUESTER_SAMPLE, capsys=capsys)
        vessel = records[0]["vessel"]
        assert vessel["mmsi"] == 538007503
        assert vessel["mmsi_info"]["category"] == "ship"
        assert vessel["mmsi_info"]["mid"] == 538
        assert vessel["dynamic"]["sog_knots"] == pytest.approx(10.6)


class TestFixtureStreams:
    def test_ais_fixture_decodes_every_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(str(AIS_FIXTURE), capsys=capsys)
        assert code == EXIT_OK
        # 15 AIS sentences -> 13 messages: the VDM type 5 and the VDO type 5
        # each span two sentences.
        assert len(records) == 15
        completed = [r for r in records if r.get("message")]
        assert len(completed) == 13
        assert all(r["checksum"]["ok"] is True for r in records)

    def test_ais_fixture_contains_own_ship_traffic(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(AIS_FIXTURE), capsys=capsys)
        vdo = [r for r in records if r["formatter"] == "VDO"]
        assert len(vdo) == 3
        assert all(r["fragments"]["own_ship"] is True for r in vdo)
        # ...and the received-traffic lines are not marked as own ship.
        vdm = [r for r in records if r["formatter"] == "VDM"]
        assert all(r["fragments"]["own_ship"] is False for r in vdm)

    def test_gps_fixture_keeps_unmodelled_sentences(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(str(GPS_FIXTURE), capsys=capsys)
        assert code == EXIT_OK
        kinds = [record["kind"] for record in records]
        assert kinds.count("gps") == 8
        # The $INDPT depth sentence is not modelled but must still be reported.
        assert kinds.count("nmea") == 1
        passthrough = next(r for r in records if r["kind"] == "nmea")
        assert passthrough["formatter"] == "DPT"
        assert passthrough["fields"] == ["2.3", "0.0"]

    def test_mixed_fixture_counts(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, err = run_lines(str(MIXED_FIXTURE), capsys=capsys)
        assert code == EXIT_OK
        kinds = [record["kind"] for record in records]
        assert kinds.count("ais") == 10
        assert kinds.count("gps") == 4
        assert kinds.count("nmea") == 1
        assert kinds.count("unknown") == 1  # the line with no introducer
        assert "1 problem(s)" in err

    def test_mixed_fixture_reassembles_across_interleaving(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(MIXED_FIXTURE), capsys=capsys)
        # Both type 5 first-fragments are held, each in its own stream: the
        # second fragment arrives after an intervening GPS sentence.
        held = [r for r in records if r.get("awaiting_fragments")]
        assert len(held) == 2
        assert {r["formatter"] for r in held} == {"VDM", "VDO"}
        assert all(r["fragments"]["number"] == 1 for r in held)
        assert all(r["message"] is None for r in held)

        ever_diadem = next(
            r for r in records if r.get("vessel", {}).get("mmsi") == 367533950
        )
        assert ever_diadem["vessel"]["static"]["name"] == "EVER DIADEM"

    def test_mixed_fixture_keeps_the_two_type5_streams_apart(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end guard against an AIVDM/AIVDO reassembly splice.

        The fixture interleaves two type 5 messages that share a talker, a
        channel and a sequential id. Both must decode to their own vessel.
        """
        _, records, _ = run_lines(str(MIXED_FIXTURE), capsys=capsys)
        latest = {
            record["vessel"]["mmsi"]: record["vessel"]
            for record in records
            if "vessel" in record
        }
        assert latest[367533950]["static"]["name"] == "EVER DIADEM"
        assert latest[525100123]["static"]["name"] == "OWN SHIP"
        # A spliced payload would have leaked one vessel's bits into the other.
        assert latest[367533950]["static"]["call_sign"] == "3FOF8"
        assert latest[525100123]["static"]["call_sign"] == "YD1234"

    def test_mixed_fixture_joins_part_a_and_part_b(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(MIXED_FIXTURE), capsys=capsys)
        # Part B arrives first and Part A second, so the last record for the
        # MMSI is the one that has both halves.
        vessel = [
            record["vessel"]
            for record in records
            if record.get("vessel", {}).get("mmsi") == 367430530
        ][-1]
        assert vessel["static"]["name"] == "TEST VESSEL"
        assert vessel["static"]["call_sign"] == "WDC1234"

    def test_own_ship_fix_is_the_last_one_seen(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(MIXED_FIXTURE), "--vessels", capsys=capsys)
        snapshot = next(r for r in records if r["kind"] == "snapshot")
        # GGA, then GLL, then RMC -- RMC carries the last position in the file.
        assert snapshot["own_ship"]["source"] == "RMC"
        assert snapshot["own_ship"]["latitude_deg"] == pytest.approx(48.1173)
        assert snapshot["own_ship"]["longitude_deg"] == pytest.approx(11.5166667)

    def test_aivdo_sets_the_own_ship_mmsi(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The !AIVDO stream is what identifies own ship; GPS fixes carry no MMSI."""
        _, records, _ = run_lines(str(MIXED_FIXTURE), "--vessels", capsys=capsys)
        snapshot = next(r for r in records if r["kind"] == "snapshot")
        assert snapshot["own_mmsi"] == 525100123

        own = next(v for v in snapshot["vessels"] if v["mmsi"] == 525100123)
        assert own["static"]["name"] == "OWN SHIP"
        assert own["mmsi_info"]["mid"] == 525  # Indonesia


class TestVesselMode:
    def test_emits_one_record_per_vessel_update(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(str(AIS_FIXTURE), "--mode", "vessel", capsys=capsys)
        assert code == EXIT_OK
        assert all(record["kind"] == "vessel" for record in records)
        assert all("vessel" in record for record in records)

    def test_vessel_records_converge_on_the_joined_view(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(AIS_FIXTURE), "--mode", "vessel", capsys=capsys)
        # The last record for a given MMSI is the most complete.
        latest = {record["vessel"]["mmsi"]: record["vessel"] for record in records}
        assert latest[367533950]["static"]["name"] == "EVER DIADEM"
        assert latest[367533950]["static"]["destination"] == "NEW YORK"
        assert latest[367430530]["static"]["call_sign"] == "WDC1234"

    def test_gps_becomes_own_ship_records(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(GPS_FIXTURE), "--mode", "vessel", capsys=capsys)
        own = [record for record in records if record["kind"] == "own_ship"]
        assert len(own) == 8
        positioned = [record for record in own if record["own_ship"]]
        # GGA, RMC, GLL and the multi-constellation GNGGA all carry a fix.
        assert len(positioned) == 4

    def test_requires_tracking(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run("--mode", "vessel", "--no-track", capsys=capsys)
        assert code == EXIT_USAGE
        assert "requires tracking" in err


class TestChecksumPolicy:
    def test_report_keeps_a_bad_checksum(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(MIXED_FIXTURE), capsys=capsys)
        bad = [r for r in records if r.get("checksum", {}).get("ok") is False]
        assert len(bad) == 1
        assert bad[0]["message"]["status"] == "A"  # still decoded

    def test_drop_discards_a_bad_checksum(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(str(MIXED_FIXTURE), "--checksum", "drop", capsys=capsys)
        assert code == EXIT_OK
        assert not [r for r in records if r.get("checksum", {}).get("ok") is False]

    def test_require_also_discards_a_missing_checksum(self, capsys: pytest.CaptureFixture[str]) -> None:
        stdin = "!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0\n"
        _, records, _ = run_lines("--checksum", "require", stdin=stdin, capsys=capsys)
        assert records == []

    def test_report_keeps_a_missing_checksum(self, capsys: pytest.CaptureFixture[str]) -> None:
        stdin = "!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0\n"
        _, records, _ = run_lines(stdin=stdin, capsys=capsys)
        assert len(records) == 1
        assert records[0]["checksum"]["ok"] is None

    def test_nothing_is_silent_quiet_suppresses_only_the_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, records, err = run_lines(str(MIXED_FIXTURE), "--quiet", capsys=capsys)
        assert err == ""
        assert len(records) == 16


class TestExitCodes:
    def test_missing_file_is_an_io_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run("/nonexistent/path/to/log.txt", capsys=capsys)
        assert code == 1
        assert "aivdm:" in err

    def test_exit_on_error_stops_at_the_first_problem(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, records, _ = run_lines(str(MIXED_FIXTURE), "--exit-on-error", capsys=capsys)
        assert code == EXIT_STOPPED_ON_ERROR
        # The two good GPS lines, then the error record for line 10, then stop.
        assert [record["kind"] for record in records] == ["gps", "gps", "unknown"]
        assert records[-1]["line_no"] == 11

    def test_verbose_lists_problems_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, _, err = run(str(MIXED_FIXTURE), "--verbose", capsys=capsys)
        assert "line 11" in err


class TestFormats:
    def test_table_is_not_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run(str(AIS_FIXTURE), "--format", "table", capsys=capsys)
        assert code == EXIT_OK
        assert "message_type" in out
        assert "538007503" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.splitlines()[0])

    def test_csv_has_a_stable_header_and_one_row_per_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run(str(MIXED_FIXTURE), "--format", "csv", capsys=capsys)
        assert code == EXIT_OK
        rows = list(csv.DictReader(io.StringIO(out)))
        assert len(rows) == 16
        assert list(rows[0]) == [
            "line_no",
            "rx_time",
            "kind",
            "talker",
            "formatter",
            "checksum_ok",
            "mmsi",
            "message_type",
            "name",
            "call_sign",
            "ship_type",
            "nav_status",
            "lat_deg",
            "lon_deg",
            "sog_knots",
            "cog_deg",
            "heading_deg",
            "destination",
            "raw",
        ]
        sample = next(row for row in rows if row["mmsi"] == "538007503")
        assert sample["lat_deg"].startswith("-5.897")
        assert sample["sog_knots"] == "10.6"

    def test_csv_vessel_mode_includes_joined_identity(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, out, _ = run(str(AIS_FIXTURE), "--mode", "vessel", "--format", "csv", capsys=capsys)
        rows = list(csv.DictReader(io.StringIO(out)))
        ever = next(row for row in rows if row["mmsi"] == "367533950")
        assert ever["name"] == "EVER DIADEM"
        assert ever["destination"] == "NEW YORK"


class TestOutputSink:
    """-o/--output: the records go to a file, not the terminal."""

    def test_writes_to_a_file_and_leaves_stdout_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "out.jsonl"
        code, out, err = run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        assert code == EXIT_OK
        assert out == ""  # nothing on stdout
        # 15 sentences parse; the 16th is malformed and still counted as a problem.
        assert "15 sentence(s)" in err
        assert "1 problem(s)" in err
        lines = target.read_text().splitlines()
        assert len([line for line in lines if line.strip()]) == 16

    def test_dash_means_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run(str(MIXED_FIXTURE), "-o", "-", capsys=capsys)
        assert code == EXIT_OK
        assert out.count("\n") == 16

    @pytest.mark.parametrize(
        ("suffix", "first_line_start"),
        [
            (".jsonl", '{"rx_time"'),
            (".json", '{"rx_time"'),
            (".ndjson", '{"rx_time"'),
            (".csv", "line_no,rx_time,kind"),
        ],
    )
    def test_format_is_inferred_from_the_extension(
        self, tmp_path: Path, suffix: str, first_line_start: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / f"out{suffix}"
        run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        first_line = target.read_text().splitlines()[0]
        assert first_line.startswith(first_line_start), first_line

    @pytest.mark.parametrize("suffix", [".txt", ".log", ".table"])
    def test_table_extension_selects_the_table_format(
        self, tmp_path: Path, suffix: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / f"out{suffix}"
        run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        first_line = target.read_text().splitlines()[0]
        # A table block header, not JSON and not CSV.
        assert first_line.startswith("#")
        assert "checksum=" in first_line

    def test_unknown_extension_falls_back_to_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "out.dat"
        run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        assert target.read_text().splitlines()[0].startswith('{"rx_time"')

    def test_explicit_format_overrides_the_extension(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "out.csv"
        run(str(MIXED_FIXTURE), "-o", str(target), "--format", "json", capsys=capsys)
        assert target.read_text().splitlines()[0].startswith('{"rx_time"')

    def test_append_adds_to_the_file_without_repeating_the_csv_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "grow.csv"
        run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        first = target.read_text()
        run(str(MIXED_FIXTURE), "-o", str(target), "--append", capsys=capsys)
        appended = target.read_text()

        assert appended.startswith(first)
        assert appended.count("line_no,rx_time,kind") == 1
        assert len(list(csv.DictReader(io.StringIO(appended)))) == 32  # 16 + 16

    def test_overwrites_by_default(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        target = tmp_path / "out.jsonl"
        target.write_text("stale content\n")
        run(str(MIXED_FIXTURE), "-o", str(target), capsys=capsys)
        assert "stale content" not in target.read_text()

    def test_append_without_output_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run(str(MIXED_FIXTURE), "--append", capsys=capsys)
        assert code == EXIT_USAGE
        assert "--append needs --output" in err

    def test_unwritable_output_is_an_io_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run(str(MIXED_FIXTURE), "-o", str(tmp_path / "nope" / "out.jsonl"), capsys=capsys)
        assert code == 1
        assert "aivdm:" in err


class TestSummaryMode:
    """--mode summary: only the final joined picture, one record per vessel."""

    def test_emits_one_record_per_vessel_and_nothing_else(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "ships.jsonl"
        code, _, _ = run(str(MIXED_FIXTURE), "--mode", "summary", "-o", str(target), capsys=capsys)
        assert code == EXIT_OK

        records = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
        kinds = [record["kind"] for record in records]
        # 5 vessels plus own ship, versus 16 records in sentence mode.
        assert len(records) == 6
        assert kinds.count("vessel") == 5
        assert kinds.count("own_ship") == 1
        assert "ais" not in kinds
        assert "gps" not in kinds

    def test_csv_gives_one_row_per_vessel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "ships.csv"
        run(str(MIXED_FIXTURE), "--mode", "summary", "-o", str(target), capsys=capsys)
        rows = list(csv.DictReader(io.StringIO(target.read_text())))
        by_mmsi = {row["mmsi"]: row for row in rows if row["mmsi"]}
        assert len(by_mmsi) == 5
        assert by_mmsi["367533950"]["name"] == "EVER DIADEM"
        assert by_mmsi["525100123"]["name"] == "OWN SHIP"
        assert by_mmsi["993691015"]["name"] == "FORELAND POINT"

    def test_vessels_are_ordered_by_mmsi(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, records, _ = run_lines(str(MIXED_FIXTURE), "--mode", "summary", capsys=capsys)
        mmis = [record["vessel"]["mmsi"] for record in records if record["kind"] == "vessel"]
        assert mmis == sorted(mmis)

    def test_requires_tracking(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run("--mode", "summary", "--no-track", capsys=capsys)
        assert code == EXIT_USAGE
        assert "requires tracking" in err

    def test_summary_of_the_real_capture_is_compact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "ships.csv"
        run(str(FIXTURES / "AIS_Test_270726.txt"), "--mode", "summary", "-o", str(target), capsys=capsys)
        rows = list(csv.DictReader(io.StringIO(target.read_text())))
        # 121 input sentences collapse to 5 vessels plus own ship.
        assert len(rows) == 6


class TestEmitterUnits:
    """The writers are also exercised directly, without the CLI loop."""

    def test_json_emits_one_line_per_record(self) -> None:
        stream = io.StringIO()
        emitter = _Emitter(stream, "json")
        emitter.emit({"kind": "x", "value": 1})
        emitter.emit({"kind": "y", "value": 2})
        assert [json.loads(line)["kind"] for line in stream.getvalue().splitlines()] == ["x", "y"]

    def test_csv_writes_the_header_once(self) -> None:
        stream = io.StringIO()
        emitter = _Emitter(stream, "csv")
        emitter.emit({"kind": "x", "line_no": 1})
        emitter.emit({"kind": "y", "line_no": 2})
        assert stream.getvalue().count("line_no") == 1

    def test_checksum_failure_is_visible_in_table_output(self) -> None:
        stream = io.StringIO()
        _Emitter(stream, "table").emit(
            {"kind": "ais", "line_no": 1, "checksum": {"declared": "00", "computed": "52", "ok": False}}
        )
        assert "BAD" in stream.getvalue()

    def test_nested_structures_are_flattened_for_table_output(self) -> None:
        stream = io.StringIO()
        _Emitter(stream, "table").emit(
            {"kind": "vessel", "vessel": {"mmsi": 1, "static": {"name": "X"}}}
        )
        assert "vessel.static.name" in stream.getvalue()
