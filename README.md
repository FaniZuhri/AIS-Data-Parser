# AIS-Data-Parser

Decode a marine NMEA 0183 stream carrying **both** AIS (`!AIVDM`/`!AIVDO`) and
GPS (`$xxGGA`, `$xxRMC`, …) sentences into machine-readable JSON Lines.

Built for a source that transmits AIS and GPS on one wire, so a consumer can
recover both the receiving station's own GNSS fix and per-vessel AIS data from a
single pass. Designed to be ported to Rust: zero runtime dependencies, explicit
bit offsets, pure functions.

**Contents:**
[Names](#names) ·
[Requirements](#requirements) ·
[Quickstart](#quickstart) ·
[Testing it on your own data](#testing-it-on-your-own-data) ·
[Usage](#usage) ·
[Output](#output) ·
[Coverage](#coverage) ·
[Verification](#verification) ·
[Architecture](#architecture) ·
[Porting to Rust](#porting-to-rust) ·
[Not implemented](#not-implemented) ·
[Development](#development) ·
[AI agents](#working-on-this-with-an-ai-agent)

## Names

Three names are in play. They are not the same thing:

| What | Name |
|---|---|
| Git repository / GitHub remote | `AIS-Data-Parser` |
| Directory on disk (currently) | `aivdm_decoder` |
| Python package, CLI, `--version` string | `aivdm` |

The package is `aivdm` regardless of where the repo is checked out. Renaming the
directory is safe; renaming the package is not — it would break `python -m aivdm`,
the `aivdm` console script, and every import in the tests.

## Requirements

- Python 3.11.9
- pipenv

Runtime dependencies: **none**. The decoder is stdlib-only on purpose — see
[Porting to Rust](#porting-to-rust).

## Install and run

```sh
pipenv install
pipenv shell
python main.py --help
```

`main.py` at the repo root is the entry point. It is a thin wrapper around
`aivdm.cli`, so `python -m aivdm` does exactly the same thing — useful once the
package is installed, or when you do not want to activate the environment first
(`pipenv run python -m aivdm`).

## Quickstart

```sh
# Decode the sample from the requester
printf '%s\n' '!AIVDM,1,1,,A,3815GkhOib7a7O9t`1qqi`?H00gP,0*52' | python main.py

# A log file, human-readable
python main.py tests/fixtures/mixed_stream.txt --format table

# One joined record per vessel, with the full joined table at the end
python main.py tests/fixtures/mixed_stream.txt --mode vessel --vessels

# Spreadsheet handoff
python main.py tests/fixtures/mixed_stream.txt --format csv > decoded.csv
```

Without activating the shell, prefix each with `pipenv run`.

## Testing it on your own data

Drop a capture anywhere and point the tool at it. `tests/fixtures/AIS_Test_270726.txt`
is a real one to try first — 105 `$GPRMC` sentences at 1 Hz plus 16 `!AIVDM`
sentences, recorded in Jakarta Bay on 2026-07-27.

```sh
cd /path/to/aivdm_decoder

# 1. Decode the lot into a file. Progress summary on stderr, records in the file.
python main.py tests/fixtures/AIS_Test_270726.txt -o decoded.jsonl
#   -> aivdm: 121 sentence(s) decoded, 0 skipped, 0 problem(s), 5 vessel(s)

# 2. Just the ships: one row per vessel, nothing else. 6 records, not 121.
python main.py tests/fixtures/AIS_Test_270726.txt --mode summary -o ships.csv

# 3. Human-readable, one block per sentence. Page through it rather than
#    scrolling.
python main.py tests/fixtures/AIS_Test_270726.txt -o decoded.txt
less decoded.txt

# 4. AIS only, no GPS noise.
grep '!AIVD' tests/fixtures/AIS_Test_270726.txt | python main.py -o ais_only.txt

# 5. Live: append to one file as the stream comes in.
tail -f /path/to/live.log | python main.py --append -o today.jsonl
```

To sanity-check the output, pull the vessel positions out and drop them on a
map — for a Jakarta Bay capture the coordinates should land in Jakarta Bay:

```sh
python main.py tests/fixtures/AIS_Test_270726.txt --mode summary -o ships.csv
python -c "
import csv
for r in csv.DictReader(open('ships.csv')):
    if r['kind'] == 'vessel':
        print(r['mmsi'], r['name'] or '-', r['lat_deg'], r['lon_deg'])
"
```

Useful flags while investigating a file:

| Flag | Use |
|---|---|
| `-v` | print per-line warnings as they happen, not just a count at the end |
| `--checksum drop` | discard sentences with a bad checksum, instead of decoding them anyway |
| `--checksum require` | also discard sentences with no checksum at all |
| `--no-track` | stateless: one record per sentence, no vessel join |
| `--exit-on-error` | stop at the first malformed line and exit 3 — good for finding a bad file fast |
| `--errors strict` | fail on undecodable bytes instead of replacing them |

If a line is not decoding as you expect, check it is really the sentence you
think it is. The tool reports the declared *and* computed checksum on every
record, so a mismatch is visible:

```sh
python main.py bad.log --format table | grep BAD
```

## Usage

```
python main.py [INPUT]

  INPUT                      NMEA log file, or '-' for stdin (default: stdin)

  -o, --output PATH          write records to PATH instead of stdout; '-' means
                             stdout (default). Format is inferred from the
                             extension unless --format is given
  --append                   append to --output instead of overwriting it
  --format {json,table,csv}  output format; default inferred from --output's
                             extension (.json/.jsonl -> json, .csv -> csv,
                             .txt/.log -> table), else json
  --mode {sentence,vessel,summary}
                             one record per input sentence (default); one per
                             vessel update; or 'summary' for just the final
                             joined vessel table, one record per vessel
  --vessels                  after the stream ends, dump the joined vessel table
  --track / --no-track       maintain the per-MMSI vessel store (default: on)
  --checksum {report,drop,require}
                             report records the checksum but keeps the sentence
                             (default); drop discards a wrong checksum; require
                             also discards a missing one
  --exit-on-error            stop at the first malformed sentence, exit 3
  --encoding ENC             input encoding (default: utf-8)
  --errors {strict,replace,ignore}
                             undecodable byte handling (default: replace)
  -q, --quiet                suppress the summary on stderr
  -v, --verbose              print per-line warnings on stderr
  --version
```

Exit codes: `0` success, `1` I/O error, `2` usage error, `3` `--exit-on-error`
tripped.

### Storing the output instead of scrolling it

A 127-line capture is 121 records, which is a lot of terminal. Send it to a file
with `-o`; the format follows the extension:

```sh
python main.py capture.log -o decoded.jsonl   # JSON Lines
python main.py capture.log -o decoded.csv     # spreadsheet
python main.py capture.log -o decoded.txt     # human-readable
```

`--format` overrides the extension if you want a mismatch, and `--output -`
explicitly means stdout. The run summary still goes to stderr, so you keep
progress feedback while the records go to the file.

For a live feed, `--append` keeps adding to the same file, and the CSV header is
only written once:

```sh
python main.py --append -o today.csv < /dev/ttyUSB0
```

If you do not want the per-sentence stream at all, take just the picture at the
end. `--mode summary` reads everything but emits only the final vessel table,
one record per vessel — on the 127-line capture that is 6 records instead of 121:

```sh
python main.py capture.log --mode summary -o ships.csv
```

Six records instead of 121. The full CSV has 19 columns; trimmed to the
interesting ones, the 127-line capture gives:

```
kind,mmsi,name,lat_deg,lon_deg,sog_knots,nav_status
own_ship,,,-6.096533,106.738239,,
vessel,525000036,BUOYS-RED01-99%,-6.060703,106.71928,0.3,
vessel,525005002,,-6.005112,106.799117,14.5,0
vessel,525015954,NUSANTARA REGAS 1,-5.975225,106.799272,0.0,5
vessel,525100325,,-6.107688,106.80949,0.0,8
vessel,538007503,,,-5.897428,106.849233,10.6,0
```

In summary mode a malformed line is still counted in the stderr problem total,
but it does not get a row in the ship table — the point of the mode is the final
picture, not the traffic.

### Nothing is dropped

In the default mode every input line produces exactly one record. A malformed
line, an unmodelled sentence formatter, an AIS message type with no decoder, and
a multi-fragment message still waiting for its other half all emit an explicit
record rather than disappearing. Lines beginning with `#` are treated as
comments and skipped — capture logs commonly carry headers, and `#` can never be
a valid NMEA introducer.

`--mode summary` is the one exception, and it is opt-in: it deliberately emits
only the final vessel table. Every line is still read and every problem still
counted on stderr, so nothing goes unnoticed — there just are no per-sentence
rows.

## Output

JSON Lines, one object per line. An AIS record:

```json
{"rx_time":"2026-09-17T10:00:00.123456+00:00","line_no":12,
 "kind":"ais","talker":"AI","formatter":"VDM","raw":"!AIVDM,...",
 "checksum":{"declared":"52","computed":"52","ok":true},
 "fragments":{"count":1,"number":1,"seq_id":null,"channel":"A",
              "fill_bits":0,"own_ship":false},
 "message":{"message_type":3,"repeat":0,"mmsi":538007503,
            "nav_status":0,"nav_status_text":"Under way using engine",
            "rot_raw":127,"rot_deg_per_min":null,
            "rot_text":"turning right at more than 5 deg/30 s (no turn indicator)",
            "sog_raw":106,"sog_knots":10.6,"position_accuracy":false,
            "lon_raw":64109540,"lon_deg":106.849233,
            "lat_raw":-3538457,"lat_deg":-5.897428,
            "cog_raw":2502,"cog_deg":250.2,"heading_deg":263,
            "utc_second":44,"utc_second_status":null,
            "maneuver":0,"maneuver_text":"Not available",
            "raim":false,"radio_status":3040},
 "vessel":{"mmsi":538007503,"mmsi_info":{"category":"ship",...}, ...}}
```

`kind` is one of:

| `kind` | meaning |
|---|---|
| `ais` | decoded AIVDM/AIVDO message |
| `gps` | decoded GPS sentence from the modelled set |
| `nmea` | a valid sentence this decoder does not model, passed through with its raw `fields` |
| `unknown` | unparseable line, with an `error` |
| `vessel` | `--mode vessel`: one joined vessel update |
| `own_ship` | `--mode vessel`: own-ship GNSS fix update |
| `snapshot` | `--vessels`: the full joined table at end of stream |

### Rate of turn: where aggsoft disagrees

If you compare against the [aggsoft online decoder](https://www.aggsoft.com/ais-decoder.htm),
expect a difference on rate of turn for `±127`:

| | raw 127 |
|---|---|
| aggsoft | `720.003210529537` |
| this decoder | `rot_deg_per_min: null`, `rot_saturation_deg_per_min: 720.003211` |

aggsoft applies the ROT formula to a value the spec defines as a sentinel.
`127` means *"turning right, turn indicator available: no"* — a flag, not a
measurement. The arithmetic is not in dispute: `(127 / 4.733)² = 720.0032105295371`
exactly, and `1..126` already covers up to `708.709`, so `127` is where the
*encoding* saturates. Reporting nine significant figures implies a precision
the field does not have.

The bound is still available, clearly labelled, so nothing is lost. `pyais`
independently agrees: its `TurnRate` enum has `NO_TI_RIGHT = 127` and returns
the enum rather than a computed number.

### Two rules that shape the output

**Sentinel values become `null`, never a plausible number.** An AIS
"not available" is not `0` and is not a coordinate. Every scaled field that has
a documented sentinel keeps its raw integer alongside the scaled value
(`sog_raw` next to `sog_knots`), because raw values carry meaning the scaled
value destroys: `sog_raw == 1022` means "102.2 knots **or more**", and
`rot_raw == 127` means "turning right at more than 5°/30 s, no turn indicator
available" — neither survives being flattened into a float.

**A known field is never overwritten with an absent one.** A type 18 position
report carries no vessel name; it must not erase the name a type 5 supplied. The
same applies to type 24 Part A and Part B, which arrive as separate messages for
the same MMSI, in either order.

## Coverage

**AIS message types decoded:** 1, 2, 3 (Class A position), 4 and 11 (base
station), 5 (static and voyage), 9 (SAR aircraft), 18 (Class B position), 19
(extended Class B), 21 (aid to navigation, including the variable-length name
extension), 24 Part A and Part B (static data, including the auxiliary-craft
mothership MMSI form), 27 (long range). Any other type is reported as
`unknown` with its raw payload rather than dropped.

**GPS sentence formatters decoded:** GGA, RMC, GLL, VTG, GSA, GSV, ZDA, HDT,
HDG, ROT. Talkers are not validated — `$GPGGA` and `$GNGGA` and the
`GL`/`GA`/`GB`/`BD`/`GI`/`GQ`/`IN` variants all reach the same decoder, matched
on the 3-character formatter. Matching the full 5-character address is a common
bug that silently drops every multi-constellation receiver.

Everything else (`GNS`, `DTM`, `DPT`, `MTW`, `VHW`, `MWV`, …) is passed through
as `kind: "nmea"` with its raw fields.

Multi-fragment AIS messages are reassembled. Type 5 is 424 bits and normally
arrives as two sentences, so this is not optional for the vessel name, callsign
and destination.

`!AIVDM` (received traffic) and `!AIVDO` (own ship) are treated as **two
independent streams**. They share a talker, a channel, and usually a sequential
id, so the reassembler keys on the formatter as well. Without that, an own-ship
fragment can be absorbed into a received-traffic message and produce a *spliced*
payload that decodes to confidently wrong data. `!AIVDO` is also what sets
`own_mmsi` — GPS fixes carry no MMSI, so the own-ship GNSS position and the
own-ship AIS identity are linked only through the `AIVDO` stream.

## Verification

Correctness was established three ways, because a single wrong bit offset
silently produces confident, wrong vessel positions:

1. **280 unit tests** (`pytest`), built around real sentences whose
   checksums are published or independently computed. Every fixture sentence is
   checksum-verified — if you add one, verify it too.
2. **Independent oracle** (`tools/oracle_check.py`). Every message type is
   encoded from known field values with [`pyais`](https://github.com/M0r13n/pyais)
   — a separate, well-tested implementation — and decoded with this library.
   11 of 12 types agree on every shared field. `pyais` is deliberately *not* a
   dependency; the script's docstring shows how to run it in a throwaway
   virtualenv.
3. **Hand-decode.** The requester's sample was decoded by hand from the spec
   before any code existed, then asserted as a complete expected dictionary in
   `tests/test_messages.py`. All 23 fields match.

Expected oracle result: every type reports `ok` **except type 21**, where this
library deliberately reports AIS vessel dimensions of `0` as `null` while
`pyais` keeps `0`. An aid to navigation is not zero metres wide.

`tools/oracle_check.py` documents two coverage gaps so its output is not
over-trusted: type 11 cannot be checked against `pyais` at all, and type 24 is
not in its case list.

### Known limitations

- **Type 11 dispatch is unverified against the oracle.** `pyais` cannot emit a
  type 11 sentence — `encode_dict({"type": 11, ...})` silently produces type 4
  bits — so this path is covered by a constructed payload rather than a
  generated one. Types 4 and 11 share a layout, so only the dispatch is
  untested by the cross-check.
- **Type 27 coordinates** use a dedicated `1/600` degree helper. The reference
  prints sentinels (`181000`/`91000`) that are arithmetically impossible in the
  declared 18/17-bit fields, so the decoder rejects out-of-range values instead
  of guessing an encoding. Type 27 is a rare long-range satellite broadcast.
- **SOTDMA radio status** (19/20 bits) is emitted as a raw integer. The
  reference defers the sub-field layout to IALA; inventing it would be worse
  than not decoding it.

## Architecture

The pipeline is a straight line, and every stage is a pure function of the
previous one:

```
nmea.parse_sentence     raw line     -> Sentence
ais.parse_aivdm         AIS sentence -> AisFragment
FragmentAssembler       fragments    -> AssembledPayload
messages.decode_payload              -> typed AIS message
gps.decode_sentence     GPS sentence -> typed GPS message
store.VesselStore       messages     -> per-MMSI joined Vessel
```

| Module | Role |
|---|---|
| `main.py` | repo-root entry point; thin wrapper over `aivdm.cli` |
| `bits.py` | `BitReader`, 6-bit armoring, AIS 6-bit ASCII |
| `nmea.py` | sentence tokenising and checksum |
| `gps.py` | GPS/GNSS sentence decoders |
| `ais.py` | AIVDM/AIVDO framing and fragment reassembly |
| `messages.py` | per-message-type bit-field decoders |
| `tables.py` | lookup tables (nav status, ship type, EPFD, AtoN, MMSI class) |
| `store.py` | per-MMSI joining of static and dynamic data |
| `cli.py` | argument parsing, read loop, output writers |

Only `cli.py` does I/O. Only `store.py` reads the clock, through an injected
callable, so tests are deterministic and the decoders stay pure.

## Porting to Rust

The design choices exist to make the port mechanical:

- Decoders are straight-line sequences of explicit integer-offset bit reads. No
  reflection, no `getattr`, no runtime field-name loops.
- `BitReader` is a cursor with a fixed API that maps to a `Vec<u8>` cursor.
- Every "not available" field is `Optional[T]`, mapping to `Option<T>`.
- Units are in the field names: `sog_knots`, `lon_deg`, `altitude_m`.
- `mypy --strict` is clean, so the types are real.
- Message types are a single lookup table over an integer, mapping to a `match`.
- `tests/nmea_builder.py` is a test-local inverse of `bits.py` — handy for
  generating Rust test vectors from the same inputs.

Given the Rust ecosystem already has mature AIS crates, this implementation's
value as a port target is as a *verified* reference: the `pyais` cross-check and
the hand-decoded fixture give a second implementation to diff against.

## Not implemented

Deliberate omissions, with the reason:

| Missing | Why |
|---|---|
| AIS types 25, 26 (single/multi-slot binary) | Field offsets are variable-length in the reference, not fixed. Needs a different decoder shape. |
| AIS types 6, 7, 8, 10, 12–17, 20, 22, 23 | Application-specific or rarely seen. Still reported as `unknown` with the raw payload. |
| MID → flag state (country) | A ~250-row static ITU table. The `mid` integer is already emitted so a consumer can map it. |
| Live serial (RS-232/RS-422) input | Would add `pyserial` as a dependency. Input is stdin + file today. |
| Live TCP/UDP listening | Same trade-off; pipe from `nc` or `socat` instead. |

## Development

```sh
pipenv install --dev
pipenv shell

pytest                                              # tests
pytest --cov=aivdm --cov-report=term-missing        # coverage
ruff check .                                        # lint
mypy aivdm                                          # strict typecheck
```

All three must be clean before a change is finished. `Pipfile.lock` is committed
and CI (`.gitlab-ci.yml`) installs with `pipenv install --deploy`.

## Working on this with an AI agent

See [AGENTS.md](AGENTS.md). It records the invariants that must not be broken,
the verification protocol, and the spec ambiguities that are already resolved —
so an agent does not have to re-derive them, or silently re-break them.

## Data sources

- [AIVDM/AIVDO protocol decoding (gpsd)](https://gpsd.gitlab.io/gpsd/AIVDM.html) — AIS bit layouts
- [NMEA Revealed (gpsd)](https://gpsd.gitlab.io/gpsd/NMEA.html) — NMEA 0183 sentence fields
- ITU-R M.1371 for the AIS message catalogue

## Licence

MIT — see [LICENSE](LICENSE). The copyright holder line currently names
TransTRACK; change it if this repo is personal.
