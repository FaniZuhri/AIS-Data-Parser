"""Command-line interface: read a marine NMEA stream, emit decoded records.

Reads a log file or stdin line by line and writes one record per input line.
Nothing is ever silently dropped: a malformed line, an unmodelled sentence, an
unknown AIS message type and a half-arrived multi-fragment message all produce
an explicit record.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import IO, Any

from aivdm import __version__
from aivdm.ais import AisFragment, FragmentAssembler, parse_aivdm
from aivdm.gps import decode_sentence, fix_of
from aivdm.messages import decode_payload
from aivdm.nmea import MalformedSentence, Sentence, parse_sentence
from aivdm.store import VesselStore

__all__ = ["main"]

EXIT_OK = 0
EXIT_IO_ERROR = 1
EXIT_USAGE = 2
EXIT_STOPPED_ON_ERROR = 3

_AIS_FORMATTERS = ("VDM", "VDO")

# Columns for --format csv. Each is (header, candidate dotted paths), tried in
# order so one record shape can carry AIS, GPS and vessel rows.
_CSV_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("line_no", ("line_no",)),
    ("rx_time", ("rx_time",)),
    ("kind", ("kind",)),
    ("talker", ("talker",)),
    ("formatter", ("formatter",)),
    ("checksum_ok", ("checksum.ok",)),
    ("mmsi", ("message.mmsi", "vessel.mmsi", "own_mmsi")),
    ("message_type", ("message.message_type", "vessel.dynamic.message_type")),
    ("name", ("vessel.static.name", "message.name")),
    ("call_sign", ("vessel.static.call_sign", "message.call_sign")),
    ("ship_type", ("vessel.static.ship_type", "message.ship_type")),
    ("nav_status", ("message.nav_status", "vessel.dynamic.nav_status")),
    ("lat_deg", ("message.lat_deg", "vessel.dynamic.lat_deg", "own_ship.latitude_deg")),
    ("lon_deg", ("message.lon_deg", "vessel.dynamic.lon_deg", "own_ship.longitude_deg")),
    ("sog_knots", ("message.sog_knots", "vessel.dynamic.sog_knots")),
    ("cog_deg", ("message.cog_deg", "vessel.dynamic.cog_deg")),
    ("heading_deg", ("message.heading_deg", "vessel.dynamic.heading_deg")),
    ("destination", ("vessel.static.destination", "message.destination")),
    ("raw", ("raw",)),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aivdm",
        description="Decode AIVDM/AIVDO and NMEA 0183 GPS sentences from a marine stream.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="NMEA log file, or '-' for stdin (default: stdin)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "csv"),
        default="json",
        help="output format (default: json, i.e. JSON Lines)",
    )
    parser.add_argument(
        "--mode",
        choices=("sentence", "vessel"),
        default="sentence",
        help="one record per input sentence (default), or one per vessel update",
    )
    parser.add_argument(
        "--vessels",
        action="store_true",
        help="after the stream ends, dump the joined vessel table",
    )
    parser.add_argument(
        "--track",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="maintain the per-MMSI vessel store (default: on)",
    )
    parser.add_argument(
        "--checksum",
        choices=("report", "drop", "require"),
        default="report",
        help=(
            "report records the checksum but keeps the sentence (default); "
            "drop discards sentences with a wrong checksum; "
            "require also discards sentences with no checksum at all"
        ),
    )
    parser.add_argument(
        "--exit-on-error",
        action="store_true",
        help="stop at the first malformed sentence and exit 3",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="input encoding (default: utf-8)",
    )
    parser.add_argument(
        "--errors",
        default="replace",
        choices=("strict", "replace", "ignore"),
        help="how to handle undecodable bytes (default: replace)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress the summary on stderr")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print per-line warnings on stderr",
    )
    parser.add_argument("--version", action="version", version=f"aivdm {__version__}")
    return parser.parse_args(argv)


class _Emitter:
    """Writes records in the selected format."""

    def __init__(self, stream: IO[str], fmt: str) -> None:
        self._stream = stream
        self._fmt = fmt
        self._csv_header_written = False
        if fmt == "csv":
            self._csv_writer = csv.writer(stream)

    def emit(self, record: dict[str, Any]) -> None:
        if self._fmt == "json":
            self._stream.write(json.dumps(record, default=str) + "\n")
        elif self._fmt == "csv":
            flat = _flatten(record)
            if not self._csv_header_written:
                self._csv_writer.writerow([header for header, _ in _CSV_FIELDS])
                self._csv_header_written = True
            self._csv_writer.writerow(
                [_format_value(_lookup(flat, paths)) for _, paths in _CSV_FIELDS]
            )
        else:
            self._write_table(record)

    def _write_table(self, record: dict[str, Any]) -> None:
        header = "  ".join(
            part
            for part in (
                f"#{record.get('line_no', '')}",
                str(record.get("rx_time", "")),
                str(record.get("kind", "")).upper(),
                f"{record.get('talker', '')}{record.get('formatter', '')}".strip(),
            )
            if part
        )
        checksum = record.get("checksum")
        if isinstance(checksum, dict):
            header += f"  checksum={_checksum_text(checksum)}"
        if record.get("error"):
            header += f"  error={record['error']}"
        self._stream.write(header + "\n")

        flat = _flatten(record, skip=_HEADER_KEYS)
        if flat:
            width = max(len(key) for key in flat)
            for key, value in flat.items():
                self._stream.write(f"    {key.ljust(width)}  {_format_value(value)}\n")
        self._stream.write("\n")


_HEADER_KEYS = frozenset(
    {"line_no", "rx_time", "kind", "talker", "formatter", "checksum", "error"}
)


def _checksum_text(checksum: dict[str, Any]) -> str:
    if checksum.get("ok") is True:
        return "ok"
    if checksum.get("ok") is False:
        return f"BAD (declared {checksum.get('declared')}, computed {checksum.get('computed')})"
    return "absent"


def _flatten(
    record: dict[str, Any],
    prefix: str = "",
    *,
    skip: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Flatten nested dicts into dotted keys, skipping empty containers."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if not prefix and key in skip:
            continue
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=f"{dotted}.", skip=skip))
        elif isinstance(value, list):
            if not value:
                continue
            if all(not isinstance(item, dict) for item in value):
                out[dotted] = value
            else:
                for index, item in enumerate(value):
                    out.update(_flatten(item, prefix=f"{dotted}[{index}].", skip=skip))
        else:
            out[dotted] = value
    return out


def _lookup(flat: dict[str, Any], paths: tuple[str, ...]) -> Any:
    for path in paths:
        if path in flat and flat[path] is not None:
            return flat[path]
    return None


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "|".join(_format_value(item) for item in value)
    return str(value)


def _checksum_block(sentence: Sentence) -> dict[str, Any]:
    return {
        "declared": sentence.checksum_declared,
        "computed": sentence.checksum_computed,
        "ok": sentence.checksum_ok,
    }


def _fragments_block(fragment: AisFragment) -> dict[str, Any]:
    return {
        "count": fragment.fragment_count,
        "number": fragment.fragment_number,
        "seq_id": fragment.seq_id,
        "channel": fragment.channel,
        "fill_bits": fragment.fill_bits,
        "own_ship": fragment.is_own_ship,
    }


def _envelope(sentence: Sentence, line_no: int, rx_time: str, kind: str) -> dict[str, Any]:
    return {
        "rx_time": rx_time,
        "line_no": line_no,
        "kind": kind,
        "talker": sentence.talker,
        "formatter": sentence.formatter,
        "raw": sentence.raw,
        "checksum": _checksum_block(sentence),
    }


def _error_record(
    *,
    line_no: int,
    rx_time: str,
    raw: str,
    error: str,
    sentence: Sentence | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "rx_time": rx_time,
        "line_no": line_no,
        "kind": "unknown",
        "raw": raw,
        "error": error,
    }
    if sentence is not None:
        record["talker"] = sentence.talker
        record["formatter"] = sentence.formatter
        record["checksum"] = _checksum_block(sentence)
    return record


def _iter_lines(args: argparse.Namespace) -> Iterator[tuple[int, str]]:
    if args.input in ("-", None):
        for line_no, line in enumerate(sys.stdin, start=1):
            yield line_no, line
        return
    with open(args.input, encoding=args.encoding, errors=args.errors) as handle:
        for line_no, line in enumerate(handle, start=1):
            yield line_no, line


def _run(args: argparse.Namespace, emitter: _Emitter) -> int:
    store = VesselStore()
    assembler = FragmentAssembler()

    counted = 0
    skipped = 0
    errors = 0
    malformed_entries: list[str] = []

    def warn(message: str) -> None:
        nonlocal errors
        errors += 1
        malformed_entries.append(message)
        if args.verbose:
            print(f"aivdm: {message}", file=sys.stderr)

    try:
        lines = _iter_lines(args)
        for line_no, line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # A '#' line can never be a valid sentence -- the introducer must be
            # '!' or '$' -- and capture logs commonly carry comment headers, so
            # skip rather than report it as malformed.
            if stripped.startswith("#"):
                continue

            rx_time = _utc_now()

            try:
                sentence = parse_sentence(line)
            except MalformedSentence as error:
                warn(f"line {line_no}: {error}")
                emitter.emit(
                    _error_record(
                        line_no=line_no, rx_time=rx_time, raw=line.rstrip(), error=str(error)
                    )
                )
                if args.exit_on_error:
                    return EXIT_STOPPED_ON_ERROR
                continue

            if args.checksum == "drop" and sentence.checksum_ok is False:
                skipped += 1
                continue
            if args.checksum == "require" and sentence.checksum_ok is not True:
                skipped += 1
                continue

            counted += 1

            if sentence.formatter in _AIS_FORMATTERS:
                record = _handle_ais(args, sentence, line_no, rx_time, assembler, store, warn)
            else:
                record = _handle_gps(args, sentence, line_no, rx_time, store)

            if record is not None:
                emitter.emit(record)

            if args.exit_on_error and errors:
                return EXIT_STOPPED_ON_ERROR
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `| head`); not an error.
        raise SystemExit(EXIT_OK) from None
    except OSError as error:
        print(f"aivdm: {error}", file=sys.stderr)
        return EXIT_IO_ERROR

    if args.vessels and args.track:
        emitter.emit(
            {
                "rx_time": _utc_now(),
                "kind": "snapshot",
                **store.snapshot(),
            }
        )

    if not args.quiet:
        print(
            f"aivdm: {counted} sentence(s) decoded, {skipped} skipped, "
            f"{errors} problem(s), {len(store)} vessel(s)",
            file=sys.stderr,
        )
        for message in malformed_entries[:10]:
            print(f"aivdm:   {message}", file=sys.stderr)

    return EXIT_OK


def _handle_ais(
    args: argparse.Namespace,
    sentence: Sentence,
    line_no: int,
    rx_time: str,
    assembler: FragmentAssembler,
    store: VesselStore,
    warn: Callable[[str], None],
) -> dict[str, Any] | None:
    record = _envelope(sentence, line_no, rx_time, "ais")
    try:
        fragment = parse_aivdm(sentence)
    except MalformedSentence as error:
        warn(f"line {line_no}: {error}")
        record["error"] = str(error)
        return record

    record["fragments"] = _fragments_block(fragment)

    assembled = assembler.push(fragment)
    if assembled is None:
        if args.mode == "vessel":
            # Not a decode yet; vessel mode reports state, not traffic.
            return None
        record["message"] = None
        record["awaiting_fragments"] = True
        return record

    message = decode_payload(assembled.payload, assembled.fill_bits)

    vessel = None
    if args.track:
        vessel = store.update(message, channel=fragment.channel, formatter=fragment.formatter)
        if fragment.is_own_ship:
            store.set_own_mmsi(message.mmsi)

    if args.mode == "vessel":
        if vessel is None:
            return None
        # Emitted even for an identity-only message, so the caller sees a name
        # arrive before any position does.
        return {
            "rx_time": rx_time,
            "line_no": line_no,
            "kind": "vessel",
            "vessel": vessel.to_dict(),
        }

    record["message"] = asdict(message)
    if vessel is not None:
        record["vessel"] = vessel.to_dict()
    return record


def _handle_gps(
    args: argparse.Namespace,
    sentence: Sentence,
    line_no: int,
    rx_time: str,
    store: VesselStore,
) -> dict[str, Any]:
    message = decode_sentence(sentence)

    if message is None:
        # Not modelled, but still not dropped.
        record = _envelope(sentence, line_no, rx_time, "nmea")
        record["fields"] = list(sentence.fields)
        return record

    fix = fix_of(message) if args.track else None
    if fix is not None:
        store.update_own_ship(fix, source=sentence.formatter)

    if args.mode == "vessel":
        if fix is None:
            return {"rx_time": rx_time, "line_no": line_no, "kind": "own_ship", "own_ship": None}
        return {
            "rx_time": rx_time,
            "line_no": line_no,
            "kind": "own_ship",
            "own_ship": None if store.own_ship is None else store.own_ship.to_dict(),
        }

    record = _envelope(sentence, line_no, rx_time, "gps")
    record["message"] = asdict(message)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.mode == "vessel" and not args.track:
        print("aivdm: --mode vessel requires tracking; drop --no-track", file=sys.stderr)
        return EXIT_USAGE
    if args.vessels and not args.track:
        print("aivdm: --vessels requires tracking; drop --no-track", file=sys.stderr)
        return EXIT_USAGE

    emitter = _Emitter(sys.stdout, args.format)
    return _run(args, emitter)
