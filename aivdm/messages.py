"""Per-message-type AIS payload decoders.

Offsets come from the ITU-R M.1371 field layout as tabulated in the AIVDM/AIVDO
decoding reference. Every decoder here is a straight-line sequence of explicit
bit reads: no reflection, no field-name loops, no dynamic dispatch beyond one
message-type lookup table. That keeps the Rust port mechanical and keeps the
offsets reviewable against the spec.

Two conventions apply throughout:

* A field whose value is a documented "not available" sentinel decodes to
  ``None``, never to a plausible-looking number.
* Scaled fields are emitted alongside their raw integer (``sog_raw`` next to
  ``sog_knots``) because raw values carry meaning the scaled value destroys --
  ``sog_raw == 1022`` means "102.2 knots or more", not "102.2 knots".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from aivdm.bits import BitReader, ShortPayload
from aivdm.tables import aton_type_text, epfd_text, maneuver_text, nav_status_text, ship_type_text

__all__ = [
    "AisMessage",
    "AtoNReport",
    "BaseStationReport",
    "ClassBPositionReport",
    "Dimensions",
    "Eta",
    "ExtendedClassBReport",
    "LongRangeReport",
    "PositionReport",
    "SarAircraftReport",
    "StaticDataReportA",
    "StaticDataReportB",
    "StaticVoyageData",
    "UnknownMessage",
    "decode_payload",
]

# --- shared scales and sentinels -------------------------------------------

_COORD_SCALE: Final = 600_000.0  # 1/10000 minute -> degrees
_NO_LONGITUDE: Final = 181 * 600_000
_NO_LATITUDE: Final = 91 * 600_000
_NO_SOG: Final = 1023
_NO_COG: Final = 3600
_NO_HEADING: Final = 511
_NO_ALTITUDE: Final = 4095

# AIS longitude/latitude resolve to 1/10000 minute, about 0.185 m. Rounding to
# six decimal places (~0.11 m) is finer than the encoding and keeps the JSON
# readable instead of emitting 106.84923333333334.
_COORD_PRECISION: Final = 6

_ROT_SCALE: Final = 4.733
_NO_TURN_INFORMATION: Final = -128
_TURNING_RIGHT_NO_TI: Final = 127
_TURNING_LEFT_NO_TI: Final = -127

# Consecutive raw values differ by ~11 deg/min near the top of the range and by
# ~0.045 near zero. Six decimals keeps raw +/-1 (0.044643) distinguishable from
# raw 0 ("not turning"), without implying precision the field does not carry.
_ROT_PRECISION: Final = 6

_SECOND_SPECIAL: Final[dict[int, str]] = {
    60: "not available",
    61: "manual input mode",
    62: "estimated (dead reckoning) mode",
    63: "positioning system inoperative",
}


def _longitude(raw: int) -> float | None:
    if raw == _NO_LONGITUDE or not -180 * 600_000 <= raw <= 180 * 600_000:
        return None
    return round(raw / _COORD_SCALE, _COORD_PRECISION)


def _latitude(raw: int) -> float | None:
    if raw == _NO_LATITUDE or not -90 * 600_000 <= raw <= 90 * 600_000:
        return None
    return round(raw / _COORD_SCALE, _COORD_PRECISION)


def _sog_knots(raw: int) -> float | None:
    """Speed over ground in knots, 0.1-knot resolution.

    ``raw == 1022`` means "102.2 knots or higher"; the caller can detect that
    from ``sog_raw``.
    """
    return None if raw == _NO_SOG else round(raw / 10.0, 1)


def _sog_knots_whole(raw: int) -> float | None:
    """Type 9 and 27 encode SOG in whole knots, not tenths."""
    return None if raw == _NO_SOG else float(raw)


def _cog(raw: int, unavailable: int = _NO_COG) -> float | None:
    if raw == unavailable or raw > 3600:
        return None
    return round(raw / 10.0, 1)


def _heading(raw: int, unavailable: int = _NO_HEADING) -> int | None:
    if raw == unavailable or raw > 359:
        return None
    return raw


def _second(raw: int) -> tuple[int | None, str | None]:
    if raw in _SECOND_SPECIAL:
        return None, _SECOND_SPECIAL[raw]
    return raw, None


def _rot(raw: int) -> tuple[float | None, str | None, float | None]:
    """Decode rate of turn.

    Returns ``(degrees_per_minute, annotation, saturation_bound)``.

    The field is two things at once:

    * A **quantity**, for ``0`` and ``+/-1..+/-126``, via the sensor formula
      ``ROT_AIS = 4.733 * sqrt(ROT_sensor)``. Decoding squares the quotient and
      the sign must be applied outside the square, or port/starboard is lost.
    * A **flag**, for ``-128`` (no information at all) and ``+/-127`` (turning,
      but the transmitter's turn indicator is unavailable, so the magnitude is
      simply not quantified). These must not be run through the formula.

    ``+/-127`` is also where the encoding saturates: the formula evaluated there
    is ~720.003 deg/min, the largest magnitude the field can express. Decoders
    that apply the formula blindly report 720.003210529537 for a value that
    carries no magnitude at all, so it is exposed separately as the
    ``saturation_bound`` while ``degrees_per_minute`` stays ``None``.
    """
    if raw == _NO_TURN_INFORMATION:
        return None, "no turn information available", None
    if raw == _TURNING_RIGHT_NO_TI:
        return (
            None,
            "turning right at more than 5 deg/30 s (no turn indicator)",
            _rot_saturation(),
        )
    if raw == _TURNING_LEFT_NO_TI:
        return (
            None,
            "turning left at more than 5 deg/30 s (no turn indicator)",
            _rot_saturation(),
        )
    if raw == 0:
        return 0.0, None, None
    magnitude = (abs(raw) / _ROT_SCALE) ** 2
    signed = magnitude if raw > 0 else -magnitude
    return round(signed, _ROT_PRECISION), None, None


def _rot_saturation() -> float:
    """The magnitude at which the rate-of-turn field saturates."""
    return round((_TURNING_RIGHT_NO_TI / _ROT_SCALE) ** 2, _ROT_PRECISION)


def _dimension(raw: int) -> int | None:
    """Vessel dimensions: 0 means not available, 511/63 mean "or greater"."""
    return None if raw == 0 else raw


def _eta(month: int, day: int, hour: int, minute: int) -> Eta:
    return Eta(
        month=None if month == 0 else month,
        day=None if day == 0 else day,
        hour=None if hour == 24 else hour,
        minute=None if minute == 60 else minute,
    )


# --- shared value objects ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dimensions:
    bow: int | None
    stern: int | None
    port: int | None
    starboard: int | None


@dataclass(frozen=True, slots=True)
class Eta:
    month: int | None
    day: int | None
    hour: int | None
    minute: int | None


# --- message dataclasses ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionReport:
    """Types 1, 2 and 3 share this layout; ``message_type`` distinguishes them."""

    message_type: int
    repeat: int
    mmsi: int
    nav_status: int | None
    nav_status_text: str | None
    rot_raw: int
    rot_deg_per_min: float | None
    rot_saturation_deg_per_min: float | None
    rot_text: str | None
    sog_raw: int
    sog_knots: float | None
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    cog_raw: int
    cog_deg: float | None
    heading_deg: int | None
    utc_second: int | None
    utc_second_status: str | None
    maneuver: int | None
    maneuver_text: str | None
    raim: bool
    radio_status: int


@dataclass(frozen=True, slots=True)
class BaseStationReport:
    """Types 4 and 11 share this layout."""

    message_type: int
    repeat: int
    mmsi: int
    year: int | None
    month: int | None
    day: int | None
    hour: int | None
    minute: int | None
    second: int | None
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    epfd: int | None
    epfd_text: str | None
    raim: bool
    radio_status: int


@dataclass(frozen=True, slots=True)
class StaticVoyageData:
    """Type 5. Normally split across two AIVDM sentences (424 bits)."""

    message_type: int
    repeat: int
    mmsi: int
    ais_version: int | None
    imo_number: int
    call_sign: str
    name: str
    ship_type: int | None
    ship_type_text: str | None
    dimensions: Dimensions
    epfd: int | None
    epfd_text: str | None
    eta: Eta
    draught_m: float | None
    destination: str
    dte: bool | None


@dataclass(frozen=True, slots=True)
class SarAircraftReport:
    """Type 9. SOG is in whole knots here, not tenths."""

    message_type: int
    repeat: int
    mmsi: int
    altitude_raw: int
    altitude_m: int | None
    sog_raw: int
    sog_knots: float | None
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    cog_raw: int
    cog_deg: float | None
    utc_second: int | None
    utc_second_status: str | None
    dte: bool | None
    assigned: bool | None
    raim: bool


@dataclass(frozen=True, slots=True)
class ClassBPositionReport:
    """Type 18. The regional-reserved block here is 8 bits at offset 38."""

    message_type: int
    repeat: int
    mmsi: int
    sog_raw: int
    sog_knots: float | None
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    cog_raw: int
    cog_deg: float | None
    heading_deg: int | None
    utc_second: int | None
    utc_second_status: str | None
    cs_unit: bool | None
    has_display: bool | None
    has_dsc: bool | None
    can_use_all_bands: bool | None
    accepts_message_22: bool | None
    assigned: bool | None
    raim: bool
    radio_status: int


@dataclass(frozen=True, slots=True)
class ExtendedClassBReport:
    """Type 19.

    Bits 0-138 match type 18, except the second regional-reserved block is
    4 bits here rather than 2. Kept as its own decoder rather than sharing a
    helper with type 18 -- the widths genuinely differ.
    """

    message_type: int
    repeat: int
    mmsi: int
    sog_raw: int
    sog_knots: float | None
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    cog_raw: int
    cog_deg: float | None
    heading_deg: int | None
    utc_second: int | None
    utc_second_status: str | None
    name: str
    ship_type: int | None
    ship_type_text: str | None
    dimensions: Dimensions
    epfd: int | None
    epfd_text: str | None
    raim: bool
    dte: bool | None
    assigned: bool | None


@dataclass(frozen=True, slots=True)
class AtoNReport:
    """Type 21. Variable length: 272 bits plus an optional name extension."""

    message_type: int
    repeat: int
    mmsi: int
    aton_type: int | None
    aton_type_text: str | None
    name: str
    name_extension: str
    position_accuracy: bool | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    dimensions: Dimensions
    epfd: int | None
    epfd_text: str | None
    utc_second: int | None
    utc_second_status: str | None
    off_position: bool | None
    raim: bool
    virtual_aid: bool | None
    assigned: bool | None


@dataclass(frozen=True, slots=True)
class StaticDataReportA:
    """Type 24 part A: vessel name only."""

    message_type: int
    repeat: int
    mmsi: int
    part_number: int
    name: str


@dataclass(frozen=True, slots=True)
class StaticDataReportB:
    """Type 24 part B.

    For an auxiliary craft (MMSI starting ``98``) bits 132-161 hold the
    mothership MMSI instead of vessel dimensions.
    """

    message_type: int
    repeat: int
    mmsi: int
    part_number: int
    ship_type: int | None
    ship_type_text: str | None
    vendor_id: str
    unit_model: int | None
    serial_number: int | None
    call_sign: str
    dimensions: Dimensions | None
    mothership_mmsi: int | None


@dataclass(frozen=True, slots=True)
class LongRangeReport:
    """Type 27. Coarse long-range broadcast, 96 bits."""

    message_type: int
    repeat: int
    mmsi: int
    position_accuracy: bool | None
    raim: bool
    nav_status: int | None
    nav_status_text: str | None
    lon_raw: int
    lon_deg: float | None
    lat_raw: int
    lat_deg: float | None
    sog_raw: int
    sog_knots: float | None
    cog_raw: int
    cog_deg: float | None
    gnss_position: bool | None


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    """A message type with no decoder, or a payload too short to decode.

    Emitted rather than dropped so nothing disappears silently.
    """

    message_type: int
    repeat: int | None
    mmsi: int | None
    bit_length: int
    payload: str


AisMessage: TypeAlias = (
    PositionReport
    | BaseStationReport
    | StaticVoyageData
    | SarAircraftReport
    | ClassBPositionReport
    | ExtendedClassBReport
    | AtoNReport
    | StaticDataReportA
    | StaticDataReportB
    | LongRangeReport
    | UnknownMessage
)


# --- decoders ---------------------------------------------------------------


def _decode_position_report(reader: BitReader, message_type: int) -> PositionReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    nav_status = reader.u(4)
    rot_raw = reader.i(8)
    sog_raw = reader.u(10)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    cog_raw = reader.u(12)
    heading_raw = reader.u(9)
    second_raw = reader.u(6)
    maneuver = reader.u(2)
    reader.skip(3)
    raim = reader.flag()
    radio_status = reader.u(19)

    utc_second, utc_second_status = _second(second_raw)
    rot_deg, rot_text, rot_saturation = _rot(rot_raw)

    return PositionReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        nav_status=nav_status,
        nav_status_text=nav_status_text(nav_status),
        rot_raw=rot_raw,
        rot_deg_per_min=rot_deg,
        rot_saturation_deg_per_min=rot_saturation,
        rot_text=rot_text,
        sog_raw=sog_raw,
        sog_knots=_sog_knots(sog_raw),
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        cog_raw=cog_raw,
        cog_deg=_cog(cog_raw),
        heading_deg=_heading(heading_raw),
        utc_second=utc_second,
        utc_second_status=utc_second_status,
        maneuver=maneuver,
        maneuver_text=maneuver_text(maneuver),
        raim=raim,
        radio_status=radio_status,
    )


def _decode_base_station(reader: BitReader, message_type: int) -> BaseStationReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    year = reader.u(14)
    month = reader.u(4)
    day = reader.u(5)
    hour = reader.u(5)
    minute = reader.u(6)
    second_raw = reader.u(6)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    epfd = reader.u(4)
    reader.skip(10)
    raim = reader.flag()
    radio_status = reader.u(19)

    second, _ = _second(second_raw)

    return BaseStationReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        year=None if year == 0 else year,
        month=None if month == 0 else month,
        day=None if day == 0 else day,
        hour=None if hour == 24 else hour,
        minute=None if minute == 60 else minute,
        second=second,
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        epfd=epfd,
        epfd_text=epfd_text(epfd),
        raim=raim,
        radio_status=radio_status,
    )


def _decode_static_voyage(reader: BitReader, message_type: int) -> StaticVoyageData:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    ais_version = reader.u(2)
    imo_number = reader.u(30)
    call_sign = reader.text(42)
    name = reader.text(120)
    ship_type = reader.u(8)
    dim_bow = reader.u(9)
    dim_stern = reader.u(9)
    dim_port = reader.u(6)
    dim_starboard = reader.u(6)
    epfd = reader.u(4)
    eta_month = reader.u(4)
    eta_day = reader.u(5)
    eta_hour = reader.u(5)
    eta_minute = reader.u(6)
    draught_raw = reader.u(8)

    # Type 5 turns up at 420, 422 and 426 bits in the wild. Everything above is
    # present in all of them; only the destination/DTE tail is at risk, so it is
    # read only when the whole 120-bit field is actually there.
    destination = ""
    dte: bool | None = None
    if reader.remaining() >= 120:
        destination = reader.text(120)
        if reader.remaining() >= 1:
            dte = reader.flag()

    return StaticVoyageData(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        ais_version=ais_version,
        # No sentinel is defined for IMO number; 0 is emitted as-is.
        imo_number=imo_number,
        call_sign=call_sign,
        name=name,
        ship_type=ship_type,
        ship_type_text=ship_type_text(ship_type),
        dimensions=Dimensions(
            bow=_dimension(dim_bow),
            stern=_dimension(dim_stern),
            port=_dimension(dim_port),
            starboard=_dimension(dim_starboard),
        ),
        epfd=epfd,
        epfd_text=epfd_text(epfd),
        eta=_eta(eta_month, eta_day, eta_hour, eta_minute),
        draught_m=round(draught_raw / 10.0, 1),
        destination=destination,
        dte=dte,
    )


def _decode_sar_aircraft(reader: BitReader, message_type: int) -> SarAircraftReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    altitude_raw = reader.u(12)
    sog_raw = reader.u(10)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    cog_raw = reader.u(12)
    second_raw = reader.u(6)
    reader.skip(8)
    dte = reader.flag()
    reader.skip(3)
    assigned = reader.flag()
    raim = reader.flag()

    utc_second, utc_second_status = _second(second_raw)

    return SarAircraftReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        altitude_raw=altitude_raw,
        altitude_m=None if altitude_raw == _NO_ALTITUDE else altitude_raw,
        sog_raw=sog_raw,
        sog_knots=_sog_knots_whole(sog_raw),
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        cog_raw=cog_raw,
        cog_deg=_cog(cog_raw),
        utc_second=utc_second,
        utc_second_status=utc_second_status,
        dte=dte,
        assigned=assigned,
        raim=raim,
    )


def _decode_class_b(reader: BitReader, message_type: int) -> ClassBPositionReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    reader.skip(8)  # regional reserved
    sog_raw = reader.u(10)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    cog_raw = reader.u(12)
    heading_raw = reader.u(9)
    second_raw = reader.u(6)
    reader.skip(2)  # regional reserved
    cs_unit = reader.flag()
    has_display = reader.flag()
    has_dsc = reader.flag()
    can_use_all_bands = reader.flag()
    accepts_message_22 = reader.flag()
    assigned = reader.flag()
    raim = reader.flag()
    radio_status = reader.u(20)

    utc_second, utc_second_status = _second(second_raw)

    return ClassBPositionReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        sog_raw=sog_raw,
        sog_knots=_sog_knots(sog_raw),
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        cog_raw=cog_raw,
        cog_deg=_cog(cog_raw),
        heading_deg=_heading(heading_raw),
        utc_second=utc_second,
        utc_second_status=utc_second_status,
        cs_unit=cs_unit,
        has_display=has_display,
        has_dsc=has_dsc,
        can_use_all_bands=can_use_all_bands,
        accepts_message_22=accepts_message_22,
        assigned=assigned,
        raim=raim,
        radio_status=radio_status,
    )


def _decode_extended_class_b(reader: BitReader, message_type: int) -> ExtendedClassBReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    reader.skip(8)  # regional reserved
    sog_raw = reader.u(10)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    cog_raw = reader.u(12)
    heading_raw = reader.u(9)
    second_raw = reader.u(6)
    reader.skip(4)  # regional reserved -- 4 bits here, not 2 as in type 18
    name = reader.text(120)
    ship_type = reader.u(8)
    dim_bow = reader.u(9)
    dim_stern = reader.u(9)
    dim_port = reader.u(6)
    dim_starboard = reader.u(6)
    epfd = reader.u(4)
    raim = reader.flag()
    dte = reader.flag()
    assigned = reader.flag()
    reader.skip(4)

    utc_second, utc_second_status = _second(second_raw)

    return ExtendedClassBReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        sog_raw=sog_raw,
        sog_knots=_sog_knots(sog_raw),
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        cog_raw=cog_raw,
        cog_deg=_cog(cog_raw),
        heading_deg=_heading(heading_raw),
        utc_second=utc_second,
        utc_second_status=utc_second_status,
        name=name,
        ship_type=ship_type,
        ship_type_text=ship_type_text(ship_type),
        dimensions=Dimensions(
            bow=_dimension(dim_bow),
            stern=_dimension(dim_stern),
            port=_dimension(dim_port),
            starboard=_dimension(dim_starboard),
        ),
        epfd=epfd,
        epfd_text=epfd_text(epfd),
        raim=raim,
        dte=dte,
        assigned=assigned,
    )


def _decode_aton(reader: BitReader, message_type: int) -> AtoNReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    aton_type = reader.u(5)
    name = reader.text(120)
    accuracy = reader.flag()
    lon_raw = reader.i(28)
    lat_raw = reader.i(27)
    dim_bow = reader.u(9)
    dim_stern = reader.u(9)
    dim_port = reader.u(6)
    dim_starboard = reader.u(6)
    epfd = reader.u(4)
    second_raw = reader.u(6)
    off_position = reader.flag()
    reader.skip(8)  # regional reserved
    raim = reader.flag()
    virtual_aid = reader.flag()
    assigned = reader.flag()
    reader.skip(1)

    # The name extension is whatever is left, up to 14 six-bit characters.
    # Only meaningful when the 20-character name was full.
    extension = ""
    leftover = reader.remaining()
    if leftover >= 6:
        extension = reader.text((leftover // 6) * 6)

    utc_second, utc_second_status = _second(second_raw)

    return AtoNReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        aton_type=aton_type,
        aton_type_text=aton_type_text(aton_type),
        name=name,
        name_extension=extension,
        position_accuracy=accuracy,
        lon_raw=lon_raw,
        lon_deg=_longitude(lon_raw),
        lat_raw=lat_raw,
        lat_deg=_latitude(lat_raw),
        dimensions=Dimensions(
            bow=_dimension(dim_bow),
            stern=_dimension(dim_stern),
            port=_dimension(dim_port),
            starboard=_dimension(dim_starboard),
        ),
        epfd=epfd,
        epfd_text=epfd_text(epfd),
        utc_second=utc_second,
        utc_second_status=utc_second_status,
        off_position=off_position,
        raim=raim,
        virtual_aid=virtual_aid,
        assigned=assigned,
    )


def _decode_static_data(reader: BitReader, message_type: int) -> AisMessage:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    part_number = reader.u(2)

    if part_number == 0:
        return StaticDataReportA(
            message_type=message_type,
            repeat=repeat,
            mmsi=mmsi,
            part_number=part_number,
            name=reader.text(120) if reader.remaining() >= 120 else "",
        )

    if part_number == 1:
        ship_type = reader.u(8)
        vendor_id = reader.text(18)
        unit_model = reader.u(4)
        serial_number = reader.u(20)
        call_sign = reader.text(42)

        # "98" in the first two digits marks an auxiliary craft, whose 30-bit
        # tail is a mothership MMSI rather than dimensions.
        if f"{mmsi:09d}".startswith("98"):
            return StaticDataReportB(
                message_type=message_type,
                repeat=repeat,
                mmsi=mmsi,
                part_number=part_number,
                ship_type=ship_type,
                ship_type_text=ship_type_text(ship_type),
                vendor_id=vendor_id,
                unit_model=unit_model,
                serial_number=serial_number,
                call_sign=call_sign,
                dimensions=None,
                mothership_mmsi=reader.u(30),
            )

        return StaticDataReportB(
            message_type=message_type,
            repeat=repeat,
            mmsi=mmsi,
            part_number=part_number,
            ship_type=ship_type,
            ship_type_text=ship_type_text(ship_type),
            vendor_id=vendor_id,
            unit_model=unit_model,
            serial_number=serial_number,
            call_sign=call_sign,
            dimensions=Dimensions(
                bow=_dimension(reader.u(9)),
                stern=_dimension(reader.u(9)),
                port=_dimension(reader.u(6)),
                starboard=_dimension(reader.u(6)),
            ),
            mothership_mmsi=None,
        )

    # Part numbers 2 and 3 are not permitted.
    return UnknownMessage(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        bit_length=max(0, reader.size),
        payload="",
    )


def _decode_long_range(reader: BitReader, message_type: int) -> LongRangeReport:
    repeat = reader.u(2)
    mmsi = reader.u(30)
    accuracy = reader.flag()
    raim = reader.flag()
    nav_status = reader.u(4)
    lon_raw = reader.i(18)
    lat_raw = reader.i(17)
    sog_raw = reader.u(6)
    cog_raw = reader.u(9)
    gnss = reader.flag()
    reader.skip(1)

    # Type 27 uses 18-bit and 17-bit coordinates, not the 28/27 of the common
    # navigation block, so the shared helpers do not apply. The scale is
    # 1/10 minute (1/600 degree), which is consistent with the coordinate
    # widths; the 181/91 degree sentinels then fall outside +/-180 and +/-90
    # and are rejected by the range check, whatever encoding they use.
    lon_deg = round(lon_raw / 600.0, _COORD_PRECISION)
    lat_deg = round(lat_raw / 600.0, _COORD_PRECISION)

    return LongRangeReport(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        position_accuracy=accuracy,
        raim=raim,
        nav_status=nav_status,
        nav_status_text=nav_status_text(nav_status),
        lon_raw=lon_raw,
        lon_deg=lon_deg if -180.0 <= lon_deg <= 180.0 else None,
        lat_raw=lat_raw,
        lat_deg=lat_deg if -90.0 <= lat_deg <= 90.0 else None,
        sog_raw=sog_raw,
        sog_knots=None if sog_raw == 63 else float(sog_raw),
        cog_raw=cog_raw,
        cog_deg=None if cog_raw >= 360 else float(cog_raw),
        gnss_position=gnss,
    )


_DECODERS: Final[dict[int, Callable[[BitReader, int], AisMessage]]] = {
    1: _decode_position_report,
    2: _decode_position_report,
    3: _decode_position_report,
    4: _decode_base_station,
    5: _decode_static_voyage,
    9: _decode_sar_aircraft,
    11: _decode_base_station,
    18: _decode_class_b,
    19: _decode_extended_class_b,
    21: _decode_aton,
    24: _decode_static_data,
    27: _decode_long_range,
}


def _unknown(payload: str, fill_bits: int, message_type: int) -> UnknownMessage:
    repeat: int | None = None
    mmsi: int | None = None
    try:
        reader = BitReader(payload, fill_bits)
        reader.u(6)
        repeat = reader.u(2)
        mmsi = reader.u(30)
    except (ShortPayload, ValueError):
        pass

    return UnknownMessage(
        message_type=message_type,
        repeat=repeat,
        mmsi=mmsi,
        bit_length=max(0, len(payload) * 6 - fill_bits),
        payload=payload,
    )


def decode_payload(payload: str, fill_bits: int = 0) -> AisMessage:
    """Decode an assembled AIS payload.

    A payload too short for its message type degrades to
    :class:`UnknownMessage` rather than raising: a truncated sentence on a
    live feed must not abort the stream. A payload with no readable type at all
    (fewer than six usable bits) is reported as ``message_type == 0``, which is
    not a valid AIS type and so cannot be confused with a real one.
    """
    reader = BitReader(payload, fill_bits)
    try:
        message_type = reader.u(6)
    except ShortPayload:
        return UnknownMessage(
            message_type=0,
            repeat=None,
            mmsi=None,
            bit_length=reader.size,
            payload=payload,
        )

    decoder = _DECODERS.get(message_type)
    if decoder is None:
        return _unknown(payload, fill_bits, message_type)

    try:
        return decoder(reader, message_type)
    except ShortPayload:
        return _unknown(payload, fill_bits, message_type)
