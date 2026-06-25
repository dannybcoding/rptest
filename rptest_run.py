"""
serialstress_run.py  -  Linux RealPort Serial Stress Tester
Equivalent to the Windows rptest.exe utility for Digi RealPort devices.

Usage examples (mirroring rptest syntax):
  # Basic bidirectional test on 4 port pairs, 60 sec, 115200 baud
  python3 serialstress_run.py -bxp 0-3 -bps 115200 -ttr 60000

  # TX-only on ports 0-1, RX-only on ports 2-3, 9600 baud
  python3 serialstress_run.py -txp 0-1 -rxp 2-3 -bps 9600 -ttr 30000

  # Run 10 iterations, sleep 2s between, verify data, detailed log
  python3 serialstress_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep 10 -slp 2000 -ver 1 -dex 1

  # Run until failure, hardware flow control (RTS/CTS handshake)
  python3 serialstress_run.py -bxp 0-3 -bps 115200 -ttr 60000 -rep -1 -rts 2 -cts 1

Port numbering maps to:
  DUT ports : /dev/tty<ID><Portnumber>  (e.g. port 0 = /dev/ttye800)
  AUX ports : /dev/tty<ID><Portnumber>   (e.g. port 0 = /dev/ttyTS00)
"""

import serial
import time
import threading
import logging
import sys
import argparse
import os
import re
from logging.handlers import RotatingFileHandler

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

def port_index_to_names(index, dut_id, aux_id):
    return (f"/dev/tty{dut_id}{index:02d}", f"/dev/tty{aux_id}{index:02d}")

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
# I/O worker threads
# ---------------------------------------------------------------------------

def send_data(dut_port, cfg, sent_count, sent_data, pattern):
    port_name   = dut_port.port
    buf_size    = cfg['bss']
    btw_sec     = cfg['btw'] / 1000.0
    ttr_sec     = cfg['ttr'] / 1000.0 if cfg['ttr'] >= 0 else None
    nob         = cfg['nob']

    try:
        time.sleep(0.5)
        start_time   = time.time()
        buffers_sent = 0

        while not stop_event.is_set():
            if ttr_sec is not None and (time.time() - start_time) >= ttr_sec:
                break
            if nob >= 0 and buffers_sent >= nob:
                break

            chunk = pattern[:buf_size]
            try:
                dut_port.write(chunk)
            except serial.SerialTimeoutException:
                logging.warning(f"[{port_name}] Write timeout")
                if cfg['res']:
                    continue
                break

            sent_count[port_name] += len(chunk)
            if cfg['ver']:
                sent_data[port_name].extend(chunk)
            buffers_sent += 1

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
        # ALWAYS drain the TX buffer before closing. write() only queues bytes;
        # closing without flushing can discard whatever is still in the OS /
        # RealPort write buffer, silently dropping the tail of the transfer.
        try:
            dut_port.flush()          # blocks until all written data is transmitted
        except Exception:
            pass
        if cfg['flb']:
            try:
                dut_port.reset_output_buffer()
            except Exception:
                pass
        try:
            dut_port.close()
        except Exception:
            pass


def receive_data(aux_port, cfg, received_count, received_data, senders_done):
    """
    Drain the RX port until the senders are finished AND no new data has
    arrived for `quiet_grace` seconds. Do NOT stop merely because the clock
    passed `ttr`: on network-attached serial (RealPort) the tail of a transfer
    arrives in bursts with small gaps, so a clock-based cutoff truncates data.
    """
    port_name   = aux_port.port
    ttr_sec     = cfg['ttr'] / 1000.0 if cfg['ttr'] >= 0 else None
    rtc_sec     = cfg['rtc'] / 1000.0
    quiet_grace = max(rtc_sec, 2.0)          # how long to wait after the last byte
    # Absolute safety cap so a stuck link can't drain forever.
    hard_cap    = (ttr_sec + 60) if ttr_sec is not None else None

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

            aux_port.timeout = min(rtc_sec, 1.0)
            n = aux_port.in_waiting
            chunk = aux_port.read(n if n > 0 else 1)

            if chunk:
                received_count[port_name] += len(chunk)
                if cfg['ver']:
                    received_data[port_name].extend(chunk)
                last_data_time = time.time()

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
        try:
            aux_port.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data verification
# ---------------------------------------------------------------------------

def verify_data(sent_data, received_data, port_mapping):
    all_ok = True
    logging.info("=" * 60)
    logging.info("DATA VERIFICATION")
    logging.info("=" * 60)
    for dut_port, aux_port in port_mapping.items():
        sent     = bytes(sent_data.get(dut_port, []))
        received = bytes(received_data.get(aux_port, []))
        if sent == received:
            logging.info(f"  PASS  {dut_port} -> {aux_port}  ({len(sent)} bytes match)")
        else:
            all_ok = False
            mismatch_idx = next(
                (i for i, (s, r) in enumerate(zip(sent, received)) if s != r),
                min(len(sent), len(received))
            )
            logging.info(f"  FAIL  {dut_port} -> {aux_port}  "
                         f"sent={len(sent)}B received={len(received)}B "
                         f"first mismatch @ byte {mismatch_idx}")
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
# Single iteration
# ---------------------------------------------------------------------------

def run_iteration(tx_indices, rx_indices, bx_indices, cfg, pattern, iteration_num):
    port_mapping   = {}
    bx_port_names  = [port_index_to_names(i, cfg['dut'], cfg['aux']) for i in bx_indices]
    tx_port_names  = [port_index_to_names(i, cfg['dut'], cfg['aux']) for i in tx_indices]
    rx_port_names  = [port_index_to_names(i, cfg['dut'], cfg['aux']) for i in rx_indices]

    for dut, aux in bx_port_names:
        port_mapping[dut] = aux
    for (dut, _), (_, aux) in zip(tx_port_names, rx_port_names):
        port_mapping[dut] = aux

    threads        = []
    tx_threads     = []
    rx_threads     = []
    sent_count     = {}
    received_count = {}
    sent_data      = {}
    received_data  = {}
    open_ports     = []
    senders_done   = threading.Event()

    logging.info(f"--- Iteration {iteration_num} start ---")

    try:
        for dut_name in list(port_mapping.keys()):
            try:
                port = open_port(dut_name, cfg)
                open_ports.append(port)
                sent_count[dut_name] = 0
                sent_data[dut_name]  = []
                t = threading.Thread(
                    target=send_data,
                    args=(port, cfg, sent_count, sent_data, pattern),
                    name=f"TX-{dut_name}", daemon=True)
                threads.append(t)
                tx_threads.append(t)
                logging.debug(f"Opened TX port: {dut_name}")
            except Exception as e:
                logging.error(f"Cannot open TX port {dut_name}: {e}")
                stop_event.set()
                return False

        for aux_name in list(port_mapping.values()):
            try:
                port = open_port(aux_name, cfg)
                open_ports.append(port)
                received_count[aux_name] = 0
                received_data[aux_name]  = []
                t = threading.Thread(
                    target=receive_data,
                    args=(port, cfg, received_count, received_data, senders_done),
                    name=f"RX-{aux_name}", daemon=True)
                threads.append(t)
                rx_threads.append(t)
                logging.debug(f"Opened RX port: {aux_name}")
            except Exception as e:
                logging.error(f"Cannot open RX port {aux_name}: {e}")
                stop_event.set()
                return False

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

        # Results
        logging.info("=" * 60)
        logging.info(f"ITERATION {iteration_num} RESULTS")
        logging.info("=" * 60)
        dropped = {}
        ttr_s = max(cfg['ttr'] / 1000.0, 1) if cfg['ttr'] >= 0 else 1
        for dut_name, aux_name in port_mapping.items():
            s = sent_count.get(dut_name, 0)
            r = received_count.get(aux_name, 0)
            d = max(0, s - r)
            dropped[aux_name] = d
            logging.info(f"  {dut_name} -> {aux_name}")
            logging.info(f"    Sent:     {s:>10} bytes  ({s/ttr_s:>8.0f} B/s)")
            logging.info(f"    Received: {r:>10} bytes  ({r/ttr_s:>8.0f} B/s)")
            logging.info(f"    Dropped:  {d:>10} bytes")
        total_sent     = sum(sent_count.values())
        total_received = sum(received_count.values())
        logging.info(f"  TOTALS  sent={total_sent}  received={total_received}  "
                     f"dropped={sum(dropped.values())}")
        logging.info("=" * 60)

        passed = True
        if cfg['ver']:
            passed = verify_data(sent_data, received_data, port_mapping)
        if any(v > 0 for v in dropped.values()):
            logging.error("Dropped bytes detected.")
            passed = False

        return passed

    except Exception as e:
        logging.error(f"Error in run_iteration: {e}")
        return False
    finally:
        for p in open_ports:
            try:
                if p.is_open:
                    p.close()
            except Exception:
                pass
        sent_data.clear()
        received_data.clear()

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
    p.add_argument('-rxp', default='', metavar='PORTS',
                   help='Receive-only port indices')
    p.add_argument('-txp', default='', metavar='PORTS',
                   help='Transmit-only port indices')
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
                   help='Buffer/chunk size bytes (default 1152)')
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
    return p.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if not args.bxp and not args.rxp and not args.txp:
        print("ERROR: Specify at least one of -bxp, -rxp, -txp")
        print("Example: python3 serialstress_run.py -bxp 0-3 -bps 115200 -ttr 60000")
        sys.exit(1)

    valid_baudrates = [50, 75, 110, 300, 600, 1200, 9600, 14400, 19200,
                       28800, 57600, 115200, 230400]
    if args.bps not in valid_baudrates:
        sys.exit(f"ERROR: Invalid baud rate {args.bps}. "
                 f"Valid: {', '.join(map(str, valid_baudrates))}")

    bx_indices = parse_port_range(args.bxp) if args.bxp else []
    tx_indices = parse_port_range(args.txp) if args.txp else []
    rx_indices = parse_port_range(args.rxp) if args.rxp else []

    cfg = dict(
        bps=args.bps, dbs=args.dbs, par=args.par, sbs=args.sbs,
        rts=args.rts, cts=args.cts, dtr=args.dtr, xon=args.xon,
        qui=args.qui, quo=args.quo, rtc=args.rtc, wtc=args.wtc,
        bss=args.bss, nob=args.nob, ttr=args.ttr,
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
    logging.info(f"  BX ports : {bx_indices}  TX ports: {tx_indices}  RX ports: {rx_indices}")
    logging.info(f"  Baud: {cfg['bps']}  DataBits: {cfg['dbs']}  Parity: {cfg['par']}  StopBits: {cfg['sbs']}")
    logging.info(f"  RTS: {cfg['rts']}  CTS: {cfg['cts']}  XonXoff: {cfg['xon']}")
    logging.info(f"  BufSize: {cfg['bss']}B  MaxBufs: {cfg['nob']}  TTR: {cfg['ttr']}ms")
    logging.info(f"  Reps: {args.rep}  SleepBetween: {args.slp}ms  Verify: {cfg['ver']}")
    logging.info("=" * 60)

    iteration  = 0
    total_pass = 0
    total_fail = 0

    try:
        while True:
            iteration += 1
            stop_event.clear()
            logging.info(f"\n>>> Iteration {iteration}"
                         + (f" of {args.rep}" if args.rep > 0 else " (continuous)"))

            passed = run_iteration(tx_indices, rx_indices, bx_indices, cfg, pattern, iteration)

            if passed:
                total_pass += 1
                logging.info(f"Iteration {iteration}: PASS  ({total_pass} pass / {total_fail} fail)")
            else:
                total_fail += 1
                logging.error(f"Iteration {iteration}: FAIL  ({total_pass} pass / {total_fail} fail)")
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
    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
