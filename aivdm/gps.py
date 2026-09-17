"""NMEA 0183 GPS/GNSS sentence decoders.

One frozen dataclass per sentence formatter. Field names match the JSON keys
emitted by the CLI, and ``dataclasses.asdict`` does the serialisation, so there
is no hand-written mapping layer to drift out of sync.

Two rules govern every decoder here:

1. An absent, empty, or out-of-range field decodes to ``None`` -- never ``0``.
   A fabricated zero is how a decoder reports a vessel in the Gulf of Guinea.
2. Every decoder tolerates a short field list. Receivers truncate sentences.

Talker IDs are deliberately not validated: ``$GPGGA`` and ``$GNGGA`` (and
``GL``/``GA``/``GB``/``BD``/``GI``/``GQ``/``IN``) all reach the same decoder,
matched on the 3-character formatter. Matching the full 5-character address
would silently drop every multi-constellation receiver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from aivdm.nmea import Sentence

__all__ = [
    "Gga",
    "Gll",
    "GpsFix",
    "GpsMessage",
    "GsSatellite",
    "Gsa",
    "Gsv",
    "Hdg",
    "Hdt",
    "Rmc",
    "Rot",
    "Vtg",
    "Zda",
    "decode_sentence",
    "fix_of",
]

_LAT_LIMIT: Final = 90.0
_LON_LIMIT: Final = 180.0

# NMEA carries ddmm.mmmm, i.e. 1/10000 minute, about 0.185 m. Rounding to six
# decimal places (~0.11 m) is finer than the encoding and stops values like
# 48.11729999999999 reaching the output.
_COORD_PRECISION: Final = 6


def _at(fields: tuple[str, ...], index: int) -> str:
    """Field access that tolerates a truncated sentence."""
    return fields[index] if index < len(fields) else ""


def _opt_str(value: str) -> str | None:
    text = value.strip()
    return text or None


def _opt_float(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _opt_int(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _signed(value: float, indicator: str) -> float:
    """Apply an E/W (or N/S) indicator to a magnitude."""
    return value if indicator in ("E", "N") else -value


def _signed_opt(value: float | None, indicator: str) -> float | None:
    """Apply an E/W indicator, refusing to guess when it is absent.

    An empty or unrecognised indicator must not default to either sign --
    treating it as West would silently negate a valid magnetic variation.
    """
    if value is None:
        return None
    text = indicator.strip().upper()
    if text not in ("E", "W"):
        return None
    return value if text == "E" else -value


def _coord(value: str, hemisphere: str, *, limit: float) -> float | None:
    """Convert ``ddmm.mmmm`` / ``dddmm.mmmm`` plus an indicator to degrees.

    NMEA carries no minus signs -- the sign exists only in the indicator field,
    so an empty or unrecognised indicator means the coordinate is unusable
    rather than positive.
    """
    text = value.strip()
    indicator = hemisphere.strip().upper()
    if not text or indicator not in ("N", "S", "E", "W"):
        return None

    try:
        packed = float(text)
    except ValueError:
        return None

    if packed < 0:
        # Signs are not permitted in this field; refuse rather than guess
        # whether the sign or the indicator is authoritative.
        return None

    degrees = int(packed // 100)
    minutes = packed - degrees * 100
    if not 0.0 <= minutes < 60.0:
        return None

    result = _signed(degrees + minutes / 60.0, indicator)
    if not -limit <= result <= limit:
        return None
    return round(result, _COORD_PRECISION)


def _latitude(value: str, hemisphere: str) -> float | None:
    return _coord(value, hemisphere, limit=_LAT_LIMIT)


def _longitude(value: str, hemisphere: str) -> float | None:
    return _coord(value, hemisphere, limit=_LON_LIMIT)


def _iso_date(year: str, month: str, day: str) -> str | None:
    """Build an ISO date from explicit 4-digit year / month / day fields."""
    y, m, d = _opt_int(year), _opt_int(month), _opt_int(day)
    if y is None or m is None or d is None:
        return None
    if not 1 <= m <= 12 or not 1 <= d <= 31:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


@dataclass(frozen=True, slots=True)
class Gga:
    """Global Positioning System Fix Data."""

    utc_time: str | None
    latitude_deg: float | None
    longitude_deg: float | None
    fix_quality: int | None
    fix_quality_text: str | None
    satellites_used: int | None
    hdop: float | None
    altitude_m: float | None
    geoid_separation_m: float | None
    dgps_age_s: float | None
    dgps_station_id: str | None


@dataclass(frozen=True, slots=True)
class Rmc:
    """Recommended Minimum Navigation Information."""

    utc_time: str | None
    utc_date: str | None
    status: str | None
    latitude_deg: float | None
    longitude_deg: float | None
    sog_knots: float | None
    cog_deg: float | None
    magnetic_variation_deg: float | None
    mode: str | None


@dataclass(frozen=True, slots=True)
class Gll:
    """Geographic Position -- Latitude/Longitude."""

    latitude_deg: float | None
    longitude_deg: float | None
    utc_time: str | None
    status: str | None
    mode: str | None


@dataclass(frozen=True, slots=True)
class Vtg:
    """Track Made Good and Ground Speed."""

    cog_true_deg: float | None
    cog_magnetic_deg: float | None
    sog_knots: float | None
    sog_kmh: float | None
    mode: str | None


@dataclass(frozen=True, slots=True)
class Gsa:
    """GNSS DOP and Active Satellites."""

    selection_mode: str | None
    fix_type: int | None
    fix_type_text: str | None
    active_prns: tuple[int, ...]
    pdop: float | None
    hdop: float | None
    vdop: float | None
    system_id: str | None


@dataclass(frozen=True, slots=True)
class GsSatellite:
    """One satellite quadruple from a GSV sentence."""

    prn: int
    elevation_deg: float | None
    azimuth_deg: float | None
    snr_db: float | None


@dataclass(frozen=True, slots=True)
class Gsv:
    """Satellites in View.

    A GSV group spans several sentences; they are paginated by
    ``messages_total`` / ``message_number`` and are emitted one record each.
    """

    messages_total: int | None
    message_number: int | None
    satellites_in_view: int | None
    satellites: tuple[GsSatellite, ...]
    signal_id: int | None


@dataclass(frozen=True, slots=True)
class Zda:
    """Time & Date (UTC)."""

    utc_time: str | None
    utc_date: str | None
    local_zone_hours: int | None
    local_zone_minutes: int | None


@dataclass(frozen=True, slots=True)
class Hdt:
    """Heading, True."""

    heading_true_deg: float | None


@dataclass(frozen=True, slots=True)
class Hdg:
    """Heading, Deviation & Variation."""

    heading_magnetic_deg: float | None
    deviation_deg: float | None
    variation_deg: float | None


@dataclass(frozen=True, slots=True)
class Rot:
    """Rate of Turn."""

    rot_deg_per_min: float | None
    status: str | None


GpsMessage: TypeAlias = Gga | Rmc | Gll | Vtg | Gsa | Gsv | Zda | Hdt | Hdg | Rot

# Sentences that carry an own-ship position.
_POSITION_BEARING: Final = (Gga, Rmc, Gll)

_FIX_QUALITY_TEXT: Final[dict[int, str]] = {
    0: "fix not available",
    1: "GPS fix",
    2: "differential GPS fix",
    3: "PPS fix",
    4: "real time kinematic",
    5: "float RTK",
    6: "estimated (dead reckoning)",
    7: "manual input mode",
    8: "simulation mode",
    9: "SBAS/WAAS",
}

_FIX_TYPE_TEXT: Final[dict[int, str]] = {
    1: "no fix",
    2: "2D fix",
    3: "3D fix",
}


@dataclass(frozen=True, slots=True)
class GpsFix:
    """A usable own-ship position extracted from a GPS sentence."""

    latitude_deg: float
    longitude_deg: float
    utc_time: str | None


def _decode_gga(fields: tuple[str, ...]) -> Gga:
    quality = _opt_int(_at(fields, 5))
    # Quality 0 means "no fix": many receivers still emit coordinates, often
    # 0.000/0.000. Trust the quality flag over the numbers.
    fixed = quality != 0
    return Gga(
        utc_time=_opt_str(_at(fields, 0)),
        latitude_deg=_latitude(_at(fields, 1), _at(fields, 2)) if fixed else None,
        longitude_deg=_longitude(_at(fields, 3), _at(fields, 4)) if fixed else None,
        fix_quality=quality,
        fix_quality_text=_FIX_QUALITY_TEXT.get(quality) if quality is not None else None,
        satellites_used=_opt_int(_at(fields, 6)),
        hdop=_opt_float(_at(fields, 7)),
        altitude_m=_opt_float(_at(fields, 8)),
        geoid_separation_m=_opt_float(_at(fields, 10)),
        dgps_age_s=_opt_float(_at(fields, 12)),
        dgps_station_id=_opt_str(_at(fields, 13)),
    )


def _decode_rmc(fields: tuple[str, ...]) -> Rmc:
    status = _opt_str(_at(fields, 1))
    valid = status != "V"
    return Rmc(
        utc_time=_opt_str(_at(fields, 0)),
        # Kept as the raw ddmmyy from the wire: inventing a century for a
        # 2-digit year is a correctness landmine, not a convenience.
        utc_date=_opt_str(_at(fields, 8)),
        status=status,
        latitude_deg=_latitude(_at(fields, 2), _at(fields, 3)) if valid else None,
        longitude_deg=_longitude(_at(fields, 4), _at(fields, 5)) if valid else None,
        sog_knots=_opt_float(_at(fields, 6)),
        cog_deg=_opt_float(_at(fields, 7)),
        magnetic_variation_deg=_signed_opt(_opt_float(_at(fields, 9)), _at(fields, 10)),
        mode=_opt_str(_at(fields, 11)),
    )


def _decode_gll(fields: tuple[str, ...]) -> Gll:
    status = _opt_str(_at(fields, 5))
    valid = status != "V"
    return Gll(
        latitude_deg=_latitude(_at(fields, 0), _at(fields, 1)) if valid else None,
        longitude_deg=_longitude(_at(fields, 2), _at(fields, 3)) if valid else None,
        utc_time=_opt_str(_at(fields, 4)),
        status=status,
        mode=_opt_str(_at(fields, 6)),
    )


def _decode_vtg(fields: tuple[str, ...]) -> Vtg:
    # NMEA 3.01+ interleaves unit letters; pre-3.01 emitted four bare numbers.
    # Field 2 being a literal 'T' is the documented discriminator.
    if _at(fields, 1).strip().upper() == "T":
        return Vtg(
            cog_true_deg=_opt_float(_at(fields, 0)),
            cog_magnetic_deg=_opt_float(_at(fields, 2)),
            sog_knots=_opt_float(_at(fields, 4)),
            sog_kmh=_opt_float(_at(fields, 6)),
            mode=_opt_str(_at(fields, 8)),
        )
    return Vtg(
        cog_true_deg=_opt_float(_at(fields, 0)),
        cog_magnetic_deg=_opt_float(_at(fields, 1)),
        sog_knots=_opt_float(_at(fields, 2)),
        sog_kmh=_opt_float(_at(fields, 3)),
        mode=None,
    )


def _decode_gsa(fields: tuple[str, ...]) -> Gsa:
    fix_type = _opt_int(_at(fields, 1))
    prns = tuple(prn for prn in (_opt_int(_at(fields, i)) for i in range(2, 14)) if prn is not None)
    return Gsa(
        selection_mode=_opt_str(_at(fields, 0)),
        fix_type=fix_type,
        fix_type_text=_FIX_TYPE_TEXT.get(fix_type) if fix_type is not None else None,
        active_prns=prns,
        pdop=_opt_float(_at(fields, 14)),
        hdop=_opt_float(_at(fields, 15)),
        vdop=_opt_float(_at(fields, 16)),
        # NMEA 4.11 appends a System ID just before the checksum; its presence
        # is version-dependent, not length-dependent.
        system_id=_opt_str(_at(fields, 17)),
    )


def _decode_gsv(fields: tuple[str, ...]) -> Gsv:
    tail = list(fields[3:])
    signal_id: int | None = None
    # A trailing field that does not complete a quadruple is the NMEA 4.10+
    # Signal ID, which applies to the whole sentence.
    if len(tail) % 4 == 1:
        signal_id = _opt_int(tail.pop())

    satellites: list[GsSatellite] = []
    for index in range(0, len(tail) - 3, 4):
        prn = _opt_int(tail[index])
        if prn is None:
            continue
        satellites.append(
            GsSatellite(
                prn=prn,
                elevation_deg=_opt_float(tail[index + 1]),
                azimuth_deg=_opt_float(tail[index + 2]),
                snr_db=_opt_float(tail[index + 3]),
            )
        )

    return Gsv(
        messages_total=_opt_int(_at(fields, 0)),
        message_number=_opt_int(_at(fields, 1)),
        satellites_in_view=_opt_int(_at(fields, 2)),
        satellites=tuple(satellites),
        signal_id=signal_id,
    )


def _decode_zda(fields: tuple[str, ...]) -> Zda:
    return Zda(
        utc_time=_opt_str(_at(fields, 0)),
        utc_date=_iso_date(_at(fields, 3), _at(fields, 2), _at(fields, 1)),
        local_zone_hours=_opt_int(_at(fields, 4)),
        local_zone_minutes=_opt_int(_at(fields, 5)),
    )


def _decode_hdt(fields: tuple[str, ...]) -> Hdt:
    return Hdt(heading_true_deg=_opt_float(_at(fields, 0)))


def _decode_hdg(fields: tuple[str, ...]) -> Hdg:
    return Hdg(
        heading_magnetic_deg=_opt_float(_at(fields, 0)),
        deviation_deg=_signed_opt(_opt_float(_at(fields, 1)), _at(fields, 2)),
        variation_deg=_signed_opt(_opt_float(_at(fields, 3)), _at(fields, 4)),
    )


def _decode_rot(fields: tuple[str, ...]) -> Rot:
    return Rot(
        rot_deg_per_min=_opt_float(_at(fields, 0)),
        status=_opt_str(_at(fields, 1)),
    )


_DECODERS: Final[dict[str, Callable[[tuple[str, ...]], GpsMessage]]] = {
    "GGA": _decode_gga,
    "RMC": _decode_rmc,
    "GLL": _decode_gll,
    "VTG": _decode_vtg,
    "GSA": _decode_gsa,
    "GSV": _decode_gsv,
    "ZDA": _decode_zda,
    "HDT": _decode_hdt,
    "HDG": _decode_hdg,
    "ROT": _decode_rot,
}


def decode_sentence(sentence: Sentence) -> GpsMessage | None:
    """Decode a sentence's fields, or ``None`` if the formatter is not modelled.

    An unmodelled sentence is not an error -- the caller emits it as a raw
    pass-through rather than dropping it.
    """
    decoder = _DECODERS.get(sentence.formatter)
    return decoder(sentence.fields) if decoder is not None else None


def fix_of(message: GpsMessage) -> GpsFix | None:
    """Extract a usable own-ship position from a decoded sentence."""
    if isinstance(message, _POSITION_BEARING):
        if message.latitude_deg is None or message.longitude_deg is None:
            return None
        return GpsFix(
            latitude_deg=message.latitude_deg,
            longitude_deg=message.longitude_deg,
            utc_time=message.utc_time,
        )
    return None
