"""
rptest_run.py  -  Linux RealPort Serial Stress Tester
Equivalent to the Windows rptest.exe utility for Digi RealPort devices.

Usage examples (mirroring rptest syntax):
  # Basic bidirectional test on 4 port pairs, 60 sec, 115200 baud
  python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000

  # One-directional: e8 ports 0-1 transmit -> TS ports 0-1 receive
  python3 rptest_run.py -txp e8:0-1 -rxp TS:0-1 -bps 9600 -ttr 30000

  # Reverse direction: TS transmits -> e8 receives
  python3 rptest_run.py -txp TS:0-1 -rxp e8:0-1 -bps 9600 -ttr 30000

  # Run 10 iterations, sleep 2s between, verify data, detailed log
  python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep 10 -slp 2000 -ver 1 -dex 1

  # Run until failure, hardware flow control (RTS/CTS handshake)
  python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep -1 -rts 2 -cts 1

Port numbering maps to:
  Device node : /dev/tty<ID><Portnumber>   (e.g. e8 port 0 = /dev/ttye800,
                                                  TS port 0 = /dev/ttyTS00)
  -bxp uses the -dut and -aux families as the two ends and runs both directions.
  -txp/-rxp may name the device family explicitly (ID:ports) to choose which
  side transmits and which receives; without an id they default to -dut (tx)
  and -aux (rx).
"""

import serial
import time
import threading
import logging
import sys
import argparse
import os
import re
import zlib
import json
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Structured event stream (--json)
# ---------------------------------------------------------------------------
# When --json is set, the runner prints one JSON object per line to stdout, in
# addition to the human-readable log. The GUI consumes these structured events
# for its live stats instead of regex-scraping log strings, so changing a log
# message can never silently break the display. Each event is a dict with a
# "type" field. Human log lines and JSON lines share stdout; the GUI tells them
# apart by trying json.loads (log lines never start with '{').
JSON_OUT = False

def emit_event(obj):
    """Emit one structured event as a JSON line (no-op unless --json is set)."""
    if JSON_OUT:
        try:
            print(json.dumps(obj), flush=True)
        except (TypeError, ValueError):
            pass

# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
CURRENT_LOG = 'serial_test.log'
BACKUP_LOG  = 'serial_test_old.log'

def rotate_log():
    if os.path.exists(CURRENT_LOG):
        if os.path.exists(BACKUP_LOG):
            os.remove(BACKUP_LOG)
        os.rename(CURRENT_LOG, BACKUP_LOG)

rotate_log()

handler = RotatingFileHandler(CURRENT_LOG, maxBytes=5 * 1024 * 1024, backupCount=1)
logging.basicConfig(
    handlers=[handler],
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logging.getLogger('').addHandler(console)

# ---------------------------------------------------------------------------
# Global stop event
# ---------------------------------------------------------------------------
stop_event = threading.Event()

# ---------------------------------------------------------------------------
# Port-name helpers
# ---------------------------------------------------------------------------
#DUT_PREFIX = -dut
#AUX_PREFIX = -aux

#def port_index_to_names(index):
#    return (f"{DUT_PREFIX}{index:02d}", f"{AUX_PREFIX}{index:02d}")

def device_port_name(device_id, index):
    """Build a full device node from a family id and a port index.
       ('e8', 0) -> '/dev/ttye800' ;  ('TS', 12) -> '/dev/ttyTS12'."""
    return f"/dev/tty{device_id}{index:02d}"

def port_index_to_names(index, dut_id, aux_id):
    return (device_port_name(dut_id, index), device_port_name(aux_id, index))

def parse_port_range(spec):
    """
    Parse rptest-style port spec '0-3' or '0,2,4-6' into sorted list of ints.
    """
    indices = set()
    for part in spec.split(','):
        part = part.strip()
        m = re.match(r'^(\d+)-(\d+)$', part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            indices.update(range(lo, hi + 1))
        elif re.match(r'^\d+$', part):
            indices.add(int(part))
        else:
            raise argparse.ArgumentTypeError(f"Invalid port spec: '{part}'")
    return sorted(indices)


def parse_device_ports(spec, default_id):
    """
    Parse a TX/RX port spec that may carry a leading device-family id, so the
    caller can choose which device transmits and which receives:

        'e8:0-3'  -> ('e8', [0, 1, 2, 3])     # this family is the TX/RX side
        'TS:0,2'  -> ('TS', [0, 2])
        '0-3'     -> (default_id, [0, 1, 2, 3])  # no id => fall back to default
        ''        -> (default_id, [])

    default_id is the -dut family for -txp and the -aux family for -rxp, which
    preserves the original behaviour when no id is given.
    """
    spec = (spec or '').strip()
    if not spec:
        return default_id, []
    if ':' in spec:
        dev, _, rng = spec.partition(':')
        dev = dev.strip()
        return (dev or default_id), parse_port_range(rng)
    return default_id, parse_port_range(spec)

# ---------------------------------------------------------------------------
# Test-buffer pattern helpers
# ---------------------------------------------------------------------------
def build_pattern(tbp_hex=None, ctx_file=None, buf_size=1152):
    if ctx_file:
        with open(ctx_file, 'rb') as f:
            raw = f.read()
        return (raw * ((buf_size // len(raw)) + 1))[:buf_size]

    if tbp_hex:
        clean = tbp_hex.replace('0x', '').replace('-', '')
        try:
            base_bytes = bytes.fromhex(clean)
        except ValueError:
            logging.warning(f"Could not parse tbp '{tbp_hex}', using default 0x00-FF pattern.")
            base_bytes = bytes(range(256))
    else:
        base_bytes = bytes(range(256))

    return (base_bytes * ((buf_size // len(base_bytes)) + 1))[:buf_size]

# ---------------------------------------------------------------------------
# Sequence-framed payload helpers
# ---------------------------------------------------------------------------
# Each buffer is wrapped in a self-describing frame so the receiver can tell
# *which* buffers arrived, in what order, and whether they were corrupted —
# instead of one opaque byte-stream compare that desyncs on a single dropped
# byte.
#
#   +--------+--------+--------+------------------+--------+
#   | SYNC   | SEQ    | LEN    | PAYLOAD          | CRC32  |
#   | 4 B    | 4 B BE | 2 B BE | LEN B            | 4 B BE |
#   +--------+--------+--------+------------------+--------+
#
#   SYNC : fixed marker, used only to re-locate frame boundaries after a gap.
#   SEQ  : per-port frame counter from 0 (detects drops / dupes / reordering).
#   LEN  : payload length in bytes.
#   CRC32: zlib.crc32 over SEQ + LEN + PAYLOAD (detects corruption).
#
# CRC validation makes the SYNC marker safe even if the same bytes occur inside
# a payload: a false hit fails CRC and the scanner advances one byte and keeps
# hunting. In the normal no-loss path the parser jumps frame-to-frame and never
# scans payload at all.

FRAME_SYNC     = b'\xA5\x5A\xA5\x5A'
FRAME_OVERHEAD = len(FRAME_SYNC) + 4 + 2 + 4      # sync + seq + len + crc = 14

def build_frame(seq, pattern, payload_len):
    """Wrap one payload slice in a sequence-numbered, CRC-protected frame."""
    payload = bytes(pattern[:payload_len])
    body    = seq.to_bytes(4, 'big') + payload_len.to_bytes(2, 'big') + payload
    crc     = zlib.crc32(body) & 0xFFFFFFFF
    return FRAME_SYNC + body + crc.to_bytes(4, 'big')

_SLEN = len(FRAME_SYNC)

def _try_frame(raw, i, n):
    """
    Attempt to read one frame starting at offset i in buffer raw (length n).

    Returns (kind, seq, next_i):
      'ok'         - valid frame; seq set; next_i = end of this frame
      'bad'        - SYNC matched but CRC failed; seq None; next_i = i + 1
      'nosync'     - no SYNC at i;              seq None; next_i = i + 1
      'incomplete' - SYNC matched but the frame runs past n; next_i = i
                     (caller should stop and wait for more bytes)
    """
    if raw[i:i + _SLEN] != FRAME_SYNC:
        return ('nosync', None, i + 1)
    length    = int.from_bytes(raw[i + _SLEN + 4:i + _SLEN + 6], 'big')
    frame_end = i + _SLEN + 6 + length + 4
    if frame_end > n:
        return ('incomplete', None, i)
    body     = raw[i + _SLEN:i + _SLEN + 6 + length]
    crc_recv = int.from_bytes(raw[frame_end - 4:frame_end], 'big')
    if (zlib.crc32(body) & 0xFFFFFFFF) == crc_recv:
        seq = int.from_bytes(raw[i + _SLEN:i + _SLEN + 4], 'big')
        return ('ok', seq, frame_end)
    return ('bad', None, i + 1)                # false / corrupt sync -> resync

def parse_frames(raw):
    """
    Batch parser: recover every frame from a complete byte buffer at once.
    Kept as a stateless utility (handy for offline analysis and self-tests);
    the live receive path uses FrameVerifier instead so memory stays flat.

    Returns (seqs, crc_errors, resync_bytes).
    """
    raw          = bytes(raw)
    n            = len(raw)
    seqs         = []
    crc_errors   = 0
    resync_bytes = 0
    i            = 0
    while i + FRAME_OVERHEAD <= n:
        kind, seq, nxt = _try_frame(raw, i, n)
        if kind == 'incomplete':
            break
        if kind == 'ok':
            seqs.append(seq)
        elif kind == 'bad':
            crc_errors   += 1
            resync_bytes += 1
        else:                                  # 'nosync'
            resync_bytes += 1
        i = nxt
    return seqs, crc_errors, resync_bytes


class FrameVerifier:
    """
    Streaming, constant-memory frame verifier — one instance per receiving port.

    Bytes are pushed in with feed() as they arrive. Each complete frame is folded
    into running counters and then thrown away, so memory stays flat no matter how
    long the run is, instead of growing one full payload per buffer the way an
    accumulate-then-compare approach does.

    A serial / RealPort link is a FIFO byte channel: frames cannot truly arrive
    out of order, so SEQ numbers from CRC-valid frames are expected to count up
    0, 1, 2, … A forward jump in SEQ is a dropped run; a SEQ at or below one we
    have already passed is a duplicate or framing anomaly (out_of_order).
    """
    MAX_DROP_RANGES = 32          # cap stored dropped-SEQ ranges for the report

    def __init__(self):
        self._buf          = bytearray()   # carry-over: at most one partial frame
        self.expected_next = 0
        self.received_ok   = 0
        self.dropped       = 0
        self.out_of_order  = 0
        self.crc_errors    = 0
        self.resync_bytes  = 0
        self.max_seq       = -1
        self.drop_ranges   = []
        self.unexpected    = False
        self._ranges_trunc = False

    def feed(self, chunk):
        buf = self._buf
        buf.extend(chunk)
        n = len(buf)
        i = 0
        while i + FRAME_OVERHEAD <= n:
            kind, seq, nxt = _try_frame(buf, i, n)
            if kind == 'incomplete':
                break                          # wait for the rest of this frame
            if kind == 'ok':
                self._account(seq)
            elif kind == 'bad':
                self.crc_errors   += 1
                self.resync_bytes += 1
            else:                              # 'nosync'
                self.resync_bytes += 1
            i = nxt
        if i:
            del buf[:i]                        # drop everything already consumed

    def _account(self, seq):
        if seq == self.expected_next:
            self.expected_next = seq + 1
        elif seq > self.expected_next:
            self._add_drop_range(self.expected_next, seq - 1)
            self.expected_next = seq + 1
        else:                                  # seq <= one we already passed
            self.out_of_order += 1
        self.received_ok += 1
        if seq > self.max_seq:
            self.max_seq = seq

    def _add_drop_range(self, start, end):
        self.dropped += (end - start + 1)
        if len(self.drop_ranges) < self.MAX_DROP_RANGES:
            self.drop_ranges.append((start, end))
        else:
            self._ranges_trunc = True

    def finalize(self, expected):
        """Call once after the run: any frames never seen at the tail are drops."""
        if self.expected_next < expected:
            self._add_drop_range(self.expected_next, expected - 1)
            self.expected_next = expected
        if self.max_seq >= expected:           # a SEQ the sender never wrote
            self.unexpected = True

    @property
    def ranges_complete(self):
        return not self._ranges_trunc

# ---------------------------------------------------------------------------
# I/O worker threads
# ---------------------------------------------------------------------------

def send_data(port, cfg, sent_count, sent_frames, sent_elapsed, pattern):
    # `port` may be shared with a receive_data thread (bidirectional -bxp).
    # This thread must NOT close the handle — closing would yank it out from
    # under its receive thread mid-drain. run_iteration closes centrally once
    # every thread has stopped.
    port_name   = port.port
    payload_len = cfg['payload_len']
    btw_sec     = cfg['btw'] / 1000.0
    ttr_sec     = cfg['ttr'] / 1000.0 if cfg['ttr'] >= 0 else None
    nob         = cfg['nob']
    seq         = 0
    start_time  = time.time()   # reset after warmup; defined here so finally is safe

    try:
        time.sleep(0.5)
        start_time   = time.time()   # the real send window starts after warmup
        buffers_sent = 0

        while not stop_event.is_set():
            if ttr_sec is not None and (time.time() - start_time) >= ttr_sec:
                break
            if nob >= 0 and buffers_sent >= nob:
                break

            frame = build_frame(seq, pattern, payload_len)
            try:
                port.write(frame)
            except serial.SerialTimeoutException:
                logging.warning(f"[{port_name}] Write timeout")
                if cfg['res']:
                    continue          # resend same seq; nothing counted yet
                break

            sent_count[port_name]  += len(frame)
            buffers_sent           += 1
            seq                    += 1
            sent_frames[port_name]  = buffers_sent

            if cfg['verbose']:
                elapsed = time.time() - start_time
                tput = sent_count[port_name] / elapsed if elapsed > 0 else 0
                logging.debug(f"[TX {port_name}] buf#{buffers_sent} "
                              f"total={sent_count[port_name]}B tput={tput:.0f}B/s")
            if btw_sec > 0:
                time.sleep(btw_sec)

    except Exception as e:
        logging.error(f"[TX {port_name}] Error: {e}")
        stop_event.set()
    finally:
        # Wall-clock the port was actively writing (measured before flush, which
        # is buffer-drain time, not active sending). Used for the real B/s figure.
        sent_elapsed[port_name] = max(time.time() - start_time, 0.0)
        # ALWAYS drain the TX buffer. write() only queues bytes; without this the
        # tail of the transfer can be discarded when the port is later closed.
        try:
            port.flush()              # blocks until all written data is transmitted
        except Exception:
            pass
        if cfg['flb']:
            try:
                port.reset_output_buffer()
            except Exception:
                pass


def receive_data(port, cfg, received_count, verifiers, recv_elapsed, senders_done):
    """
    Drain the RX port until the senders are finished AND no new data has
    arrived for `quiet_grace` seconds. Do NOT stop merely because the clock
    passed `ttr`: on network-attached serial (RealPort) the tail of a transfer
    arrives in bursts with small gaps, so a clock-based cutoff truncates data.

    `port` may be shared with a send_data thread (bidirectional -bxp); this
    thread must NOT close it — run_iteration closes centrally after all joins.
    """
    port_name   = port.port
    ttr_sec     = cfg['ttr'] / 1000.0 if cfg['ttr'] >= 0 else None
    rtc_sec     = cfg['rtc'] / 1000.0
    quiet_grace = max(rtc_sec, 2.0)          # how long to wait after the last byte
    # Absolute safety cap so a stuck link can't drain forever.
    hard_cap    = (ttr_sec + 60) if ttr_sec is not None else None

    first_data_time = None        # set on first byte; defined here so finally is safe
    last_data_time  = None

    try:
        time.sleep(0.5)
        start_time     = time.time()
        last_data_time = start_time

        while not stop_event.is_set():
            now = time.time()
            if hard_cap is not None and (now - start_time) > hard_cap:
                logging.warning(f"[RX {port_name}] hard cap reached, "
                                f"stopping drain (possible stuck link)")
                break

            port.timeout = min(rtc_sec, 1.0)
            n = port.in_waiting
            chunk = port.read(n if n > 0 else 1)

            if chunk:
                received_count[port_name] += len(chunk)
                if cfg['ver']:
                    verifiers[port_name].feed(chunk)   # fold + discard, flat memory
                now = time.time()
                if first_data_time is None:
                    first_data_time = now              # clock starts on the first byte
                last_data_time = now

                if cfg['verbose']:
                    elapsed = last_data_time - start_time
                    tput = received_count[port_name] / elapsed if elapsed > 0 else 0
                    logging.debug(f"[RX {port_name}] +{len(chunk)}B "
                                  f"total={received_count[port_name]}B tput={tput:.0f}B/s")
                continue

            # No data this round. Only quit once the senders are done sending
            # AND the line has been quiet for quiet_grace seconds.
            if senders_done.is_set() and (time.time() - last_data_time) >= quiet_grace:
                break
            logging.debug(f"[RX {port_name}] idle "
                          f"(senders_done={senders_done.is_set()})")

    except Exception as e:
        logging.error(f"[RX {port_name}] Error: {e}")
        stop_event.set()
    finally:
        # Wall-clock from the first byte to the last byte actually received — the
        # window data was flowing, which is the meaningful divisor for RX B/s.
        if first_data_time is not None and last_data_time is not None:
            recv_elapsed[port_name] = max(last_data_time - first_data_time, 0.0)
        else:
            recv_elapsed[port_name] = 0.0


# ---------------------------------------------------------------------------
# Data verification
# ---------------------------------------------------------------------------

def verify_data(sent_frames, verifiers, verify_pairs, iteration=None):
    """
    Frame-aware verification, read from the per-port streaming verifiers.

    The parsing already happened incrementally as bytes arrived (FrameVerifier),
    so here we just finalize each receiver and report what happened to each link:
    dropped, corrupted or out-of-order frames — instead of a single opaque
    "first mismatch @ byte N" that one dropped byte would smear across the stream.
    """
    all_ok = True
    logging.info("=" * 60)
    logging.info("DATA VERIFICATION (frame-level, streaming)")
    logging.info("=" * 60)

    for sender, receiver in verify_pairs:
        expected = sent_frames.get(sender, 0)
        v        = verifiers.get(receiver)
        if v is None:                          # verify disabled / no verifier
            continue
        v.finalize(expected)

        ok = (v.dropped == 0 and v.crc_errors == 0 and v.out_of_order == 0
              and not v.unexpected and v.received_ok == expected)

        emit_event({"type": "verify", "iteration": iteration,
                    "sender": sender, "receiver": receiver, "ok": ok,
                    "expected": expected, "received_ok": v.received_ok,
                    "dropped": v.dropped, "out_of_order": v.out_of_order,
                    "corrupted": v.crc_errors})

        if ok:
            logging.info(f"  PASS  {sender} -> {receiver}  "
                         f"({expected} frames, all present and in order)")
        else:
            all_ok = False
            logging.info(f"  FAIL  {sender} -> {receiver}")
            logging.info(f"        sent={expected}  received_ok={v.received_ok}  "
                         f"dropped={v.dropped}  out_of_order={v.out_of_order}  "
                         f"corrupted={v.crc_errors}")
            if v.drop_ranges:
                preview = ', '.join(f"{a}" if a == b else f"{a}-{b}"
                                    for a, b in v.drop_ranges[:10])
                more = '' if (v.ranges_complete and len(v.drop_ranges) <= 10) else ' (+more)'
                logging.info(f"        dropped SEQs: {preview}{more}")
            if v.unexpected:
                logging.info(f"        saw SEQ >= {expected} never sent "
                             f"(max seq seen = {v.max_seq})")
            if v.resync_bytes:
                logging.info(f"        {v.resync_bytes} byte(s) skipped during resync")

    logging.info("=" * 60)
    return all_ok

# ---------------------------------------------------------------------------
# Port open helper
# ---------------------------------------------------------------------------

def open_port(name, cfg):
    parity_map   = {0: serial.PARITY_NONE, 1: serial.PARITY_ODD,
                    2: serial.PARITY_EVEN, 3: serial.PARITY_MARK,
                    4: serial.PARITY_SPACE}
    stopbits_map = {0: serial.STOPBITS_ONE,
                    1: serial.STOPBITS_ONE_POINT_FIVE,
                    2: serial.STOPBITS_TWO}

    rtscts  = (cfg['rts'] == 2 and cfg['cts'] == 1)
    xonxoff = bool(cfg['xon'])

    port = serial.Serial(
        port          = name,
        baudrate      = cfg['bps'],
        bytesize      = cfg['dbs'],
        parity        = parity_map.get(cfg['par'], serial.PARITY_NONE),
        stopbits      = stopbits_map.get(cfg['sbs'], serial.STOPBITS_ONE),
        rtscts        = rtscts,
        xonxoff       = xonxoff,
        timeout       = cfg['rtc'] / 1000.0,
        write_timeout = cfg['wtc'] / 1000.0,
    )
    if cfg['rts'] == 1:
        port.rts = True
    if cfg['dtr'] == 1:
        port.dtr = True
    return port

# ---------------------------------------------------------------------------
# Modem line status
# ---------------------------------------------------------------------------
# Snapshot of the input modem control lines (CTS/DSR/CD/RI) per port, for a bit
# of protocol context alongside the byte counts. Read via pyserial's line
# properties; each guarded so an unsupported line just reports as unknown.
# (Kernel framing/parity/overrun counters aren't read: the RealPort driver
#  doesn't implement the TIOCGICOUNT ioctl, so there's nothing to report.)

def read_modem_lines(port):
    """Current state of the input modem control lines (True/False/None)."""
    out = {}
    for name in ('cts', 'dsr', 'cd', 'ri'):
        try:
            out[name] = bool(getattr(port, name))
        except Exception:
            out[name] = None
    return out

# ---------------------------------------------------------------------------
# Single iteration
# ---------------------------------------------------------------------------

def run_iteration(tx_dev, tx_indices, rx_dev, rx_indices, bx_indices, cfg, pattern, iteration_num):
    # Build sending ports, receiving ports, and directional (sender -> receiver)
    # verification pairs.
    #
    #   -bxp : TRUE bidirectional. For each index data flows BOTH ways
    #            DUT -> AUX  and  AUX -> DUT
    #          so both ports send AND receive.
    #   -txp/-rxp : one-directional DUT(tx) -> AUX(rx), as before.
    send_ports   = []   # ordered, unique
    recv_ports   = []   # ordered, unique
    verify_pairs = []   # list of (sender_name, receiver_name)

    def _add(seq, name):
        if name not in seq:
            seq.append(name)

    for i in bx_indices:
        dut, aux = port_index_to_names(i, cfg['dut'], cfg['aux'])
        _add(send_ports, dut); _add(recv_ports, aux); verify_pairs.append((dut, aux))
        _add(send_ports, aux); _add(recv_ports, dut); verify_pairs.append((aux, dut))

    # -txp/-rxp : one-directional. tx_dev transmits, rx_dev receives. The two
    # are paired by position, so list matching indices in the same order. Use
    # opposite families at the same index (the two ends of one cable) for data
    # to actually arrive, e.g. -txp e8:0-3 -rxp TS:0-3  (or the reverse).
    for ti, ri in zip(tx_indices, rx_indices):
        tx_name = device_port_name(tx_dev, ti)
        rx_name = device_port_name(rx_dev, ri)
        _add(send_ports, tx_name); _add(recv_ports, rx_name); verify_pairs.append((tx_name, rx_name))

    all_ports = []
    for n in send_ports + recv_ports:
        _add(all_ports, n)

    threads        = []
    tx_threads     = []
    rx_threads     = []
    sent_count     = {}
    received_count = {}
    sent_frames    = {}   # name -> number of frames written
    sent_elapsed   = {}   # name -> seconds the TX loop actually ran
    recv_elapsed   = {}   # name -> seconds from first to last byte received
    verifiers      = {}   # name -> FrameVerifier (streaming, flat memory)
    port_objs      = {}   # name -> Serial (opened once, may host TX and RX)
    senders_done   = threading.Event()

    logging.info(f"--- Iteration {iteration_num} start ---")
    emit_event({"type": "iteration_start", "iteration": iteration_num})

    try:
        # Open every participating port exactly once.
        for name in all_ports:
            try:
                port_objs[name] = open_port(name, cfg)
                logging.debug(f"Opened port: {name}")
            except Exception as e:
                logging.error(f"Cannot open port {name}: {e}")
                stop_event.set()
                for p in port_objs.values():
                    try:
                        p.close()
                    except Exception:
                        pass
                return False

        # One send thread per sending port.
        for name in send_ports:
            sent_count[name]   = 0
            sent_frames[name]  = 0
            sent_elapsed[name] = 0.0
            t = threading.Thread(
                target=send_data,
                args=(port_objs[name], cfg, sent_count, sent_frames, sent_elapsed, pattern),
                name=f"TX-{name}", daemon=True)
            threads.append(t); tx_threads.append(t)

        # One receive thread per receiving port.
        for name in recv_ports:
            received_count[name] = 0
            recv_elapsed[name]   = 0.0
            verifiers[name]      = FrameVerifier()
            t = threading.Thread(
                target=receive_data,
                args=(port_objs[name], cfg, received_count, verifiers, recv_elapsed, senders_done),
                name=f"RX-{name}", daemon=True)
            threads.append(t); rx_threads.append(t)

        for t in threads:
            t.start()

        ttr_sec = cfg['ttr'] / 1000.0 if cfg['ttr'] >= 0 else None

        # 1) Wait for the senders to finish. They self-terminate at ttr / nob.
        if ttr_sec is not None:
            send_wait = ttr_sec + 15
        else:
            # Continuous run: senders only stop when stop_event is set externally.
            while not stop_event.is_set():
                time.sleep(1)
            send_wait = 15
        for t in tx_threads:
            t.join(timeout=send_wait)

        # 2) Tell the receivers the senders are done. They keep draining until
        #    the line has been quiet for quiet_grace, so the in-flight tail of
        #    each transfer is fully captured before we tear down.
        senders_done.set()

        # 3) Let the receivers drain and exit on their own.
        rx_wait = (ttr_sec + 75) if ttr_sec is not None else 75
        for t in rx_threads:
            t.join(timeout=rx_wait)

        # 4) Now everything is done (or timed out) — signal global stop.
        stop_event.set()
        for t in threads:
            t.join(timeout=10)

        # Results — one block per direction.
        logging.info("=" * 60)
        logging.info(f"ITERATION {iteration_num} RESULTS")
        logging.info("=" * 60)
        dropped = {}
        extra   = {}
        for sender, receiver in verify_pairs:
            s      = sent_count.get(sender, 0)
            r      = received_count.get(receiver, 0)
            d      = max(0, s - r)
            x      = max(0, r - s)
            s_el   = sent_elapsed.get(sender, 0.0)
            r_el   = recv_elapsed.get(receiver, 0.0)
            s_rate = s / s_el if s_el > 0 else 0
            r_rate = r / r_el if r_el > 0 else 0
            dropped[(sender, receiver)] = d
            extra[(sender, receiver)]   = x
            logging.info(f"  {sender} -> {receiver}")
            logging.info(f"    Sent:     {s:>10} bytes  ({s_rate:>8.0f} B/s over {s_el:6.2f}s)")
            logging.info(f"    Received: {r:>10} bytes  ({r_rate:>8.0f} B/s over {r_el:6.2f}s)")
            logging.info(f"    Dropped:  {d:>10} bytes")
            if x:
                logging.info(f"    Extra:    {x:>10} bytes  (received MORE than sent)")
            emit_event({"type": "pair_result", "iteration": iteration_num,
                        "sender": sender, "receiver": receiver,
                        "sent": s, "received": r, "dropped": d, "extra": x,
                        "sent_bps": round(s_rate), "recv_bps": round(r_rate),
                        "sent_s": round(s_el, 2), "recv_s": round(r_el, 2)})
        total_sent     = sum(sent_count.values())
        total_received = sum(received_count.values())
        logging.info(f"  TOTALS  sent={total_sent}  received={total_received}  "
                     f"dropped={sum(dropped.values())}  extra={sum(extra.values())}")
        logging.info("=" * 60)
        emit_event({"type": "iteration_totals", "iteration": iteration_num,
                    "sent": total_sent, "received": total_received,
                    "dropped": sum(dropped.values()), "extra": sum(extra.values())})

        # Modem line snapshot — informational context, no effect on pass/fail.
        logging.info("MODEM STATUS")
        modem_all = {}
        for name in all_ports:
            modem = read_modem_lines(port_objs[name])
            modem_all[name] = modem
            modem_str = ' '.join(
                f"{k.upper()}={'1' if v else '0'}" if v is not None else f"{k.upper()}=?"
                for k, v in modem.items())
            logging.info(f"  {name}: {modem_str}")
        logging.info("=" * 60)
        emit_event({"type": "modem", "iteration": iteration_num, "ports": modem_all})

        passed = True
        if cfg['ver']:
            passed = verify_data(sent_frames, verifiers, verify_pairs, iteration_num)
        elif any(v > 0 for v in dropped.values()):
            logging.error("Dropped bytes detected (frame verify disabled).")
            passed = False

        # Receiving MORE than we sent means duplication or injected line noise.
        # That's a failure in any mode: when verify is on, noise inserted between
        # frames is skipped as resync and won't trip the frame checks, so this
        # byte-count surplus is the signal that catches it.
        if any(v > 0 for v in extra.values()):
            logging.error("Extra bytes detected (received more than sent).")
            passed = False

        return passed

    except Exception as e:
        logging.error(f"Error in run_iteration: {e}")
        return False
    finally:
        # Central close — only after every thread has stopped, so a port's
        # receive thread is never reading a handle its send thread just closed.
        for p in port_objs.values():
            try:
                if p.is_open:
                    p.close()
            except Exception:
                pass
        sent_frames.clear()
        sent_elapsed.clear()
        recv_elapsed.clear()
        verifiers.clear()

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description='Digi RealPort Serial Stress Tester (Linux rptest equivalent)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    p.add_argument('-dut', default='e8', metavar='ID',
                   help='DUT device ID, e.g. e8 → /dev/ttye8<nn>')
    p.add_argument('-aux', default='TS', metavar='ID',
                   help='AUX device ID, e.g. TS → /dev/ttyTS<nn>')
    p.add_argument('-bxp', default='', metavar='PORTS',
                   help='Bidirectional port indices, e.g. 0-3 or 0,1,2,3')
    p.add_argument('-rxp', default='', metavar='[ID:]PORTS',
                   help='Receive side. Optional device id chooses which family '
                        'receives, e.g. TS:0-3 (default: -aux family)')
    p.add_argument('-txp', default='', metavar='[ID:]PORTS',
                   help='Transmit side. Optional device id chooses which family '
                        'transmits, e.g. e8:0-3 (default: -dut family). Pairs '
                        'with -rxp by position.')
    p.add_argument('-tbp', default=None, metavar='HEX',
                   help='Test buffer pattern hex, e.g. 0xAA')
    p.add_argument('-ctx', default=None, metavar='FILE',
                   help='Read raw test pattern from binary file')
    p.add_argument('-rep', type=int, default=1, metavar='N',
                   help='Repetitions (-1=infinite)')
    p.add_argument('-slp', type=int, default=0, metavar='MS',
                   help='Sleep between reps (ms)')
    p.add_argument('-clo', type=int, default=1, choices=[0, 1],
                   help='Close ports between reps (1=yes 0=no)')
    p.add_argument('-bps', type=int, default=9600, metavar='BAUD',
                   help='Baud rate (default 9600)')
    p.add_argument('-dbs', type=int, default=8, choices=[5, 6, 7, 8],
                   help='Data bits (default 8)')
    p.add_argument('-par', type=int, default=0, choices=[0, 1, 2, 3, 4],
                   help='Parity 0=None 1=Odd 2=Even 3=Mark 4=Space')
    p.add_argument('-sbs', type=int, default=0, choices=[0, 1, 2],
                   help='Stop bits 0=1 1=1.5 2=2')
    p.add_argument('-rts', type=int, default=0, choices=[0, 1, 2, 3],
                   help='RTS 0=off 1=on 2=handshake 3=toggle')
    p.add_argument('-cts', type=int, default=0,
                   help='CTS -1=off 0=hsIfRtsToggle 1=handshake')
    p.add_argument('-dtr', type=int, default=0, choices=[0, 1, 2],
                   help='DTR 0=off 1=on 2=handshake')
    p.add_argument('-xon', type=int, default=0, choices=[0, 1],
                   help='Xon/Xoff 0=off 1=on')
    p.add_argument('-qui', type=int, default=4096, metavar='N',
                   help='Input queue size bytes')
    p.add_argument('-quo', type=int, default=4096, metavar='N',
                   help='Output queue size bytes')
    p.add_argument('-rtc', type=int, default=15600, metavar='MS',
                   help='ReadTotalTimeoutConstant ms')
    p.add_argument('-wtc', type=int, default=15600, metavar='MS',
                   help='WriteTotalTimeoutConstant ms')
    p.add_argument('-bss', type=int, default=1152, metavar='N',
                   help='On-wire frame size bytes (default 1152); usable payload '
                        'is this minus 14 B of frame overhead')
    p.add_argument('-nob', type=int, default=-1, metavar='N',
                   help='Max buffers per port (-1=infinite)')
    p.add_argument('-ttr', type=int, default=-1, metavar='MS',
                   help='Time to run ms (-1=forever)')
    p.add_argument('-res', type=int, default=0, choices=[0, 1],
                   help='Retry send on timeout 0=no 1=yes')
    p.add_argument('-btw', type=int, default=0, metavar='MS',
                   help='Sleep between I/O ops ms')
    p.add_argument('-ver', type=int, default=1, choices=[0, 1],
                   help='Verify received data 0=no 1=yes')
    p.add_argument('-flb', type=int, default=0, choices=[0, 1],
                   help='Flush port when done 0=no 1=yes')
    p.add_argument('-dex', type=int, default=0, choices=[0, 1],
                   help='Verbose/debug output 0=no 1=yes')
    p.add_argument('--logfile', default=CURRENT_LOG, metavar='PATH',
                   help=f'Log file path (default: {CURRENT_LOG})')
    p.add_argument('--json', action='store_true',
                   help='Also emit structured JSON-lines events on stdout '
                        '(used by the GUI; immune to log-wording changes)')
    return p.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    global JSON_OUT
    JSON_OUT = bool(args.json)

    if not args.bxp and not args.rxp and not args.txp:
        print("ERROR: Specify at least one of -bxp, -rxp, -txp")
        print("Example: python3 rptest_run.py -bxp 0-3 -bps 115200 -ttr 60000")
        sys.exit(1)

    valid_baudrates = [50, 75, 110, 300, 600, 1200, 9600, 14400, 19200,
                       28800, 57600, 115200, 230400]
    if args.bps not in valid_baudrates:
        sys.exit(f"ERROR: Invalid baud rate {args.bps}. "
                 f"Valid: {', '.join(map(str, valid_baudrates))}")

    if args.bss <= FRAME_OVERHEAD:
        sys.exit(f"ERROR: -bss must be greater than {FRAME_OVERHEAD} "
                 f"(frame overhead). Got {args.bss}.")
    payload_len = args.bss - FRAME_OVERHEAD
    if payload_len > 0xFFFF:
        sys.exit(f"ERROR: -bss too large; payload {payload_len} B exceeds the "
                 f"65535-byte frame limit. Use -bss <= {0xFFFF + FRAME_OVERHEAD}.")

    bx_indices = parse_port_range(args.bxp) if args.bxp else []
    tx_dev, tx_indices = parse_device_ports(args.txp, args.dut)
    rx_dev, rx_indices = parse_device_ports(args.rxp, args.aux)

    # -txp and -rxp work as a pair: the TX side feeds the RX side, one direction.
    if bool(tx_indices) ^ bool(rx_indices):
        logging.warning("-txp and -rxp are used together (TX side -> RX side). "
                        "Specifying one without the other transmits/listens with "
                        "nothing on the far end and verifies nothing.")
    if tx_indices and rx_indices and len(tx_indices) != len(rx_indices):
        logging.warning(f"-txp has {len(tx_indices)} port(s) but -rxp has "
                        f"{len(rx_indices)}; only the first "
                        f"{min(len(tx_indices), len(rx_indices))} pair up.")

    cfg = dict(
        bps=args.bps, dbs=args.dbs, par=args.par, sbs=args.sbs,
        rts=args.rts, cts=args.cts, dtr=args.dtr, xon=args.xon,
        qui=args.qui, quo=args.quo, rtc=args.rtc, wtc=args.wtc,
        bss=args.bss, nob=args.nob, ttr=args.ttr,
        payload_len=payload_len,
        res=bool(args.res), btw=args.btw, ver=bool(args.ver),
        flb=bool(args.flb), verbose=bool(args.dex),
        dut=args.dut,
        aux=args.aux,
    )

    if args.dex:
        logging.getLogger('').setLevel(logging.DEBUG)
        console.setLevel(logging.DEBUG)

    pattern = build_pattern(tbp_hex=args.tbp, ctx_file=args.ctx, buf_size=args.bss)

    logging.info("=" * 60)
    logging.info("DIGI REALPORT SERIAL STRESS TEST")
    logging.info("=" * 60)
    logging.info(f"  BX ports : {bx_indices}  (dut={cfg['dut']} <-> aux={cfg['aux']}, both directions)")
    logging.info(f"  TX ports : {tx_indices} (device {tx_dev})  ->  RX ports : {rx_indices} (device {rx_dev})")
    logging.info(f"  Baud: {cfg['bps']}  DataBits: {cfg['dbs']}  Parity: {cfg['par']}  StopBits: {cfg['sbs']}")
    logging.info(f"  RTS: {cfg['rts']}  CTS: {cfg['cts']}  XonXoff: {cfg['xon']}")
    logging.info(f"  FrameSize: {cfg['bss']}B (payload {payload_len}B)  MaxBufs: {cfg['nob']}  TTR: {cfg['ttr']}ms")
    logging.info(f"  Reps: {args.rep}  SleepBetween: {args.slp}ms  Verify: {cfg['ver']}")
    logging.info("=" * 60)

    emit_event({
        "type": "run_start",
        "bx": bx_indices, "tx": tx_indices, "rx": rx_indices,
        "tx_dev": tx_dev, "rx_dev": rx_dev,
        "bps": cfg['bps'], "bss": cfg['bss'], "ttr": cfg['ttr'],
        "rep": args.rep, "ver": cfg['ver'],
    })

    iteration  = 0
    total_pass = 0
    total_fail = 0

    try:
        while True:
            iteration += 1
            stop_event.clear()
            logging.info(f"\n>>> Iteration {iteration}"
                         + (f" of {args.rep}" if args.rep > 0 else " (continuous)"))

            passed = run_iteration(tx_dev, tx_indices, rx_dev, rx_indices,
                                   bx_indices, cfg, pattern, iteration)

            if passed:
                total_pass += 1
                logging.info(f"Iteration {iteration}: PASS  ({total_pass} pass / {total_fail} fail)")
                emit_event({"type": "iteration_result", "iteration": iteration,
                            "result": "PASS", "pass": total_pass, "fail": total_fail})
            else:
                total_fail += 1
                logging.error(f"Iteration {iteration}: FAIL  ({total_pass} pass / {total_fail} fail)")
                emit_event({"type": "iteration_result", "iteration": iteration,
                            "result": "FAIL", "pass": total_pass, "fail": total_fail})
                logging.error("Stopping due to failure.")
                break

            if args.rep >= 0 and iteration >= args.rep:
                logging.info(f"Completed {args.rep} iteration(s).")
                break

            if args.slp > 0:
                time.sleep(args.slp / 1000.0)

            stop_event.clear()

    except KeyboardInterrupt:
        logging.info("\nKeyboardInterrupt - stopping.")
        stop_event.set()

    logging.info("=" * 60)
    logging.info("TEST COMPLETE")
    logging.info(f"  Iterations: {iteration}  Passed: {total_pass}  Failed: {total_fail}")
    logging.info(f"  Result: {'PASS' if total_fail == 0 else 'FAIL'}")
    logging.info("=" * 60)
    emit_event({"type": "run_complete",
                "result": "PASS" if total_fail == 0 else "FAIL",
                "iterations": iteration, "passed": total_pass, "failed": total_fail})
    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
