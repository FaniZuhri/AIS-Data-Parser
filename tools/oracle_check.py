"""Independent cross-check of the AIS decoders against pyais.

This is a development tool, not part of the package. ``pyais`` is deliberately
NOT a dependency -- the library under test is stdlib-only. Install it in a
throwaway virtualenv outside the project and run this from the repo root:

    python3.11 -m venv /tmp/aivdm-oracle
    /tmp/aivdm-oracle/bin/pip install pyais
    /tmp/aivdm-oracle/bin/python tools/oracle_check.py

Why it exists: pyais is a separate, well-tested implementation with its own
reading of the bit layout. Encoding a message from known field values with
pyais and recovering those values with our decoder means two independent
implementations agree. A single wrong bit offset shows up immediately.

Expected result: every type reports "ok", except type 21, where we deliberately
report vessel dimensions of 0 as null while pyais keeps 0. See AGENTS.md.

Known coverage gaps, so the output is not over-trusted:

* Type 11 cannot be checked here. pyais encodes ``{"type": 11, ...}`` with
  type 4 bits (its MessageType11 subclass does not override the field), so this
  script silently re-tests type 4. Type 11 dispatch is covered by a constructed
  payload in tests/test_messages.py instead.
* Type 24 is not in CASES below; its three variants were verified by hand
  during development and are pinned by tests/test_messages.py.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyais.decode import decode as pyais_decode
from pyais.encode import encode_dict

from aivdm.ais import FragmentAssembler, parse_aivdm
from aivdm.messages import decode_payload
from aivdm.nmea import parse_sentence

# Our JSON key -> pyais key, for fields that carry the same meaning.
KEY_MAP: dict[str, str] = {
    "message_type": "msg_type",
    "nav_status": "status",
    "rot_deg_per_min": "turn",
    "sog_knots": "speed",
    "position_accuracy": "accuracy",
    "lon_deg": "lon",
    "lat_deg": "lat",
    "cog_deg": "course",
    "heading_deg": "heading",
    "utc_second": "second",
    "maneuver": "maneuver",
    "raim": "raim",
    "radio_status": "radio",
    "mmsi": "mmsi",
    "repeat": "repeat",
    "altitude_m": "alt",
    "name": "shipname",
    "call_sign": "callsign",
    "imo_number": "imo",
    "ship_type": "ship_type",
    "draught_m": "draught",
    "destination": "destination",
    "epfd": "epfd",
    "dte": "dte",
    "assigned": "assigned",
    "cs_unit": "cs",
    "has_display": "display",
    "has_dsc": "dsc",
    "can_use_all_bands": "band",
    "accepts_message_22": "msg22",
    "aton_type": "aid_type",
    "off_position": "off_position",
    "virtual_aid": "virtual_aid",
    "gnss_position": "gnss",
    "part_number": "partno",
    "vendor_id": "vendorid",
    "serial_number": "serial",
    "unit_model": "model",
    "year": "year",
    "month": "month",
    "day": "day",
    "hour": "hour",
    "minute": "minute",
}

# Our nested paths -> pyais key.
NESTED_MAP: dict[str, str] = {
    "dimensions.bow": "to_bow",
    "dimensions.stern": "to_stern",
    "dimensions.port": "to_port",
    "dimensions.starboard": "to_starboard",
    "eta.month": "month",
    "eta.day": "day",
    "eta.hour": "hour",
    "eta.minute": "minute",
}

# Absolute tolerances for fields where the two implementations round differently.
TOLERANCE: dict[str, float] = {
    "turn": 0.6,  # pyais rounds ROT to whole deg/min; we keep one decimal
    "speed": 0.05,
    "course": 0.05,
    "lon": 1e-5,
    "lat": 1e-5,
    "draught": 0.05,
}

CASES: list[dict] = [
    {
        "type": 1,
        "repeat": 0,
        "mmsi": 227006760,
        "status": 5,
        "turn": 12.0,
        "speed": 12.3,
        "accuracy": True,
        "lon": -4.4321,
        "lat": 48.7654,
        "course": 123.4,
        "heading": 121,
        "second": 42,
        "maneuver": 1,
        "raim": False,
        "radio": 3040,
    },
    {
        "type": 3,
        "repeat": 0,
        "mmsi": 538007503,
        "status": 0,
        "turn": -20.0,
        "speed": 10.6,
        "accuracy": False,
        "lon": 106.849233,
        "lat": -5.897428,
        "course": 250.2,
        "heading": 263,
        "second": 44,
        "maneuver": 0,
        "raim": False,
        "radio": 3040,
    },
    {
        "type": 4,
        "repeat": 0,
        "mmsi": 3669702,
        "year": 2024,
        "month": 5,
        "day": 15,
        "hour": 14,
        "minute": 30,
        "second": 12,
        "accuracy": True,
        "lon": -70.1234,
        "lat": 42.5678,
        "epfd": 1,
        "raim": False,
        "radio": 0,
    },
    {
        "type": 5,
        "repeat": 0,
        "mmsi": 367533950,
        "ais_version": 0,
        "imo": 9134270,
        "callsign": "3FOF8",
        "shipname": "EVER DIADEM",
        "ship_type": 70,
        "to_bow": 225,
        "to_stern": 70,
        "to_port": 1,
        "to_starboard": 31,
        "epfd": 1,
        "month": 5,
        "day": 15,
        "hour": 14,
        "minute": 30,
        "draught": 12.3,
        "destination": "NEW YORK",
        "dte": False,
    },
    {
        "type": 9,
        "repeat": 0,
        "mmsi": 111232511,
        "alt": 303,
        "speed": 42,
        "accuracy": True,
        "lon": 43.0075,
        "lat": 31.4222,
        "course": 34.5,
        "second": 15,
        "dte": False,
        "assigned": False,
        "raim": False,
        "radio": 33392,
        "reserved_1": 0,
    },
    {
        "type": 11,
        "repeat": 0,
        "mmsi": 3669701,
        "year": 2024,
        "month": 9,
        "day": 17,
        "hour": 6,
        "minute": 5,
        "second": 4,
        "accuracy": False,
        "lon": 106.8492,
        "lat": -5.8974,
        "epfd": 7,
        "raim": True,
        "radio": 0,
    },
    {
        "type": 18,
        "repeat": 0,
        "mmsi": 367430530,
        "speed": 8.7,
        "accuracy": False,
        "lon": -122.345678,
        "lat": 37.812345,
        "course": 210.5,
        "heading": 209,
        "second": 30,
        "cs": True,
        "display": False,
        "dsc": True,
        "band": True,
        "msg22": False,
        "assigned": False,
        "raim": False,
        "radio": 917510,
        "reserved_1": 0,
        "reserved_2": 0,
    },
    {
        "type": 19,
        "repeat": 0,
        "mmsi": 367059850,
        "speed": 5.4,
        "accuracy": True,
        "lon": -94.412345,
        "lat": 29.123456,
        "course": 88.8,
        "heading": 90,
        "second": 7,
        "shipname": "CAPT JOSEPH",
        "ship_type": 52,
        "to_bow": 30,
        "to_stern": 20,
        "to_port": 4,
        "to_starboard": 4,
        "epfd": 1,
        "raim": False,
        "dte": False,
        "assigned": False,
        "reserved_1": 0,
        "reserved_2": 0,
    },
    {
        "type": 21,
        "repeat": 0,
        "mmsi": 993691015,
        "aid_type": 14,
        "name": "FORELAND POINT",
        "accuracy": True,
        "lon": 1.429167,
        "lat": 51.375,
        "to_bow": 0,
        "to_stern": 0,
        "to_port": 0,
        "to_starboard": 0,
        "epfd": 7,
        "second": 55,
        "off_position": False,
        "virtual_aid": False,
        "assigned": False,
        "raim": False,
        "reserved_1": 0,
    },
    {
        "type": 27,
        "repeat": 0,
        "mmsi": 206914217,
        "speed": 57,
        "accuracy": True,
        "lon": 137.016667,
        "lat": 4.833333,
        "course": 167,
        "raim": False,
        "gnss": False,
    },
]


def our_decode(sentences: list[str]) -> dict | None:
    assembler = FragmentAssembler()
    for sentence in sentences:
        fragment = parse_aivdm(parse_sentence(sentence))
        assembled = assembler.push(fragment)
        if assembled is not None:
            message = decode_payload(assembled.payload, assembled.fill_bits)
            flat = dataclasses.asdict(message)
            # Lift nested value objects to dotted keys so they can be compared
            # field by field against pyais's flat output.
            for outer in ("dimensions", "eta"):
                nested = flat.get(outer)
                if isinstance(nested, dict):
                    for key, value in nested.items():
                        flat[f"{outer}.{key}"] = value
            return flat
    return None


def normalise(flat: dict) -> dict:
    out: dict = {}
    for key, value in flat.items():
        out[key] = value
    for ours, theirs in {**KEY_MAP, **NESTED_MAP}.items():
        if ours in flat:
            out[theirs] = flat[ours]
    return out


def compare(ours: dict, theirs: dict) -> tuple[list[str], list[str]]:
    mismatches: list[str] = []
    agreed: list[str] = []
    for key, theirs_value in theirs.items():
        if key not in ours:
            continue
        mine = ours[key]
        if isinstance(mine, bool) or isinstance(theirs_value, bool):
            same = bool(mine) == bool(theirs_value)
        elif isinstance(mine, (int, float)) and isinstance(theirs_value, (int, float)):
            same = abs(float(mine) - float(theirs_value)) <= TOLERANCE.get(key, 0.0)
        else:
            same = str(mine).strip() == str(theirs_value).strip()
        label = f"{key}: ours={mine!r} pyais={theirs_value!r}"
        (agreed if same else mismatches).append(label)
    return agreed, mismatches


def main() -> int:
    failures = 0
    for case in CASES:
        data = dict(case)
        msg_type = data["type"]
        try:
            sentences = encode_dict(data, talker_id="AI", sentence_type="VDM", radio_channel="A")
        except Exception as error:
            print(f"=== type {msg_type}: pyais could not encode: {error}")
            failures += 1
            continue

        ours = our_decode(sentences)
        decoded = pyais_decode(*[s.encode() for s in sentences])
        theirs = json.loads(json.dumps(decoded.asdict(), default=str))

        total_chars = len("".join(sentences))
        print(f"=== type {msg_type}  ({len(sentences)} sentence(s), {total_chars} chars)")
        for sentence in sentences:
            print(f"    {sentence}")
        if ours is None:
            print("    OUR DECODER PRODUCED NOTHING")
            failures += 1
            continue

        agreed, mismatches = compare(normalise(ours), theirs)
        print(f"    our keys : {sorted(ours)}")
        print(f"    pyais keys: {sorted(theirs)}")
        if mismatches:
            print(f"    >>> {len(mismatches)} MISMATCH(ES) / {len(agreed)} agreed")
            for line in mismatches:
                print(f"      {line}")
            failures += 1
        else:
            print(f"    ok: {len(agreed)} shared fields agree")

    print()
    print("FAILURES:", failures)
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
