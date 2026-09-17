"""Tests for the per-MMSI vessel store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nmea_builder import build, class_b_position, position_report, static_data_part_a

from aivdm.ais import FragmentAssembler, parse_aivdm
from aivdm.gps import decode_sentence, fix_of
from aivdm.messages import decode_payload
from aivdm.nmea import parse_sentence
from aivdm.store import VesselStore

# Reference vectors. TYPE_5 and TYPE_18 deliberately share an MMSI (367533950
# and 367430530 respectively do NOT -- see the pairing notes below).
TYPE_1 = "!AIVDM,1,1,,A,13HOI:541swceR8Kqmr4lSkDP0gP,0*5E"  # MMSI 227006760
TYPE_5 = [
    "!AIVDM,2,1,0,A,55NPOOP2;H;s<HtKP00EHE:0@T4@Dl0000000016L961O5GfNNkQEp6ClRh0,0*71",
    "!AIVDM,2,2,0,A,00000000000,2*24",
]  # MMSI 367533950 -- EVER DIADEM
TYPE_18 = "!AIVDM,1,1,,A,B5NJ;PP0Emkw8`UJ;bv3U`g5SP06,0*53"  # MMSI 367430530
TYPE_24A = "!AIVDM,1,1,,A,H5NJ;PQ@E=B1HE=<Dh0000000000,0*0E"  # MMSI 367430530
TYPE_24B = "!AIVDM,1,1,,A,H5NJ;PU6123<30qG43ijkl3h<440,0*3A"  # MMSI 367430530
TYPE_21 = "!AIVDM,1,1,,A,E>kb5Qo37a2V0W2@87TW:000000@3AFL>de5000003sP0000000000000000,4*6B"
GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

EVER_DIADEM_MMSI = 367533950
JOSEPH_MMSI = 367430530

FROZEN = datetime(2026, 9, 17, 10, 30, 0, tzinfo=UTC)


def freeze() -> datetime:
    return FROZEN


def feed(*sentences: str, store: VesselStore, channel: str = "A", formatter: str = "VDM"):
    """Push sentences through framing, reassembly and decoding into the store."""
    assembler = FragmentAssembler()
    last = None
    for sentence in sentences:
        assembled = assembler.push(parse_aivdm(parse_sentence(sentence)))
        if assembled is not None:
            last = store.update(
                decode_payload(assembled.payload, assembled.fill_bits),
                channel=channel,
                formatter=formatter,
            )
    return last


def feed_payload(payload_fields, *, store: VesselStore, channel: str = "A"):
    """Push a constructed payload straight into the store."""
    payload, fill_bits = build(payload_fields)
    return store.update(decode_payload(payload, fill_bits), channel=channel, formatter="VDM")


class TestJoining:
    def test_static_and_dynamic_join_on_mmsi(self) -> None:
        store = VesselStore(clock=freeze)
        feed(*TYPE_5, store=store)
        feed_payload(class_b_position(mmsi=EVER_DIADEM_MMSI, sog=87, cog=2105), store=store)

        vessel = store.get(EVER_DIADEM_MMSI)
        assert vessel is not None
        # The identity came from the two-sentence type 5...
        assert vessel.static.name == "EVER DIADEM"
        assert vessel.static.call_sign == "3FOF8"
        assert vessel.static.destination == "NEW YORK"
        # ...and the motion from a type 18 that carried no identity at all.
        assert vessel.dynamic is not None
        assert vessel.dynamic.message_type == 18
        assert vessel.dynamic.sog_knots == pytest.approx(8.7)
        assert vessel.dynamic.cog_deg == pytest.approx(210.5)

    def test_dimensions_yield_length_and_beam(self) -> None:
        store = VesselStore(clock=freeze)
        feed(*TYPE_5, store=store)
        vessel = store.get(EVER_DIADEM_MMSI)
        assert vessel is not None
        static = vessel.static.to_dict()
        assert static["dimensions_m"] == {"bow": 225, "stern": 70, "port": 1, "starboard": 31}
        assert static["length_m"] == 295
        assert static["beam_m"] == 32

    def test_part_a_and_part_b_land_on_the_same_vessel(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_24A, store=store)
        feed(TYPE_24B, store=store)

        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        # Part B carries no name, so it must not have erased Part A's.
        assert vessel.static.name == "TEST VESSEL"
        assert vessel.static.call_sign == "WDC1234"
        assert vessel.static.vendor_id == "ABC"
        assert vessel.static.ship_type == 70
        assert vessel.static.dimensions is not None
        assert vessel.static.dimensions.bow == 30

    def test_part_b_before_part_a_also_joins(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_24B, store=store)
        feed(TYPE_24A, store=store)
        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.static.name == "TEST VESSEL"
        assert vessel.static.call_sign == "WDC1234"

    def test_message_counts_and_sources_are_tracked(self) -> None:
        store = VesselStore(clock=freeze)
        feed_payload(class_b_position(mmsi=JOSEPH_MMSI), store=store, channel="A")
        feed_payload(class_b_position(mmsi=JOSEPH_MMSI), store=store, channel="B")

        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.counts == {"18": 2}
        assert vessel.channels == ["A", "B"]
        assert vessel.formatters == ["VDM"]

    def test_unknown_message_type_still_counts_against_the_mmsi(self) -> None:
        store = VesselStore(clock=freeze)
        payload, fill_bits = build([(28, 6), (0, 2), (JOSEPH_MMSI, 30), (0, 8)])
        store.update(decode_payload(payload, fill_bits))
        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.static.name is None
        assert vessel.counts == {"28": 1}


class TestMergeRules:
    """The rule that matters most: a later partial message never erases.

    Half of real-world AIS integration bugs are a good vessel name being
    clobbered by an empty field from a subsequent message.
    """

    def test_position_report_does_not_erase_a_known_name(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_24A, store=store)
        assert store.get(JOSEPH_MMSI).static.name == "TEST VESSEL"  # type: ignore[union-attr]

        feed(TYPE_18, store=store)  # same MMSI, no name field at all
        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.static.name == "TEST VESSEL"

    def test_an_empty_name_does_not_erase_a_known_name(self) -> None:
        store = VesselStore(clock=freeze)
        feed_payload(static_data_part_a(mmsi=JOSEPH_MMSI, name="TEST VESSEL"), store=store)
        assert store.get(JOSEPH_MMSI).static.name == "TEST VESSEL"  # type: ignore[union-attr]

        # A Part A that lost its name (padding only) must be a no-op.
        feed_payload(static_data_part_a(mmsi=JOSEPH_MMSI, name=""), store=store)
        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.static.name == "TEST VESSEL"

    def test_part_b_does_not_erase_the_part_a_name(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_24A, store=store)
        feed(TYPE_24B, store=store)
        vessel = store.get(JOSEPH_MMSI)
        assert vessel is not None
        assert vessel.static.name == "TEST VESSEL"

    def test_dynamic_is_replaced_wholesale_by_the_newer_report(self) -> None:
        store = VesselStore(clock=freeze)
        feed_payload(class_b_position(mmsi=JOSEPH_MMSI, sog=87, cog=2105, heading=209), store=store)
        first = store.get(JOSEPH_MMSI)
        assert first is not None and first.dynamic is not None
        assert first.dynamic.sog_knots == pytest.approx(8.7)

        feed_payload(
            position_report(
                message_type=1,
                mmsi=JOSEPH_MMSI,
                sog=153,
                cog=902,
                heading=91,
                lon=1200000,
                lat=-600000,
            ),
            store=store,
        )
        second = store.get(JOSEPH_MMSI)
        assert second is not None and second.dynamic is not None
        assert second.dynamic.message_type == 1
        assert second.dynamic.sog_knots == pytest.approx(15.3)
        assert second.dynamic.cog_deg == pytest.approx(90.2)
        assert second.dynamic.heading_deg == 91
        assert second.dynamic.lon_deg == pytest.approx(2.0)

    def test_identity_survives_a_wholesale_dynamic_replacement(self) -> None:
        store = VesselStore(clock=freeze)
        feed(*TYPE_5, store=store)
        feed_payload(class_b_position(mmsi=EVER_DIADEM_MMSI, sog=87), store=store)
        feed_payload(position_report(mmsi=EVER_DIADEM_MMSI, sog=153), store=store)

        vessel = store.get(EVER_DIADEM_MMSI)
        assert vessel is not None
        assert vessel.static.name == "EVER DIADEM"
        assert vessel.dynamic is not None
        assert vessel.dynamic.sog_knots == pytest.approx(15.3)

    def test_aid_to_navigation_is_tracked(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_21, store=store)
        vessel = store.get(993691015)
        assert vessel is not None
        assert vessel.info.category == "aton"
        assert vessel.static.name == "FORELAND POINT"
        assert vessel.dynamic is not None
        assert vessel.dynamic.lat_deg == pytest.approx(51.375)


class TestOwnShip:
    def test_gps_fix_is_stored_outside_the_mmsi_map(self) -> None:
        store = VesselStore(clock=freeze)
        message = decode_sentence(parse_sentence(GGA))
        assert message is not None
        fix = fix_of(message)
        assert fix is not None

        store.update_own_ship(fix, source="GGA")
        assert store.own_ship is not None
        assert store.own_ship.latitude_deg == pytest.approx(48.1173)
        assert store.own_ship.longitude_deg == pytest.approx(11.5166667)
        assert store.own_ship.source == "GGA"
        # A GPS fix has no MMSI, so it must not have created a vessel entry.
        assert len(store) == 0

    def test_own_mmsi_can_be_linked_from_aivdo(self) -> None:
        store = VesselStore(clock=freeze)
        assert store.own_mmsi is None
        store.set_own_mmsi(227006760)
        assert store.own_mmsi == 227006760

    def test_snapshot_carries_own_ship_and_vessels(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_1, store=store)
        message = decode_sentence(parse_sentence(GGA))
        assert message is not None
        fix = fix_of(message)
        assert fix is not None
        store.update_own_ship(fix, source="GGA")

        snapshot = store.snapshot()
        assert snapshot["own_ship"] is not None
        vessels = snapshot["vessels"]
        assert isinstance(vessels, list)
        assert len(vessels) == 1
        assert vessels[0]["mmsi"] == 227006760


class TestTimestamps:
    def test_timestamps_come_from_the_injected_clock(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_1, store=store)
        vessel = store.get(227006760)
        assert vessel is not None
        assert vessel.first_seen == FROZEN.isoformat()
        assert vessel.last_seen == FROZEN.isoformat()

    def test_a_second_update_advances_last_seen_only(self) -> None:
        # One clock read per update: _touch stamps once and uses that stamp for
        # both first_seen and last_seen.
        times = iter(
            [
                datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                datetime(2026, 9, 17, 11, 0, tzinfo=UTC),
            ]
        )
        store = VesselStore(clock=lambda: next(times))
        feed(TYPE_1, store=store)
        feed(TYPE_1, store=store)
        vessel = store.get(227006760)
        assert vessel is not None
        assert vessel.first_seen.startswith("2026-09-17T10:00")
        assert vessel.last_seen.startswith("2026-09-17T11:00")


class TestDegenerateInput:
    def test_message_without_an_mmsi_is_ignored(self) -> None:
        store = VesselStore(clock=freeze)
        message = decode_payload("0")
        assert store.update(message) is None
        assert len(store) == 0

    def test_vessel_ordering_is_by_mmsi(self) -> None:
        store = VesselStore(clock=freeze)
        feed(TYPE_18, store=store)  # 367430530
        feed(TYPE_1, store=store)  # 227006760
        assert [vessel.mmsi for vessel in store.all()] == [227006760, 367430530]

    def test_empty_store_snapshot(self) -> None:
        store = VesselStore(clock=freeze)
        assert store.snapshot() == {"own_ship": None, "own_mmsi": None, "vessels": []}
        assert len(store) == 0
