# Digi RealPort Serial Stress Tester

A Linux Python replacement for the Windows `rptest.exe` utility used to stress test Digi RealPort serial devices. Sends and receives data across port pairs, verifies integrity, tracks dropped bytes, and logs results.

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

## Reliability Fixes

Earlier versions could report dropped bytes at the very end of a transfer even on a clean link. The received data was byte-for-byte correct up to a point and then truncated — a draining/teardown problem, not data corruption. Two fixes address this:

- **The transmit buffer is now always flushed before a port closes.** `write()` only *queues* bytes; without an explicit flush, closing the port could discard whatever was still sitting in the OS/RealPort transmit buffer, dropping the tail of the transfer. Flushing now happens on every port close regardless of flags. (Previously this only occurred with `-flb`.)
- **The receiver drains until the line goes quiet instead of stopping on a fixed timer.** After the senders finish, each receive port keeps reading until no new data has arrived for a short grace period (governed by `-rtc`, minimum 2 seconds), then stops on its own. A hard cap of `ttr + 60 s` prevents an indefinite hang on a genuinely stuck link.

If you still see tail-end drops after these fixes, raise `-rtc` to widen the quiet-drain window, and double-check hardware flow control wiring (see Troubleshooting).

---

## Requirements

**Python:** 3.6 or later

**Dependencies:**

```bash
pip install pyserial --break-system-packages
```

GUI only (not needed for CLI):

```bash
pip install PyQt5 --break-system-packages
```

**Linux packages** (if PyQt5 install fails):

```bash
sudo apt install python3-pyqt5
```

---

## Port Mapping

Port indices used in the command map to Linux device nodes as follows:

| Index | DUT Port (transmit) | AUX Port (receive) |
|---|---|---|
| 0 | `/dev/ttye800` | `/dev/ttyTS00` |
| 1 | `/dev/ttye801` | `/dev/ttyTS01` |
| 2 | `/dev/ttye802` | `/dev/ttyTS02` |
| 3 | `/dev/ttye803` | `/dev/ttyTS03` |
| N | `/dev/ttye8NN` | `/dev/ttyTSNN` |

DUT ports are the RealPort virtual serial ports. AUX ports are the loopback/test side. Both must be present and accessible before running.

---

## Running Without the GUI (CLI)

### Basic syntax

```
python3 rptest_run.py [PORT GROUP] [OPTIONS]
```

At least one port group flag (`-bxp`, `-txp`, or `-rxp`) is required.

### Quickstart examples

**4-port bidirectional test, 60 seconds at 115200 baud:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000
```

**Single port pair, 30 seconds at 9600 baud:**
```bash
python3 rptest_run.py -bxp 0 -bps 9600 -ttr 30000
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

**Software flow control (Xon/Xoff):**
```bash
python3 rptest_run.py -bxp 0-3 -bps 9600 -ttr 30000 -xon 1
```

**Separate TX and RX port groups (TX on 0-1, RX on 2-3):**
```bash
python3 rptest_run.py -txp 0-1 -rxp 2-3 -bps 9600 -ttr 30000
```

**Custom test pattern (single byte, repeating):**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -tbp 0xAA
```

**Load test pattern from a binary file:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -ctx /path/to/pattern.bin
```

**Verbose debug output:**
```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 30000 -dex 1
```

**Stop Ctrl+C at any time** to abort a running test cleanly.

---

## CLI Reference

### Port designation

| Flag | Description |
|---|---|
| `-bxp PORTS` | Bidirectional ports — each port both sends and receives (loopback pair). Most common. |
| `-txp PORTS` | Transmit-only ports |
| `-rxp PORTS` | Receive-only ports |

Port specs support ranges and comma lists: `0-3`, `0,2,4`, `0-1,3`.

### Iteration control

| Flag | Default | Description |
|---|---|---|
| `-rep N` | `1` | Number of test repetitions. `-1` = run until failure |
| `-slp MS` | `0` | Sleep between repetitions in milliseconds |
| `-clo 0\|1` | `1` | Close and reopen ports between repetitions (1=yes) |

### Port settings

| Flag | Default | Description |
|---|---|---|
| `-bps BAUD` | `9600` | Baud rate. Valid values: 50, 75, 110, 300, 600, 1200, 9600, 14400, 19200, 28800, 57600, 115200, 230400 |
| `-dbs N` | `8` | Data bits: 5, 6, 7, or 8 |
| `-par N` | `0` | Parity: 0=None, 1=Odd, 2=Even, 3=Mark, 4=Space |
| `-sbs N` | `0` | Stop bits: 0=1bit, 1=1.5bits, 2=2bits |

### Flow control

| Flag | Default | Description |
|---|---|---|
| `-rts N` | `0` | RTS: 0=disable, 1=enable, 2=handshake, 3=toggle |
| `-cts N` | `0` | CTS: -1=disable, 0=hsIfRtsToggle, 1=handshake |
| `-dtr N` | `0` | DTR: 0=disable, 1=enable, 2=handshake |
| `-xon N` | `0` | Xon/Xoff: 0=disable, 1=enable |

For hardware flow control use `-rts 2 -cts 1` together.

### Timing and buffer

| Flag | Default | Description |
|---|---|---|
| `-ttr MS` | `-1` | Time to run in milliseconds. `-1` = run forever (until `-rep` limit or Ctrl+C) |
| `-bss N` | `1152` | Chunk size in bytes sent per write call |
| `-nob N` | `-1` | Max buffers (write calls) per port per iteration. `-1` = unlimited |
| `-btw MS` | `0` | Sleep between consecutive write calls in milliseconds |
| `-rtc MS` | `15600` | Read total timeout constant. Also sets the post-transfer quiet-drain grace period on receive (minimum 2000 ms). |
| `-wtc MS` | `15600` | Write total timeout constant in milliseconds |
| `-qui N` | `4096` | Input queue size in bytes |
| `-quo N` | `4096` | Output queue size in bytes |

### Test pattern

| Flag | Description |
|---|---|
| `-tbp HEX` | Hex pattern to repeat, e.g. `0xAA` or `0xFF`. Default is a 0x00–0xFF cycling ramp. |
| `-ctx FILE` | Load raw binary file as the test pattern (tiled to fill the buffer size) |

### Output and behavior

| Flag | Default | Description |
|---|---|---|
| `-ver 0\|1` | `1` | Verify received bytes against sent bytes. Reports first mismatch offset. |
| `-dex 0\|1` | `0` | Verbose/debug logging — prints per-buffer throughput and detailed RX events |
| `-res 0\|1` | `0` | Retry the write on timeout instead of stopping |
| `-flb 0\|1` | `0` | Additionally purge the output buffer when the iteration finishes. The TX buffer is **always** flushed on port close regardless of this setting; `-flb` adds an explicit output-buffer purge on top. |

---

## Running With the GUI

```bash
python3 rptest_gui.py
```

The GUI launches a dark-themed window with four configuration tabs. It builds and runs `rptest_run.py` as a subprocess, streams its output into a color-coded log panel, and displays live stats.

### Tabs

**Ports tab**
- Set bidirectional, TX-only, and RX-only port ranges
- Configure iteration count, sleep between reps, and whether to close ports between reps
- Toggle data verification and verbose logging

**Serial Settings tab**
- Baud rate, data bits, parity, stop bits
- Test pattern (hex value or binary file)

**Timing & Buffer tab**
- Time to run, sleep between I/O ops, read/write timeouts
- Buffer/chunk size, max buffers, input/output queue sizes

**Flow Control tab**
- RTS, CTS, DTR, Xon/Xoff settings
- Retry-on-timeout and flush-on-finish options

### Live stats bar

Displayed across the top of the output panel while a test is running:

| Field | Description |
|---|---|
| Iteration | Current iteration number |
| Pass | Cumulative passed iterations |
| Fail | Cumulative failed iterations (turns red on first failure) |
| Last Sent | Bytes sent in the most recent reported port pair |
| Last Recv | Bytes received in the most recent reported port pair |
| Last Drop | Dropped bytes in the most recent report (turns red if > 0) |
| Elapsed | Wall-clock time since test started |

### Command preview

The bottom of the window shows the exact command that will be run. You can copy this and run it directly from the terminal for automation or scripting.

### Log output colors

| Color | Meaning |
|---|---|
| Green | PASS result |
| Red | FAIL, ERROR, or dropped bytes |
| Cyan | Section headers and result blocks |
| Yellow | Warnings |
| Gray | Normal info lines |

### Buttons

- **Start Test** — launches the test with current settings
- **Stop** — sends SIGTERM to the subprocess and waits for it to exit cleanly
- **Clear** — clears the log display (does not affect the log file)
- **Save log…** — saves the current log display to a file of your choice

---

## Log Files

Each run automatically rotates logs:

- `serial_test.log` — the current run's log
- `serial_test_old.log` — the previous run's log (overwritten each time)

Logs are capped at 5 MB. Both files are written to the same directory the script is run from.

---

## Output Format

At the end of each iteration the script prints a results block:

```
============================================================
ITERATION 1 RESULTS
============================================================
  /dev/ttye800 -> /dev/ttyTS00
    Sent:         691200 bytes  (   11520 B/s)
    Received:     691200 bytes  (   11520 B/s)
    Dropped:           0 bytes
  TOTALS  sent=2764800  received=2764800  dropped=0
============================================================
DATA VERIFICATION
============================================================
  PASS  /dev/ttye800 -> /dev/ttyTS00  (691200 bytes match)
  PASS  /dev/ttye801 -> /dev/ttyTS01  (691200 bytes match)
  PASS  /dev/ttye802 -> /dev/ttyTS02  (691200 bytes match)
  PASS  /dev/ttye803 -> /dev/ttyTS03  (691200 bytes match)
============================================================
Iteration 1: PASS  (1 pass / 0 fail)
```

A verification failure shows the first byte offset where sent and received data diverge:

```
  FAIL  /dev/ttye800 -> /dev/ttyTS00  sent=691200B received=691187B first mismatch @ byte 45032
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All iterations passed |
| `1` | One or more iterations failed (dropped bytes, verify failure, or port error) |

This makes the script suitable for use in shell scripts and CI pipelines:

```bash
python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep 5
if [ $? -ne 0 ]; then
    echo "Serial stress test FAILED"
    exit 1
fi
```

---

## Troubleshooting

**`No such file or directory` when opening ports**
- Confirm the RealPort driver is loaded: `ls /dev/ttye8*` and `ls /dev/ttyTS*`
- Port numbers in the command are zero-based indexes — port `0` maps to `/dev/ttye800` and `/dev/ttyTS00`

**`Permission denied` on port open**
- Add your user to the `dialout` group: `sudo usermod -aG dialout $USER` then log out and back in
- Or run with `sudo python3 rptest_run.py ...`

**Dropped bytes only at the end of a transfer (tail truncation)**
- This was a known issue in earlier versions and is addressed by the fixes in the Reliability Fixes section above. Make sure you're running the current `rptest_run.py`.
- If it still occurs, increase `-rtc` (e.g. `-rtc 30000`) to widen the post-transfer quiet-drain window, which gives slow or bursty RealPort links more time to deliver the final bytes.

**All bytes dropped / nothing received**
- Check that the loopback cable or crossover wiring is correct between DUT and AUX ports
- Try a lower baud rate first (e.g. `-bps 9600`) to rule out signal integrity issues
- If using flow control, make sure both ends are configured the same way (`-rts 2 -cts 1` on both)

**GUI won't launch — `No module named 'PyQt5'`**
```bash
pip install PyQt5 --break-system-packages
# or
sudo apt install python3-pyqt5
```

**Test stops immediately with a port error**
- Run with `-dex 1` for verbose output to see which port failed and why
- Check `serial_test.log` for the full error message
