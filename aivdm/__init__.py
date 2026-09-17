"""AIVDM/AIVDO and NMEA 0183 GPS decoder for marine streams.

The pipeline is a straight line, and each stage is a pure function over the
previous stage's output:

    nmea.parse_sentence   raw line      -> Sentence
    ais.parse_aivdm       AIS sentence  -> AisFragment
    FragmentAssembler     fragments     -> AssembledPayload
    messages.decode_payload             -> typed AIS message
    gps.decode_sentence   GPS sentence  -> typed GPS message
    store.VesselStore     messages      -> per-MMSI joined Vessel

Nothing does I/O except :mod:`aivdm.cli`, and nothing reads the clock except
:class:`aivdm.store.VesselStore`, which takes an injected clock so tests are
deterministic.

Zero runtime dependencies, by design: the target is a Rust port.
"""

from __future__ import annotations

from aivdm.ais import AisFragment, AssembledPayload, FragmentAssembler, parse_aivdm
from aivdm.bits import BitReader, ShortPayload
from aivdm.gps import GpsFix, GpsMessage, decode_sentence, fix_of
from aivdm.messages import AisMessage, UnknownMessage, decode_payload
from aivdm.nmea import MalformedSentence, Sentence, checksum, parse_sentence
from aivdm.store import OwnShip, Vessel, VesselStore

__version__ = "0.1.0"

__all__ = [
    "AisFragment",
    "AisMessage",
    "AssembledPayload",
    "BitReader",
    "FragmentAssembler",
    "GpsFix",
    "GpsMessage",
    "MalformedSentence",
    "OwnShip",
    "Sentence",
    "ShortPayload",
    "UnknownMessage",
    "Vessel",
    "VesselStore",
    "__version__",
    "checksum",
    "decode_payload",
    "decode_sentence",
    "fix_of",
    "parse_aivdm",
    "parse_sentence",
]
