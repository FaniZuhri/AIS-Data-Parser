# AGENTS.md — AIS-Data-Parser

Project-specific context for any AI agent working in this repo. Read this before
changing code. It records decisions already made, invariants that must not be
broken, and traps that have already cost time once.

**Precedence.** The global `~/.commandcode/AGENTS.md` and **Ponytail** both
apply. Where this file conflicts with Ponytail, **Ponytail wins** — including
Ponytail's `ponytail:` marker convention in code. This file is not a
counterweight to those; it is repo-specific fact on top of them.

**Tone.** Caveman mode governs prose. It does **not** govern code, commit
messages, or the verification steps below — those stay at full clarity.

---

## 1. What this is

Decodes a marine NMEA 0183 stream carrying both AIS (`!AIVDM`/`!AIVDO`) and GPS
(`$xxGGA`, `$xxRMC`, …) into JSON Lines. Recovers own-ship GNSS position and
per-MMSI AIS vessel data from one pass.

Three names, not interchangeable:

| What | Name |
|---|---|
| Git repo / GitHub remote | `AIS-Data-Parser` |
| Directory on disk | `aivdm_decoder` |
| Python package + CLI | `aivdm` |

Renaming the directory is fine. Renaming the package is not — it breaks
`python -m aivdm`, the console script, and every test import.

Tech: Python 3.11.9, pipenv. **Zero runtime dependencies.**

## 2. Commands

The user's preferred workflow is `pipenv install`, `pipenv shell`, then
`python main.py …` from the repo root. `main.py` is the entry point; keep it
working. `python -m aivdm` must stay equivalent.

```sh
pipenv install --dev
pipenv shell

python main.py tests/fixtures/mixed_stream.txt -o decoded.jsonl
python main.py tests/fixtures/mixed_stream.txt --mode summary -o ships.csv
pytest                     # 280 tests
pytest --cov=aivdm --cov-report=term-missing
ruff check .
mypy aivdm                 # strict = true
```

`ruff`, `mypy`, and `pytest` must **all** be clean before a change is finished.
`mypy --strict` is not negotiable — it is what keeps the Rust port mechanical.

### Output sink

`-o/--output PATH` writes records to a file; the format is inferred from the
extension (`.json`/`.jsonl`/`.ndjson` → json, `.csv` → csv, `.txt`/`.table`/
`.log` → table) unless `--format` is given explicitly. `-o -` means stdout.
`--append` adds to an existing file and suppresses a duplicate CSV header.
`--mode summary` emits only the final joined table, one record per vessel.

Two behaviours to preserve when touching this code:

- The run summary stays on **stderr**, so it is still visible when records go to
  a file. `-q` silences it.
- `--mode summary` is the one place records are deliberately withheld: it
  suppresses the per-sentence stream *and* malformed-line records, because the
  point is the final picture. Every line is still read and every problem still
  counted on stderr. Do not "restore" the per-sentence records there.

## 3. Hard invariants

Break any of these and the value of the project drops sharply. None are
stylistic preferences.

1. **Zero runtime dependencies.** `[packages]` in `Pipfile` stays empty.
   `pyais` is a *verification* tool only (see §5) and must never enter the
   Pipfile or the package.
2. **No reflection in decoders.** No `getattr`, `setattr`, `eval`, or runtime
   field-name loops in `aivdm/`. Offsets are literal integers in straight-line
   code. A `_fill(target, "name", value)` helper using `setattr` was written and
   then deliberately removed for exactly this reason — do not reintroduce it.
3. **No clock reads in decoders.** Only `VesselStore` reads the clock, via an
   injected callable. `datetime.now()` inside a decoder breaks determinism and
   the Rust port.
4. **Sentinel → `None`, never `0`.** An AIS "not available" is not zero and is
   not a coordinate. Every scaled field with a documented sentinel also carries
   its raw integer (`sog_raw` beside `sog_knots`), because the raw value holds
   meaning the scaled one destroys: `sog_raw == 1022` is "≥102.2 kn",
   `rot_raw == 127` is "turning right >5°/30 s, no TI".
5. **A known field is never overwritten with an absent one.** This is the whole
   merge rule in `store.py`. A type 18 carries no name and must not erase a type
   5's name; type 24 Part B must not erase Part A's name. Half of real AIS
   integration bugs are this.
6. **Match the 3-char NMEA formatter, never the 5-char address.** Matching
   `$GPGGA` silently drops `$GNGGA` and every other constellation.
7. **Nothing is silently dropped.** New message types, unmodelled sentences,
   malformed lines, and half-arrived fragments all emit an explicit record.
8. **Coordinates round to 6 decimal places.** AIS/NMEA resolve to 1/10000
   minute (≈0.185 m); 6 dp (≈0.11 m) is finer and stops `48.11729999999999`
   reaching the output. Do not round coarser.

## 4. Field offsets: do not trust memory

**Get bit offsets from the reference, not from recall.** This has already gone
wrong. Recalled layouts were wrong on three message types and were only caught
by fetching the spec:

| Type | What memory got wrong |
|---|---|
| 9 | Altitude is 38-49; SOG is 50-59 **in whole knots, not tenths**; COG 116-127; timestamp 128-133; regional reserved 134-141; DTE 142; spare 143-145; assigned 146; RAIM 147 |
| 24B | Vendor ID is **18 bits at 48-65** (3 chars), *not* a 42-bit block; unit model 66-69; serial 70-89; callsign 90-131 |
| 21 | Regional reserved is **8 bits at 260-267**, not 2; name extension starts at 272 |

Also easy to get wrong and already settled:
- Type 18's second regional-reserved block is **2 bits**; type 19's is **4 bits**.
  Two code paths, not a shared helper.
- Type 27 longitude/latitude are **18/17 bits**, not the 28/27 of the common
  navigation block. Do not reuse the CNB helpers. It has its own `1/600` degree
  helper.
- Type 5 is 424 bits and spans two sentences. Variants at 420/422/426 exist in
  the wild; the destination/DTE tail is read only when all 120 bits are present,
  so the identity fields survive a short payload.

Sources: [AIVDM/AIVDO decoding (gpsd)](https://gpsd.gitlab.io/gpsd/AIVDM.html)
and [NMEA Revealed (gpsd)](https://gpsd.gitlab.io/gpsd/NMEA.html). Fetch them.
They are large — delegate the fetch to a subagent so it does not flood context.

## 5. Verification protocol

Three independent methods. Use all three for any decoder change. A single wrong
bit offset produces confident wrong vessel positions, which is worse than a
crash because nobody notices.

1. **Unit tests.** `tests/fixtures/*.txt` hold real sentences whose checksums
   are published or independently computed. **If you add a fixture sentence,
   verify its checksum** — several published-resembling strings in this repo
   were guesses that failed verification (`$GNGGA,…*44` was wrong, `*59` is
   right). `tests/nmea_builder.py` is a test-local inverse of `bits.py` for
   constructed edge cases; it lives in `tests/` on purpose so it never becomes
   a maintained public API.
   `tests/fixtures/mixed_stream.txt` deliberately interleaves an `!AIVDO` type 5
   with an `!AIVDM` type 5 that shares a talker, channel and sequential id.
   Keep that shape — it is the only end-to-end guard against the splice in §6.
   Do not "tidy" it by separating the two streams.
   `tests/fixtures/AIS_Test_270726.txt` is a 127-line **real capture** (105
   `$GPRMC` at 1 Hz + 16 `!AIVDM`, Jakarta Bay, 2026-07-27) and is the strongest
   guard against over-fitting to hand-built vectors. `tests/test_capture.py`
   pins it: all 121 checksums verify, 5 vessels join, a channel-B two-fragment
   type 5 reassembles to 424 bits, a sentence transmitted 4× is emitted 4×, and
   the requester's original sample appears verbatim at line 36. Do not edit or
   "clean" that file — its value is that it is unmodified field data.
   Expect real data to exercise the `0 → None` rule: that type 5 sends all four
   dimension fields as 0, so they decode to `null`.
2. **Independent oracle.** `tools/oracle_check.py` encodes each type from known
   field values with `pyais` and decodes with this library. Run it in a
   throwaway venv outside the project:
   ```sh
   python3.11 -m venv /tmp/aivdm-oracle
   /tmp/aivdm-oracle/bin/pip install pyais
   /tmp/aivdm-oracle/bin/python tools/oracle_check.py
   ```
   Expected: every type `ok` **except type 21** (see §6). Anything else is a
   regression.
3. **Third-party eyeball.** Paste a fixture into the aggsoft online AIS decoder
   and diff against our JSON. Expect **one class of difference**: aggsoft runs
   the rate-of-turn formula on the `±127` sentinel and prints `720.003210529537`.
   That is a known aggsoft bug, not a defect here — see §6.

**Oracle coverage gaps — do not over-trust it:**
- **Type 11 cannot be checked.** `pyais`'s `encode_dict({"type": 11, ...})`
  silently emits **type 4 bits**, so running type 11 through the oracle just
  re-tests type 4. Type 11 dispatch is pinned by a constructed payload in
  `tests/test_messages.py` instead.
- Type 24 is not in the oracle's case list; its three variants are pinned by
  tests.

## 6. Resolved spec ambiguities — do not re-litigate

Each was investigated and decided. Reversing one needs new evidence, not a
preference.

- **Type 21 dimensions `0` → `null`.** This is the one deliberate divergence
  from `pyais`, which keeps `0`. AIS uses `0` for "not available"; an aid to
  navigation is not zero metres wide. The oracle therefore reports 4 expected
  "mismatches" on type 21. That is not a bug.
- **Fill bits come from the last fragment.** Padding sits at the end of the
  concatenated bit string. The spec does not state this.
- **Reassembly key is `(talker, formatter, channel, seq_id, fragment_count)`.**
  The spec does not name a key. **The formatter must stay in it.** `VDM` and
  `VDO` share a talker, a channel, and usually a sequential id, so dropping the
  formatter makes the second stream's fragment 1 be discarded as a duplicate and
  then lets the first fragment 2 complete a payload *spliced across two
  different messages*. That decodes to confidently wrong data — worse than a
  drop. Regression tests: `test_vdm_and_vdo_do_not_merge` and
  `test_mixed_fixture_keeps_the_two_type5_streams_apart`.
  Where `seq_id` is absent, the outcome is a dropped message, never a spliced
  one: a fragment number already present is ignored, and parts are joined in
  numeric order.
- **Type 27 sentinels are rejected by range, not by value.** The reference prints
  `181000`/`91000`, which are arithmetically impossible in 18/17-bit fields. The
  range check (±180/±90) doubles as the sentinel check, so the ambiguity never
  reaches the output.
- **ROT `±127` carries no magnitude but exposes a saturation bound.** The field
  is a quantity for `0` and `±1..±126` (`ROT_AIS = 4.733 * sqrt(ROT_sensor)`),
  and a *flag* for `-128` ("no information") and `±127` ("turning, no turn
  indicator available"). Decoders that skip the sentinel check report
  `(127/4.733)**2 = 720.003210529537` for `127` — a value with no magnitude.
  aggsoft does exactly this; **do not "fix" us to match it.**
  `rot_deg_per_min` stays `None` and the bound is reported separately as
  `rot_saturation_deg_per_min` (pinned by
  `test_turning_sentinels_carry_no_magnitude_but_expose_the_saturation`).
  Note `1..126` already covers up to `708.709`, so 127 is where the *encoding*
  saturates.
- **ROT rounds to 6 decimals, not 1.** `(1/4.733)**2 == 0.044640288`; at one
  decimal that collapses to `0.0`, which reads as "not turning".
- **SOTDMA radio status is a raw integer.** The 19/20-bit sub-field layout is
  deferred to IALA and is not in our sources. Inventing it would be worse than
  not decoding it.
- **RMC/`utc_date` is passed through raw as `ddmmyy`.** Inventing a century for a
  2-digit year is a correctness landmine, not a convenience.
- **`--checksum` modes are `report` / `drop` / `require`.** The original plan
  said `report`/`ignore`/`drop`; `ignore` overlapped with `report`, so `require`
  (also reject a *missing* checksum) replaced it.
- **`#`-prefixed lines are skipped as comments.** `#` can never be a valid NMEA
  introducer, and capture logs commonly carry headers.

## 7. Traps

- **Nested git repo.** This directory sits inside `/Users/fanizuhri/TransTRACK/`,
  which is *its own* git repo — the AGS workspace repo (zero commits, everything
  excluded except `AGS/Development/AGS-Development.code-workspace`). It is
  harmless only because that repo's `.git/info/exclude` is `/*`. Tools that
  resolve "the repo" by walking up can still land on it. If you see `?? AGS/`
  in git status, you are in the outer repo. Recommended fix: move this directory
  out of `TransTRACK/` entirely.
- **`tests/` is exempt from E501** in `pyproject.toml` on purpose. Reference
  NMEA sentences must stay single literals to be diffable against the source
  they came from. Do not "fix" them by splitting the strings.
- **Comments in this codebase are deliberate.** They record spec reasoning that
  is not recoverable from the code — why a sentinel is what it is, why the ROT
  sign is applied outside the square. Do not strip them as noise. `ponytail:`
  markers name a known ceiling and its upgrade path; keep them.
- **`VesselStore._touch` reads the clock once per update** and uses that stamp
  for both `first_seen` and `last_seen`. A test depends on this.
- **Type 5 destination/DTE** are the only fields guarded by an explicit
  `remaining()` check. `decode_payload` degrades to `UnknownMessage` on
  `ShortPayload` rather than raising, and reports a payload too short to hold a
  type as `message_type == 0`.
- **`FragmentAssembler` caps pending at 64** with oldest-first eviction, and
  `VesselStore` is an unbounded dict. Both are marked `ponytail:`. Fine for
  file/pipe workloads; a long-running daemon needs TTL/LRU.

## 8. Not implemented — do not "helpfully" add these

| Missing | Why |
|---|---|
| AIS types 25, 26 | Variable-length field offsets; needs a different decoder shape |
| AIS types 6, 7, 8, 10, 12-17, 20, 22, 23 | Application-specific; still surface as `unknown` with raw payload |
| MID → flag state table | ~250 static ITU rows; `mid` integer is already emitted |
| Serial / TCP / UDP input | Would add a dependency; pipe from `nc`/`socat` instead |

## 9. Open items

- **Only `README.md` is committed.** Every source file, test, `Pipfile`,
  `.gitignore`, and the CI config are untracked. `git ls-files` is the check.
  The remote is `git@github.com:FaniZuhri/AIS-Data-Parser.git`.
- **`LICENSE` names TransTRACK** as copyright holder. Confirm before publishing
  if this is a personal repo.
- Git is **default-deny and one-shot** in this environment: run no mutating git
  command unless the user asks for it *in that turn*, and treat the grant as
  spent once it runs. Read-only git (`status`, `log`, `diff`, `show`,
  `rev-parse`, `ls-files`) is always fine.
- Nothing outside this directory may be edited without explicit per-change
  approval, including `~/.commandcode/`. The project's
  `.commandcode/settings.json` holds only a permission allowlist — never write
  to it directly.
