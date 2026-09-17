"""AIS lookup tables.

Pure data plus tiny pure functions. Kept separate from the decoders so the
tables are trivially diffable against ITU-R M.1371 and so the Rust port can
move them into a ``tables.rs`` verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ATON_TYPE",
    "EPFD",
    "MANEUVER",
    "MMSI_CATEGORY",
    "NAV_STATUS",
    "MmsiInfo",
    "aton_type_text",
    "epfd_text",
    "maneuver_text",
    "mmsi_info",
    "nav_status_text",
    "ship_type_text",
]

NAV_STATUS: Final[dict[int, str]] = {
    0: "Under way using engine",
    1: "At anchor",
    2: "Not under command",
    3: "Restricted manoeuverability",
    4: "Constrained by her draught",
    5: "Moored",
    6: "Aground",
    7: "Engaged in fishing",
    8: "Under way sailing",
    9: "Reserved for future amendment (HSC)",
    10: "Reserved for future amendment (WIG)",
    11: "Power-driven vessel towing astern (regional use)",
    12: "Power-driven vessel pushing ahead or towing alongside (regional use)",
    13: "Reserved for future use",
    14: "AIS-SART is active",
    15: "Undefined",
}

EPFD: Final[dict[int, str]] = {
    0: "Undefined",
    1: "GPS",
    2: "GLONASS",
    3: "Combined GPS/GLONASS",
    4: "Loran-C",
    5: "Chayka",
    6: "Integrated navigation system",
    7: "Surveyed",
    8: "Galileo",
    9: "Reserved",
    10: "Reserved",
    11: "Reserved",
    12: "Reserved",
    13: "Reserved",
    14: "Reserved",
    15: "Internal GNSS",
}

MANEUVER: Final[dict[int, str]] = {
    0: "Not available",
    1: "No special maneuver",
    2: "Special maneuver",
}

ATON_TYPE: Final[dict[int, str]] = {
    0: "Default, type of aid to navigation not specified",
    1: "Reference point",
    2: "RACON",
    3: "Fixed structure off shore",
    4: "Spare, reserved for future use",
    5: "Light, without sectors",
    6: "Light, with sectors",
    7: "Leading light front",
    8: "Leading light rear",
    9: "Beacon, cardinal N",
    10: "Beacon, cardinal E",
    11: "Beacon, cardinal S",
    12: "Beacon, cardinal W",
    13: "Beacon, port hand",
    14: "Beacon, starboard hand",
    15: "Beacon, preferred channel port hand",
    16: "Beacon, preferred channel starboard hand",
    17: "Beacon, isolated danger",
    18: "Beacon, safe water",
    19: "Beacon, special mark",
    20: "Cardinal mark N",
    21: "Cardinal mark E",
    22: "Cardinal mark S",
    23: "Cardinal mark W",
    24: "Port hand mark",
    25: "Starboard hand mark",
    26: "Preferred channel port hand",
    27: "Preferred channel starboard hand",
    28: "Isolated danger",
    29: "Safe water",
    30: "Special mark",
    31: "Light vessel / LANBY / rigs",
}

_SHIP_TYPE_EXACT: Final[dict[int, str]] = {
    0: "Not available",
    30: "Fishing",
    31: "Towing",
    32: "Towing: length exceeds 200 m or breadth exceeds 25 m",
    33: "Dredging or underwater ops",
    34: "Diving ops",
    35: "Military ops",
    36: "Sailing",
    37: "Pleasure craft",
    50: "Pilot vessel",
    51: "Search and rescue vessel",
    52: "Tug",
    53: "Port tender",
    54: "Anti-pollution equipment",
    55: "Law enforcement",
    56: "Spare - local vessel",
    57: "Spare - local vessel",
    58: "Medical transport",
    59: "Noncombatant ship according to RR Resolution No. 18",
}

_SHIP_TYPE_RESERVED: Final[tuple[tuple[int, int], ...]] = ((1, 19), (38, 39))

# Each family occupies a ten-code block: base = "all ships of this type",
# base+1..base+4 = hazardous category A-D, base+5..base+8 = reserved,
# base+9 = "no additional information".
_SHIP_TYPE_FAMILIES: Final[tuple[tuple[int, str], ...]] = (
    (20, "Wing in ground (WIG)"),
    (40, "High speed craft (HSC)"),
    (60, "Passenger"),
    (70, "Cargo"),
    (80, "Tanker"),
    (90, "Other type"),
)


def nav_status_text(code: int) -> str | None:
    return NAV_STATUS.get(code)


def epfd_text(code: int) -> str | None:
    return EPFD.get(code)


def maneuver_text(code: int) -> str | None:
    return MANEUVER.get(code)


def aton_type_text(code: int) -> str | None:
    return ATON_TYPE.get(code)


def ship_type_text(code: int) -> str | None:
    """Decode the 8-bit ship-and-cargo type.

    Returns ``None`` for codes above 99. ITU-R M.1371 says to treat those like
    code 0, but labelling an out-of-range value "Not available" would hide the
    fact that the transmitter sent something unrecognised; the raw code is
    emitted alongside this text so the caller can decide.
    """
    exact = _SHIP_TYPE_EXACT.get(code)
    if exact is not None:
        return exact

    for low, high in _SHIP_TYPE_RESERVED:
        if low <= code <= high:
            return "Reserved for future use"

    for base, family in _SHIP_TYPE_FAMILIES:
        if base <= code <= base + 9:
            offset = code - base
            if offset == 0:
                return f"{family}, all ships of this type"
            if 1 <= offset <= 4:
                return f"{family}, hazardous category {'ABCD'[offset - 1]}"
            if 5 <= offset <= 8:
                return f"{family}, reserved for future use"
            return f"{family}, no additional information"

    return None


MMSI_CATEGORY: Final[dict[str, str]] = {
    "ship": "Ship station",
    "coast": "Coast station",
    "group": "Group of ships",
    "sar_aircraft": "SAR aircraft",
    "handheld": "Handheld VHF transceiver",
    "auxiliary": "Auxiliary craft associated with a parent ship",
    "aton": "Aid to navigation",
    "sart": "AIS-SART",
    "mob": "Man overboard device",
    "epirb": "EPIRB AIS",
    "unknown": "Unknown",
}


@dataclass(frozen=True, slots=True)
class MmsiInfo:
    """Classification of a 9-digit MMSI.

    ``mid`` is the Maritime Identification Digits (the flag state) when the
    MMSI format carries one. A separate MID -> country table is deliberately
    not bundled here; the integer is emitted so a consumer can map it.
    """

    mmsi: int
    digits: str
    category: str
    category_text: str
    mid: int | None


def _mid(digits: str, start: int) -> int | None:
    chunk = digits[start : start + 3]
    return int(chunk) if chunk.isdigit() else None


def mmsi_info(mmsi: int) -> MmsiInfo:
    """Classify an MMSI by its format prefix.

    Prefixes are checked most-specific-first: ``970``/``972``/``974`` before
    ``99``/``98``, ``111`` before ``1``, and ``00`` before ``0``.
    """
    if not 0 <= mmsi <= 999_999_999:
        return MmsiInfo(mmsi, f"{mmsi}", "unknown", MMSI_CATEGORY["unknown"], None)

    digits = f"{mmsi:09d}"

    if digits.startswith(("970", "972", "974")):
        category = {"970": "sart", "972": "mob", "974": "epirb"}[digits[:3]]
        return MmsiInfo(mmsi, digits, category, MMSI_CATEGORY[category], None)

    if digits.startswith("111"):
        return MmsiInfo(
            mmsi, digits, "sar_aircraft", MMSI_CATEGORY["sar_aircraft"], _mid(digits, 3)
        )

    if digits.startswith("99"):
        return MmsiInfo(mmsi, digits, "aton", MMSI_CATEGORY["aton"], _mid(digits, 2))

    if digits.startswith("98"):
        return MmsiInfo(
            mmsi, digits, "auxiliary", MMSI_CATEGORY["auxiliary"], _mid(digits, 2)
        )

    if digits.startswith("00"):
        return MmsiInfo(mmsi, digits, "coast", MMSI_CATEGORY["coast"], _mid(digits, 2))

    if digits.startswith("0"):
        return MmsiInfo(mmsi, digits, "group", MMSI_CATEGORY["group"], _mid(digits, 1))

    if digits.startswith("1"):
        return MmsiInfo(
            mmsi, digits, "sar_aircraft", MMSI_CATEGORY["sar_aircraft"], _mid(digits, 1)
        )

    if digits.startswith("8"):
        return MmsiInfo(mmsi, digits, "handheld", MMSI_CATEGORY["handheld"], _mid(digits, 1))

    return MmsiInfo(mmsi, digits, "ship", MMSI_CATEGORY["ship"], _mid(digits, 0))
