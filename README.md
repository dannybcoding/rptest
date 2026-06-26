# Digi RealPort Serial Stress Tester

A Linux Python replacement for the Windows `rptest.exe` utility used to stress
test Digi RealPort serial devices. It sends and receives data across port pairs,
verifies integrity at the frame level, reports throughput and dropped/extra
bytes, isolates failures per port, and logs results.

---

## Files

| File | Purpose |
|---|---|
| `rptest_run.py` | CLI stress test script — run directly or called by the GUI |
| `rptest_gui.py` | PyQt5 GUI launcher for `rptest_run.py` |
| `serial_test.log` | Current test log (auto-created on first run) |
| `serial_test_old.log` | Previous test log (auto-rotated on each new run) |

Both `.py` files must be in the same directory.

---

## How Verification Works

Each buffer is sent as a self-describing **frame**, not as an anonymous byte
stream:

```
+--------+--------+--------+-----------+--------+
| SYNC   | SEQ    | LEN    | PAYLOAD   | CRC32  |
| 4 B    | 4 B    | 2 B    | LEN B     | 4 B    |
+--------+--------+--------+-----------+--------+
```

- **SYNC** lets the receiver re-find frame boundaries after a gap.
- **SEQ** is a per-port counter (0, 1, 2, …) — detects dropped, duplicated, or
  reordered buffers.
- **CRC32** (over SEQ + LEN + PAYLOAD) detects corruption.

Because each frame carries its own identity, a single dropped byte no longer
smears the whole comparison: the receiver reports *exactly* which frames were
**dropped**, **corrupted**, **duplicated/out-of-order**, plus any **extra**
bytes (received more than sent — duplication or injected line noise). Frame
overhead is 14 bytes, so the usable payload per buffer is `-bss` minus 14.

Verification runs incrementally as data arrives, so memory stays flat regardless
of how long a soak test runs.

**Per-port isolation:** one bad port no longer tears down the others. If a port
errors mid-run or fails to open, it is recorded and skipped; the remaining ports
finish normally, and the report lists which ports failed and which survived.

---

## Reliability Fixes

Earlier versions could report dropped bytes at the very end of a transfer even on
a clean link — a draining/teardown problem, not data corruption. Two fixes
address this:

- **The transmit buffer is always flushed before a port closes.** `write()` only
  *queues* bytes; without an explicit flush, closing the port could discard the
  tail of the transfer. Flushing now happens on every port close regardless of
  flags. (Previously this only occurred with `-flb`.)
- **The receiver drains until the line goes quiet** instead of stopping on a
  fixed timer. After the senders finish, each receive port keeps reading until no
  new data has arrived for a short grace period (governed by `-rtc`, minimum 2
  seconds), then stops on its own. A hard cap of `ttr + 60 s` prevents an
  indefinite hang on a genuinely stuck link.

If you still see tail-end drops, raise `-rtc` to widen the quiet-drain window and
double-check flow-control wiring (see Troubleshooting).

---

## Requirements

**Python:** 3.6 or later

```bash
pip install pyserial --break-system-packages
```

GUI only (not needed for CLI):

```bash
pip install PyQt5 --break-system-packages
# or, if that fails:
sudo apt install python3-pyqt5
```

---

## Port Mapping

Port indices map to Linux device nodes as `/dev/tty<ID><NN>`, where `<ID>` is the
device family and `<NN>` is the zero-padded index. The families default to `e8`
(DUT/transmit) and `TS` (AUX/receive) and are configurable with `-dut`/`-aux`:

| Index | DUT (`-dut e8`) | AUX (`-aux TS`) |
|---|---|---|
| 0 | `/dev/ttye800` | `/dev/ttyTS00` |
| 1 | `/dev/ttye801` | `/dev/ttyTS01` |
| N | `/dev/ttye80N` | `/dev/ttyTS0N` |

For example, `-dut E8 -aux T8` maps index 0 to `/dev/ttyE800` and `/dev/ttyT800`.
Both ends must be present and accessible before running.

---

## Running Without the GUI (CLI)

```
python3 rptest_run.py [PORT GROUP] [OPTIONS]
```

At least one port group flag (`-bxp`, `-txp`, or `-rxp`) is required.

### Quickstart examples

**4-port bidirectional test, 60 seconds at 115200 baud:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000
```

**8 pairs with explicit device families (as on many Digi units):**
```bash
python3 rptest_run.py -bxp 0-7 -dut E8 -aux T8 -bps 115200 -ttr 6000
```

**Run 10 times, sleep 2 seconds between iterations:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep 10 -slp 2000
```

**Run continuously until a failure is detected:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep -1
```

**Hardware flow control (RTS/CTS handshake):**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rts 2 -cts 1
```

**One-directional TX → RX, choosing which device is which side:**
```bash
# e8 ports 0-1 transmit  ->  TS ports 0-1 receive
python3 rptest_run.py -txp e8:0-1 -rxp TS:0-1 -bps 9600 -ttr 30000

# reverse: TS transmits  ->  e8 receives
python3 rptest_run.py -txp TS:0-1 -rxp e8:0-1 -bps 9600 -ttr 30000
```
The optional `ID:` prefix selects the device family for that side. Omit it to use
the `-dut` family for TX and the `-aux` family for RX. The two sides pair by
position, so list matching ports in the same order and use opposite families at
the same index (the two ends of one cable).

**Custom test pattern / pattern from file:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -tbp 0xAA
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -ctx /path/to/pattern.bin
```

**Verbose debug output:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 30000 -dex 1
```

Press **Ctrl+C** at any time to stop a running test — it shuts down gracefully
and still prints its summary.

---

## CLI Reference

### Port designation

| Flag | Description |
|---|---|
| `-bxp PORTS` | Bidirectional — each pair runs **both** directions simultaneously. Most common. |
| `-txp [ID:]PORTS` | Transmit side of a one-directional test. Optional `ID:` chooses the transmitting family (default: `-dut`). |
| `-rxp [ID:]PORTS` | Receive side. Optional `ID:` chooses the receiving family (default: `-aux`). Pairs with `-txp` by position. |
| `-dut ID` | DUT device family (default `e8`) → `/dev/tty<ID><NN>` |
| `-aux ID` | AUX device family (default `TS`) → `/dev/tty<ID><NN>` |

Port specs support ranges and comma lists: `0-3`, `0,2,4`, `0-1,3`.

### Iteration control

| Flag | Default | Description |
|---|---|---|
| `-rep N` | `1` | Number of repetitions. `-1` = run until failure |
| `-slp MS` | `0` | Sleep between repetitions (ms) |
| `-clo 0\|1` | `1` | Close and reopen ports between repetitions |

### Port settings

| Flag | Default | Description |
|---|---|---|
| `-bps BAUD` | `9600` | Baud: 50, 75, 110, 300, 600, 1200, 9600, 14400, 19200, 28800, 57600, 115200, 230400 |
| `-dbs N` | `8` | Data bits: 5, 6, 7, or 8 |
| `-par N` | `0` | Parity: 0=None, 1=Odd, 2=Even, 3=Mark, 4=Space |
| `-sbs N` | `0` | Stop bits: 0=1, 1=1.5, 2=2 |

### Flow control

| Flag | Default | Description |
|---|---|---|
| `-rts N` | `0` | RTS: 0=off, 1=on, 2=handshake, 3=toggle (RS-485) |
| `-cts N` | `0` | CTS: -1=off, 0=hsIfRtsToggle, 1=handshake |
| `-dtr N` | `0` | DTR: 0=off, 1=on, 2=handshake |
| `-xon N` | `0` | Xon/Xoff software flow control: 0=off, 1=on |

**What actually applies on Linux:** `-rts 2 -cts 1` together enable RTS/CTS
hardware flow control; `-xon 1` enables software flow control; `-rts`/`-dtr` of
0/1 drive the line low/high. `-rts 3` attempts RS-485 auto-direction control via
the kernel (`TIOCSRS485`) — this is **not** supported on RealPort virtual ports
and will log a warning if unavailable. `-dtr 2` (DSR/DTR handshake) and
`-cts 0` have no effect on Linux serial and log a one-time warning rather than
being silently ignored.

### Timing and buffer

| Flag | Default | Description |
|---|---|---|
| `-ttr MS` | `-1` | Time to run (ms). `-1` = run until `-rep` limit or stop |
| `-bss N` | `1152` | On-wire frame size (bytes). Usable payload is this minus 14 B of frame overhead |
| `-nob N` | `-1` | Max buffers per port per iteration. `-1` = unlimited |
| `-btw MS` | `0` | Sleep between consecutive writes (ms) |
| `-rtc MS` | `15600` | Read timeout; also the post-transfer quiet-drain grace (min 2000 ms) |
| `-wtc MS` | `15600` | Write timeout (ms) |
| `-qui N` | `4096` | Input queue size (bytes) |
| `-quo N` | `4096` | Output queue size (bytes) |

### Test pattern

| Flag | Description |
|---|---|
| `-tbp HEX` | Hex pattern to repeat, e.g. `0xAA`. Default is a 0x00–0xFF cycling ramp. |
| `-ctx FILE` | Load a raw binary file as the test pattern (tiled to fill the payload) |

### Output and behavior

| Flag | Default | Description |
|---|---|---|
| `-ver 0\|1` | `1` | Frame-level verification: reports dropped / corrupted / out-of-order frames per link |
| `-dex 0\|1` | `0` | Verbose/debug logging (per-buffer throughput, detailed RX events) |
| `-res 0\|1` | `0` | Retry the write on timeout instead of stopping |
| `-flb 0\|1` | `0` | Purge the output buffer when an iteration finishes (the TX buffer is always flushed on close regardless) |
| `--logfile PATH` | `serial_test.log` | Log file path |
| `--json` | off | Also emit structured JSON-lines events on stdout (used by the GUI) |

---

## Running With the GUI

```bash
python3 rptest_gui.py
```

A dark-themed window with four configuration tabs (Ports, Serial Settings,
Timing & Buffer, Flow Control). It builds and runs `rptest_run.py` as a
subprocess with `--json`, drives its live stats from the structured event stream
(not log scraping), and streams the human-readable log into a color-coded panel.

### Live stats bar

| Field | Description |
|---|---|
| Iteration | Current iteration number |
| Pass | Cumulative passed iterations |
| Fail | Cumulative failed iterations (red on first failure) |
| Sent | Total bytes sent in the latest iteration |
| Recv | Total bytes received in the latest iteration |
| Dropped | Dropped bytes (red if > 0) |
| Extra | Bytes received in excess of sent — duplication/noise (red if > 0) |
| Elapsed | Wall-clock time since the test started |

### Command preview

The bottom of the window shows the exact command that will be run (including
`--json`). Copy it to run directly from a terminal for automation.

### Buttons

- **Start Test** — launches the test with current settings
- **Stop** — sends SIGTERM; the backend drains and prints its summary before
  exiting (a hard kill is only used as a fallback if it hangs). Reported as
  "stopped by user," not a failure.
- **Clear** — clears the log display (does not affect the log file)
- **Save log…** — saves the current log display to a file

---

## Output Format

Each iteration prints a per-direction results block, a modem snapshot, and a
frame-level verification block:

```
============================================================
ITERATION 1 RESULTS
============================================================
  /dev/ttyE800 -> /dev/ttyT800
    Sent:          42624 bytes  (    6909 B/s over   6.17s)
    Received:      42624 bytes  (    6932 B/s over   6.15s)
    Dropped:           0 bytes
  TOTALS  sent=675072  received=675072  dropped=0  extra=0
============================================================
MODEM STATUS
  /dev/ttyE800: CTS=1 DSR=1 CD=0 RI=0
============================================================
DATA VERIFICATION (frame-level, streaming)
============================================================
  PASS  /dev/ttyE800 -> /dev/ttyT800  (36 frames, all present and in order)
============================================================
Iteration 1: PASS  (1 pass / 0 fail)
```

Throughput is measured over the actual active window per port (the `over X.XXs`),
not the configured run time. `MODEM STATUS` reports the CTS/DSR/CD/RI line states
(kernel framing/parity/overrun counters are not available on RealPort, so they
are not shown).

A verification failure names exactly what went wrong on that link:

```
  FAIL  /dev/ttyE800 -> /dev/ttyT800
        sent=36  received_ok=34  dropped=2  out_of_order=0  corrupted=1
        dropped SEQs: 12, 19
```

If a port errors or fails to open, a `PORT STATUS` block lists the failures and
the survivors (the other ports still run and report normally):

```
============================================================
PORT STATUS
  ERRORED OPEN /dev/ttyT803: device busy
  Survived: ['/dev/ttyE800', '/dev/ttyT800', '/dev/ttyE801', ...]
============================================================
```

---

## Log Files

Each run rotates logs: `serial_test.log` (current) and `serial_test_old.log`
(previous, overwritten each run). Logs are capped at 5 MB and written to the
directory the script runs from. Use `--logfile PATH` to change the location.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All iterations passed |
| `1` | One or more iterations failed (dropped/extra bytes, verify failure, or port error) |

Suitable for shell scripts and CI:

```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep 5 || {
    echo "Serial stress test FAILED"; exit 1; }
```

---

## Troubleshooting

**`No such file or directory` when opening ports**
- Confirm the RealPort driver is loaded and the nodes exist (e.g. `ls /dev/ttyE8*`).
- Port numbers are zero-based; index `0` maps to `<dut>00` and `<aux>00`.

**`Permission denied` on port open**
- Add your user to `dialout`: `sudo usermod -aG dialout $USER` (then re-login), or run with `sudo`.

**Dropped bytes only at the end of a transfer (tail truncation)**
- Addressed by the fixes in *Reliability Fixes*. Make sure you're on the current `rptest_run.py`.
- If it persists, increase `-rtc` (e.g. `-rtc 30000`) to widen the quiet-drain window.

**All bytes dropped / nothing received**
- Check the loopback/crossover wiring between DUT and AUX ports.
- Try a lower baud rate (e.g. `-bps 9600`) first.
- With flow control, configure both ends the same way (`-rts 2 -cts 1`).

**`Extra` bytes reported**
- The receiver got more than was sent — duplication or injected line noise. Check wiring and the device's flow-control configuration.

**One port fails but others pass**
- Expected behavior: ports are isolated. See the `PORT STATUS` block for which port failed and why; run with `-dex 1` and check `serial_test.log` for detail.

**GUI won't launch — `No module named 'PyQt5'`**
- `pip install PyQt5 --break-system-packages` or `sudo apt install python3-pyqt5`.
