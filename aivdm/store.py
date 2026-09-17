"""Per-MMSI vessel store: joins static and dynamic AIS data into one record.

This is what turns a stream of individual sentences into "the exact ship AIS
data": the vessel name, callsign and dimensions from type 5 or 24 joined to the
position, course and speed from types 1, 2, 3, 18 or 19.

The governing merge rule is that a known field is never overwritten with an
absent one. Half of AIS integration bugs come from a later message clobbering a
good vessel name with an empty one, so a partial type 24 Part B must not erase a
name that arrived earlier.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aivdm.gps import GpsFix
from aivdm.messages import (
    AisMessage,
    AtoNReport,
    BaseStationReport,
    ClassBPositionReport,
    Dimensions,
    ExtendedClassBReport,
    LongRangeReport,
    PositionReport,
    SarAircraftReport,
    StaticDataReportA,
    StaticDataReportB,
    StaticVoyageData,
)
from aivdm.tables import MmsiInfo, mmsi_info

__all__ = ["OwnShip", "Vessel", "VesselStore"]


def _clock() -> datetime:
    return datetime.now(UTC)


def _length_beam(dimensions: Dimensions | None) -> tuple[int | None, int | None]:
    """Derive overall length and beam from the bow/stern and port/starboard split.

    Both halves must be known; half a dimension is not a dimension.
    """
    if dimensions is None:
        return None, None
    length = (
        dimensions.bow + dimensions.stern
        if dimensions.bow is not None and dimensions.stern is not None
        else None
    )
    beam = (
        dimensions.port + dimensions.starboard
        if dimensions.port is not None and dimensions.starboard is not None
        else None
    )
    return length, beam


@dataclass(slots=True)
class VesselStatic:
    """Identity and voyage data, merged across every static message seen."""

    name: str | None = None
    call_sign: str | None = None
    imo_number: int | None = None
    ship_type: int | None = None
    ship_type_text: str | None = None
    dimensions: Dimensions | None = None
    epfd: int | None = None
    epfd_text: str | None = None
    eta_month: int | None = None
    eta_day: int | None = None
    eta_hour: int | None = None
    eta_minute: int | None = None
    draught_m: float | None = None
    destination: str | None = None
    dte: bool | None = None
    ais_version: int | None = None
    vendor_id: str | None = None
    unit_model: int | None = None
    serial_number: int | None = None
    mothership_mmsi: int | None = None

    def to_dict(self) -> dict[str, object]:
        length, beam = _length_beam(self.dimensions)
        return {
            "name": self.name,
            "call_sign": self.call_sign,
            "imo_number": self.imo_number,
            "ship_type": self.ship_type,
            "ship_type_text": self.ship_type_text,
            "dimensions_m": None
            if self.dimensions is None
            else {
                "bow": self.dimensions.bow,
                "stern": self.dimensions.stern,
                "port": self.dimensions.port,
                "starboard": self.dimensions.starboard,
            },
            "length_m": length,
            "beam_m": beam,
            "epfd": self.epfd,
            "epfd_text": self.epfd_text,
            "eta": None
            if self.eta_month is None
            and self.eta_day is None
            and self.eta_hour is None
            and self.eta_minute is None
            else {
                "month": self.eta_month,
                "day": self.eta_day,
                "hour": self.eta_hour,
                "minute": self.eta_minute,
            },
            "draught_m": self.draught_m,
            "destination": self.destination,
            "dte": self.dte,
            "ais_version": self.ais_version,
            "vendor_id": self.vendor_id,
            "unit_model": self.unit_model,
            "serial_number": self.serial_number,
            "mothership_mmsi": self.mothership_mmsi,
        }


@dataclass(slots=True)
class VesselDynamic:
    """The latest position report, replaced wholesale on every update."""

    message_type: int
    nav_status: int | None = None
    nav_status_text: str | None = None
    rot_raw: int | None = None
    rot_deg_per_min: float | None = None
    rot_saturation_deg_per_min: float | None = None
    sog_raw: int | None = None
    sog_knots: float | None = None
    cog_deg: float | None = None
    heading_deg: int | None = None
    lat_deg: float | None = None
    lon_deg: float | None = None
    position_accuracy: bool | None = None
    raim: bool | None = None
    utc_second: int | None = None
    altitude_m: int | None = None
    gnss_position: bool | None = None
    channel: str | None = None
    source: str | None = None
    seen_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "message_type": self.message_type,
            "nav_status": self.nav_status,
            "nav_status_text": self.nav_status_text,
            "rot_raw": self.rot_raw,
            "rot_deg_per_min": self.rot_deg_per_min,
            "rot_saturation_deg_per_min": self.rot_saturation_deg_per_min,
            "sog_raw": self.sog_raw,
            "sog_knots": self.sog_knots,
            "cog_deg": self.cog_deg,
            "heading_deg": self.heading_deg,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "position_accuracy": self.position_accuracy,
            "raim": self.raim,
            "utc_second": self.utc_second,
            "altitude_m": self.altitude_m,
            "gnss_position": self.gnss_position,
            "channel": self.channel,
            "source": self.source,
            "seen_at": self.seen_at,
        }


@dataclass(slots=True)
class OwnShip:
    """The receiving station's own GNSS fix.

    GPS sentences carry no MMSI, so this lives outside the per-MMSI map. An
    AIVDO sentence supplies the own-ship MMSI that links the two.
    """

    latitude_deg: float
    longitude_deg: float
    utc_time: str | None
    source: str
    seen_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "utc_time": self.utc_time,
            "source": self.source,
            "seen_at": self.seen_at,
        }


@dataclass(slots=True)
class Vessel:
    """One AIS target, merged from every message seen for its MMSI."""

    mmsi: int
    info: MmsiInfo
    first_seen: str
    last_seen: str
    static: VesselStatic = field(default_factory=VesselStatic)
    dynamic: VesselDynamic | None = None
    counts: dict[str, int] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    formatters: list[str] = field(default_factory=list)

    def count(self, message_type: int) -> None:
        key = str(message_type)
        self.counts[key] = self.counts.get(key, 0) + 1

    def record_source(self, channel: str, formatter: str) -> None:
        if channel and channel not in self.channels:
            self.channels.append(channel)
        if formatter and formatter not in self.formatters:
            self.formatters.append(formatter)

    def to_dict(self) -> dict[str, object]:
        return {
            "mmsi": self.mmsi,
            "mmsi_info": {
                "category": self.info.category,
                "category_text": self.info.category_text,
                "mid": self.info.mid,
            },
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "static": self.static.to_dict(),
            "dynamic": None if self.dynamic is None else self.dynamic.to_dict(),
            "counts": {
                "messages": sum(self.counts.values()),
                "by_type": dict(self.counts),
            },
            "channels": list(self.channels),
            "formatters": list(self.formatters),
        }


class VesselStore:
    """Accumulates decoded AIS messages into per-MMSI vessel records."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        # ponytail: plain unbounded dict, no TTL or eviction. Bounded by the
        # number of distinct MMSIs for file and pipe workloads, which is the
        # supported use. A long-running daemon would grow without limit and
        # needs an LRU or age-based sweep here.
        self._vessels: dict[int, Vessel] = {}
        self._own_ship: OwnShip | None = None
        self._own_mmsi: int | None = None
        self._clock = clock if clock is not None else _clock

    def now(self) -> str:
        return self._clock().isoformat()

    def set_own_mmsi(self, mmsi: int | None) -> None:
        self._own_mmsi = mmsi

    @property
    def own_mmsi(self) -> int | None:
        return self._own_mmsi

    @property
    def own_ship(self) -> OwnShip | None:
        return self._own_ship

    def update_own_ship(self, fix: GpsFix, *, source: str) -> OwnShip:
        self._own_ship = OwnShip(
            latitude_deg=fix.latitude_deg,
            longitude_deg=fix.longitude_deg,
            utc_time=fix.utc_time,
            source=source,
            seen_at=self.now(),
        )
        return self._own_ship

    def get(self, mmsi: int) -> Vessel | None:
        return self._vessels.get(mmsi)

    def all(self) -> list[Vessel]:
        return [self._vessels[mmsi] for mmsi in sorted(self._vessels)]

    def __len__(self) -> int:
        return len(self._vessels)

    def _touch(self, mmsi: int, channel: str, formatter: str) -> Vessel:
        stamp = self.now()
        vessel = self._vessels.get(mmsi)
        if vessel is None:
            vessel = Vessel(
                mmsi=mmsi,
                info=mmsi_info(mmsi),
                first_seen=stamp,
                last_seen=stamp,
            )
            self._vessels[mmsi] = vessel
        vessel.last_seen = stamp
        vessel.record_source(channel, formatter)
        return vessel

    def update(
        self, message: AisMessage, *, channel: str = "", formatter: str = ""
    ) -> Vessel | None:
        """Fold one decoded AIS message into the store.

        Returns the affected vessel, or ``None`` for a message with no usable
        MMSI.
        """
        mmsi = message.mmsi
        if mmsi is None or mmsi == 0:
            return None

        vessel = self._touch(mmsi, channel, formatter)
        vessel.count(message.message_type)

        # -- dynamic (position) messages --
        if isinstance(message, PositionReport):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                nav_status=message.nav_status,
                nav_status_text=message.nav_status_text,
                rot_raw=message.rot_raw,
                rot_deg_per_min=message.rot_deg_per_min,
                rot_saturation_deg_per_min=message.rot_saturation_deg_per_min,
                sog_raw=message.sog_raw,
                sog_knots=message.sog_knots,
                cog_deg=message.cog_deg,
                heading_deg=message.heading_deg,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                utc_second=message.utc_second,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )
        elif isinstance(message, (ClassBPositionReport, ExtendedClassBReport)):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                sog_raw=message.sog_raw,
                sog_knots=message.sog_knots,
                cog_deg=message.cog_deg,
                heading_deg=message.heading_deg,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                utc_second=message.utc_second,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )
        elif isinstance(message, SarAircraftReport):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                sog_raw=message.sog_raw,
                sog_knots=message.sog_knots,
                cog_deg=message.cog_deg,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                utc_second=message.utc_second,
                altitude_m=message.altitude_m,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )
        elif isinstance(message, LongRangeReport):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                nav_status=message.nav_status,
                nav_status_text=message.nav_status_text,
                sog_raw=message.sog_raw,
                sog_knots=message.sog_knots,
                cog_deg=message.cog_deg,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                gnss_position=message.gnss_position,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )
        elif isinstance(message, BaseStationReport):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )
        elif isinstance(message, AtoNReport):
            vessel.dynamic = VesselDynamic(
                message_type=message.message_type,
                lat_deg=message.lat_deg,
                lon_deg=message.lon_deg,
                position_accuracy=message.position_accuracy,
                raim=message.raim,
                utc_second=message.utc_second,
                channel=channel or None,
                source=formatter or None,
                seen_at=vessel.last_seen,
            )

        # -- static (identity) messages: only ever fill blanks --
        if isinstance(message, StaticVoyageData):
            static = vessel.static
            if message.name:
                static.name = message.name
            if message.call_sign:
                static.call_sign = message.call_sign
            if message.imo_number:
                static.imo_number = message.imo_number
            if message.ship_type:
                static.ship_type = message.ship_type
            if message.ship_type_text:
                static.ship_type_text = message.ship_type_text
            if _has_dimensions(message.dimensions):
                static.dimensions = message.dimensions
            if message.epfd is not None:
                static.epfd = message.epfd
            if message.epfd_text:
                static.epfd_text = message.epfd_text
            if message.eta.month is not None:
                static.eta_month = message.eta.month
            if message.eta.day is not None:
                static.eta_day = message.eta.day
            if message.eta.hour is not None:
                static.eta_hour = message.eta.hour
            if message.eta.minute is not None:
                static.eta_minute = message.eta.minute
            if message.draught_m:
                static.draught_m = message.draught_m
            if message.destination:
                static.destination = message.destination
            if message.dte is not None:
                static.dte = message.dte
            if message.ais_version is not None:
                static.ais_version = message.ais_version
        elif isinstance(message, StaticDataReportA):
            # Part A carries the name and nothing else. Part B uses the same
            # MMSI and must not undo it.
            if message.name:
                vessel.static.name = message.name
        elif isinstance(message, StaticDataReportB):
            static = vessel.static
            if message.call_sign:
                static.call_sign = message.call_sign
            if message.ship_type:
                static.ship_type = message.ship_type
            if message.ship_type_text:
                static.ship_type_text = message.ship_type_text
            if _has_dimensions(message.dimensions):
                static.dimensions = message.dimensions
            if message.vendor_id:
                static.vendor_id = message.vendor_id
            if message.unit_model is not None:
                static.unit_model = message.unit_model
            if message.serial_number is not None:
                static.serial_number = message.serial_number
            if message.mothership_mmsi:
                static.mothership_mmsi = message.mothership_mmsi
        elif isinstance(message, ExtendedClassBReport):
            static = vessel.static
            if message.name:
                static.name = message.name
            if message.ship_type:
                static.ship_type = message.ship_type
            if message.ship_type_text:
                static.ship_type_text = message.ship_type_text
            if _has_dimensions(message.dimensions):
                static.dimensions = message.dimensions
            if message.epfd is not None:
                static.epfd = message.epfd
            if message.epfd_text:
                static.epfd_text = message.epfd_text
            if message.dte is not None:
                static.dte = message.dte
        elif isinstance(message, AtoNReport):
            static = vessel.static
            if message.name:
                static.name = message.name
            if message.epfd is not None:
                static.epfd = message.epfd
            if message.epfd_text:
                static.epfd_text = message.epfd_text
            if _has_dimensions(message.dimensions):
                static.dimensions = message.dimensions

        return vessel

    def snapshot(self) -> dict[str, object]:
        """The full joined view, including own ship."""
        return {
            "own_ship": None if self._own_ship is None else self._own_ship.to_dict(),
            "own_mmsi": self._own_mmsi,
            "vessels": [vessel.to_dict() for vessel in self.all()],
        }


def _has_dimensions(dimensions: Dimensions | None) -> bool:
    """A dimensions block of all-None is absent, not a zero-sized vessel."""
    if dimensions is None:
        return False
    return any(
        value is not None
        for value in (dimensions.bow, dimensions.stern, dimensions.port, dimensions.starboard)
    )
