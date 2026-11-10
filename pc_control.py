"""Local dual-RS485 controller. Only device workers perform blocking serial I/O."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import queue
import threading
import time
import uuid

DEFAULTS = dict(
    t1=1320, t2=1560, q0=120000, q1=60000, q2=30000, qmin=10000, qmax=190000,
    k=0, rpm_min=10, rpm_max=35000, hyst=20, debounce=500, ramp=50000, sample=200,
    stale=2000, vmin=500, vmax=1800, full_mv=10000, raw_full=65535,
    pump=1, adc=1, baud=115200, adc_baud=9600, direction=0,
    pump_port="", adc_port="",
    speed_mode=0, r0=12000, r1=6000, r2=3000,
    # dual_pump is retained only for loading older configuration files.
    thresholds=[], speeds=[], dual_pump=0,
    pump2=1, pump2_baud=115200, pump2_port="", direction2=0,
)
CHANNEL_FIELDS = dict(pump=("pump_port", "pump", "baud"),
                      pump2=("pump2_port", "pump2", "pump2_baud"),
                      adc=("adc_port", "adc", "adc_baud"))
BAUDS = (4800, 9600, 14400, 19200, 38400, 56000, 57600, 115200)
COMM_FIELDS = ("pump_port", "adc_port", "pump", "adc", "baud", "adc_baud")
ACTIVE = ("STARTING", "RUN", "MANUAL", "HOLD", "COMMISSION")


class ControlError(ValueError):
    pass


def validate_config(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise ControlError("CONFIG_FIELDS")
    c = deepcopy(config)
    for key, value in c.items():
        if key in ("thresholds", "speeds"):
            if not isinstance(value, list) or any(type(x) is not int for x in value):
                raise ControlError("RULE_LIST")
        elif key.endswith("_port"):
            if not isinstance(value, str) or len(value) > 200 or any(ord(x) < 32 for x in value):
                raise ControlError("PORT_NAME")
        elif type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ControlError("INTEGER_REQUIRED")
    if any(not 1 <= c[key] <= 247 for key in ("pump", "pump2", "adc")):
        raise ControlError("ADDRESS")
    if (any(c[key] not in BAUDS for key in ("baud", "pump2_baud", "adc_baud"))
            or c["direction"] > 1 or c["direction2"] > 1 or c["dual_pump"] not in (0, 1)):
        raise ControlError("BUS")
    if c["speed_mode"] not in (0, 1) or any(
            not c["rpm_min"] <= c[key] <= c["rpm_max"] for key in ("r0", "r1", "r2")):
        raise ControlError("RPM_RANGE")
    if not 1 <= c["raw_full"] <= 65535 or not 1 <= c["full_mv"] <= 100000:
        raise ControlError("ADC_SCALE")
    thresholds = rule_thresholds(c)
    if (not 1 <= len(thresholds) <= 32 or not 0 <= c["vmin"] < c["vmax"] <= c["full_mv"]
            or not c["vmin"] < thresholds[0] or not thresholds[-1] < c["vmax"]
            or any(a >= b for a, b in zip(thresholds, thresholds[1:]))):
        raise ControlError("THRESHOLD")
    if not 0 < c["hyst"] < min(b - a for a, b in zip([c["vmin"]] + thresholds, thresholds)):
        raise ControlError("HYSTERESIS")
    if c["thresholds"]:
        if c["speed_mode"] != 1 or len(c["speeds"]) != len(thresholds) + 1:
            raise ControlError("RULE_LIST")
        if any(not c["rpm_min"] <= rpm <= c["rpm_max"] for rpm in c["speeds"]):
            raise ControlError("RPM_RANGE")
    elif c["speeds"]:
        raise ControlError("RULE_LIST")
    if not (100 <= c["sample"] <= 2000 and 3 * c["sample"] <= c["stale"] <= 10000
            and c["debounce"] <= 10000):
        raise ControlError("TIMING")
    if not 1 <= c["ramp"] <= 1000000:
        raise ControlError("RAMP")
    if not 10000 <= c["qmin"] <= c["qmax"] <= 190000:
        raise ControlError("FLOW_RANGE")
    if not 1 <= c["rpm_min"] <= c["rpm_max"] <= 35000 or c["k"] > 1000000:
        raise ControlError("RPM_RANGE")
    for key in ("q0", "q1", "q2"):
        if not c["qmin"] <= c[key] <= c["qmax"]:
            raise ControlError("STAGE_FLOW")
        if not c["speed_mode"] and c["k"] and not c["rpm_min"] <= flow_to_rpm(c, c[key]) <= c["rpm_max"]:
            raise ControlError("CALIBRATION_RPM")
    return c


def rule_thresholds(config):
    return list(config["thresholds"] or (config["t1"], config["t2"]))


def flow_to_rpm(config, flow):
    if config["speed_mode"]:
        return int(flow + 0.5)
    return int(flow * 100 / config["k"] + 0.5) if config["k"] else 0


class ModbusDevice:
    def __init__(self, port, address, baud):
        import minimalmodbus
        self.instrument = minimalmodbus.Instrument(port, address, mode=minimalmodbus.MODE_RTU)
        self.instrument.serial.baudrate = baud
        self.instrument.serial.timeout = 0.2
        self.instrument.serial.write_timeout = 0.2
        self.instrument.clear_buffers_before_each_transaction = True
        self.instrument.close_port_after_each_call = False

    def call(self, operation, value, config):
        import minimalmodbus
        i = self.instrument
        adc = operation in ("adc", "commission")
        i.address = config["adc" if adc else "pump"]
        i.serial.baudrate = config["adc_baud" if adc else "baud"]
        if operation == "commission":
            i.address, i.serial.baudrate = value["old_address"], value["old_baud"]
        attempts = 2 if operation in ("adc", "status", "stop") else 1
        for attempt in range(attempts):
            try:
                if operation == "adc":
                    return i.read_register(0, 0, functioncode=4)
                if operation == "status":
                    return i.read_register(0x3003, 0, functioncode=4)
                if operation == "mode":
                    i.write_register(0x4007, 2, functioncode=16)
                elif operation == "direction":
                    i.write_bit(2, config["direction"], functioncode=5)
                elif operation in ("initial_speed", "speed"):
                    i.write_long(0x4001, value, signed=False, byteorder=minimalmodbus.BYTEORDER_BIG)
                elif operation in ("run", "stop"):
                    i.write_bit(1, int(operation == "run"), functioncode=5)
                elif operation == "commission":
                    i.write_register(value["register"], value["value"], functioncode=6)
                else:
                    raise ControlError("UNKNOWN_OPERATION")
                return value
            except (OSError, minimalmodbus.ModbusException):
                if attempt + 1 == attempts:
                    raise
        raise RuntimeError("Unreachable retry state")

    def close(self):
        self.instrument.serial.close()


class SimPlant:
    """Explicit simulation mode; it never opens a physical serial port."""
    def __init__(self):
        self.lock = threading.Lock()
        self.voltage_mv = 1200
        self.adc_online = self.pump_online = True
        self.status = self.rpm = 0
        self.writes = []
        self.delay = 0.015
        self.second = None

    def update(self, values):
        allowed = {"voltage_mv", "adc_online", "pump_online", "leak", "stall", "external_run",
                   "pump2_online", "pump2_leak", "pump2_stall", "pump2_external_run"}
        if not isinstance(values, dict) or not set(values) <= allowed:
            raise ControlError("SIM_FIELDS")
        for key, value in values.items():
            if key == "voltage_mv":
                if type(value) is not int or not 0 <= value <= 10000:
                    raise ControlError("SIM_VOLTAGE")
            elif type(value) is not bool:
                raise ControlError("SIM_BOOLEAN")
        with self.lock:
            for key, value in values.items():
                if key.startswith("pump2_"):
                    if self.second is None:
                        self.second = SimPlant()
                    self.second.update({"pump_" + key[6:] if key == "pump2_online" else key[6:]: value})
                    continue
                if key in ("voltage_mv", "adc_online", "pump_online"):
                    setattr(self, key, value)
                else:
                    bit = {"leak": 4, "stall": 8, "external_run": 1}[key]
                    self.status = self.status | bit if value else self.status & ~bit

    def factory(self, port, address, baud):
        if port == "SIM-PUMP2":
            if self.second is None:
                self.second = SimPlant()
            self.second.delay = self.delay
            return SimDevice(self.second, "SIM-PUMP")
        if port not in ("SIM-PUMP", "SIM-BZY"):
            raise ControlError("SIM_PORT_ONLY")
        return SimDevice(self, port)


class SimDevice:
    def __init__(self, plant, port):
        self.plant, self.port = plant, port

    def call(self, operation, value, config):
        p = self.plant
        if p.delay:
            time.sleep(p.delay)
        with p.lock:
            adc = operation in ("adc", "commission")
            if adc != (self.port == "SIM-BZY"):
                raise ControlError("WRONG_DEVICE_PORT")
            if not (p.adc_online if adc else p.pump_online):
                raise OSError("Simulated device disconnected")
            if operation == "adc":
                return round(p.voltage_mv * config["raw_full"] / config["full_mv"])
            if operation == "status":
                return p.status
            p.writes.append((operation, value))
            if operation in ("speed", "initial_speed"):
                p.rpm = value
            elif operation == "run":
                p.status |= 1
            elif operation == "stop":
                p.status &= ~1
            elif operation == "direction":
                p.status = (p.status & ~2) | (config["direction"] << 1)
            return value

    def close(self):
        pass


class CsvRecorder:
    FIELDS = ("pc_time", "elapsed_ms", "state", "fault", "raw_adc", "voltage_mv",
              "filtered_mv", "stage", "target_ul_min", "command_ul_min", "rpm_x100",
              "estimated_ml_min", "pump_valid", "pump_status", "adc_errors", "pump_errors",
              "pump1_valid", "pump1_status", "pump1_rpm_x100", "pump2_valid", "pump2_status", "pump2_rpm_x100")

    def __init__(self, folder, simulation):
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = folder / (("SIM-" if simulation else "") + stamp + "-" + uuid.uuid4().hex[:6] + ".csv")
        self.file = self.path.open("w", encoding="utf-8-sig", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=self.FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.queue = queue.Queue(maxsize=256)
        self.lock = threading.Lock()
        self.error = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="csv-writer", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while True:
                row = self.queue.get()
                try:
                    if row is None:
                        return
                    with self.lock:
                        self.writer.writerow(row)
                        self.file.flush()
                finally:
                    self.queue.task_done()
        except OSError as exc:
            self.error = str(exc)
        finally:
            self.file.close()

    def record(self, t, config):
        if self.error or self.closed:
            return
        row = dict(zip(self.FIELDS, (
            datetime.now().astimezone().isoformat(timespec="milliseconds"),
            t["ms"], t["state"], t["fault"], t["raw"],
            t["mv"] if t["voltage_valid"] else "",
            t["filtered_mv"] if t["voltage_valid"] else "", t["stage"] + 1,
            t["target_ul_min"] if not config["speed_mode"] else "", t["command_ul_min"] if not config["speed_mode"] else "",
            t["rpm_x100"] if t["pump_valid"] and t["rpm_known"] else "",
            t["rpm_x100"] * config["k"] / 100000
            if t["pump_valid"] and t["rpm_known"] and config["k"] and not config["speed_mode"] else "",
            t["pump_valid"], t["status"], t["adc_errors"], t["pump_errors"],
            *[value for key in ("pump", "pump2") for value in (
                t["pumps"][key]["valid"], t["pumps"][key]["status"],
                t["pumps"][key]["rpm_x100"] if t["pumps"][key]["rpm_known"] else "")],
        )))
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.error = "CSV_QUEUE_FULL"

    def download(self):
        with self.lock:
            return self.path.read_bytes()

    def close(self):
        if self.closed:
            return
        self.closed = True
        # A recorder that already failed must not block device shutdown.
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=0.1)
                break
            except queue.Full:
                continue
        self.thread.join(timeout=2)


class Controller:
    def __init__(self, folder, simulation=False, factory=None, clock=time.monotonic,
                 executor=None, autostart=True):
        self.folder = Path(folder)
        self.simulation = simulation
        self.plant = SimPlant() if simulation else None
        self.factory = factory or (self.plant.factory if simulation else ModbusDevice)
        self.clock = clock
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="rs485")
        self.pump_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pump")
        self.lock = threading.RLock()
        self.lifecycle = threading.Lock()
        self.shutdown_event = threading.Event()
        self.server_id = uuid.uuid4().hex
        self.events = deque(maxlen=100)
        self.event_seq = self.seq = 0
        self.history = deque(maxlen=1200)
        self.config = deepcopy(DEFAULTS)
        self.saved = False
        self.state, self.fault = "DISCONNECTED", "NONE"
        self.connected = False
        self.devices = {}
        self.connecting = set()
        self.pending = {}
        self.pumps = {key: dict(valid=False, last_status=None, status=0, rpm=None, error="")
                      for key in ("pump", "pump2")}
        self.epoch = 0
        self.recorder = None
        self._zero(self.clock())
        self._load()
        self.thread = None
        if autostart:
            self.thread = threading.Thread(target=self._loop, name="control", daemon=True)
            self.thread.start()

    def _zero(self, now):
        self.started = now
        self.last_tick = now
        self.last_sample = self.last_status = None
        self.adc_valid = self.pump_valid = False
        self.raw = 0
        self.mv = self.filtered = 0.0
        self.stage, self.candidate = 0, None
        self.candidate_since = self.ramp_at = now
        self.flow = self.target = 0.0
        self.applied_rpm = None
        self.status = 0
        self.adc_errors = self.pump_errors = 0
        self.stop_required = False
        self.start_step = 0
        self.manual = False
        self.manual_rpm = 0
        self.commission = None
        self.next_adc = self.next_status = self.next_speed = self.next_stop = now
        self.next_record = now

    def _event(self, name, detail="", level="info"):
        self.event_seq += 1
        self.events.append(dict(id=self.event_seq, time=datetime.now().astimezone().isoformat(),
                                event=name, detail=str(detail)[:500], level=level))

    def _load(self):
        path = self.folder / "config.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") not in ("pump-pc-v1", "pump-pc-v2"):
                raise ControlError("CONFIG_VERSION")
            self.config = validate_config(dict(DEFAULTS, **data["config"]))
            self.saved = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._event("CONFIG_LOAD_FAILED", exc, "error")

    def _voltage_fresh(self, now):
        return (self.adc_valid and self.last_sample is not None
                and now - self.last_sample < self.config["stale"] / 1000)

    def _pump_fresh(self, now):
        return bool(self._pump_keys()) and self.pump_valid and all(
            self._individual_fresh(key, now) for key in self._pump_keys())

    def _pump_keys(self):
        return tuple(key for key in ("pump", "pump2") if key in self.devices)

    def _individual_fresh(self, key, now):
        p = self.pumps[key]
        return key in self.devices and p["valid"] and p["last_status"] is not None and now - p["last_status"] < 1.5

    def _require_idle(self):
        if self.state not in ("IDLE", "DISCONNECTED") or self.stop_required or self.connecting:
            raise ControlError("STOP_AND_WAIT")

    def _require_stopped(self, now):
        if self.connecting or self.state != "IDLE" or not self._pump_fresh(now) or self.status & 13:
            raise ControlError("STOP_AND_WAIT")

    def connect(self):
        if self.connected:
            raise ControlError("ALREADY_CONNECTED")
        if not self.config["pump_port"] or not self.config["adc_port"]:
            raise ControlError("SELECT_PORTS")
        if self.config["pump_port"].casefold() == self.config["adc_port"].casefold():
            raise ControlError("SAME_PORT")
        self.connect_channel("pump")
        if self.config["pump2_port"]:
            self.connect_channel("pump2")
        try:
            self.connect_channel("adc")
        except Exception:
            # Preserve an opened pump connection for status and stop operations.
            raise

    def connect_channel(self, channel, settings=None):
        if channel not in CHANNEL_FIELDS:
            raise ControlError("CHANNEL")
        with self.lifecycle:
            with self.lock:
                if channel in self.devices or channel in self.connecting:
                    raise ControlError("ALREADY_CONNECTED")
                c = dict(self.config)
                keys = CHANNEL_FIELDS[channel]
                if channel != "adc":
                    if not (self.state in ("IDLE", "DISCONNECTED", "FAULT")
                            or (self.state == "HOLD" and self.fault == "EXTERNAL_RUN")):
                        raise ControlError("STOP_AND_WAIT")
                if settings is not None:
                    if not isinstance(settings, dict) or set(settings) != set(keys):
                        raise ControlError("CONFIG_FIELDS")
                    c.update(settings)
                    c = validate_config(c)
                port = c[channel + "_port"]
                if not port:
                    raise ControlError("SELECT_PORTS")
                if any(port.casefold() == c[other + "_port"].casefold()
                       for other in self.devices.keys() | self.connecting if other != channel):
                    raise ControlError("SAME_PORT")
                if self.simulation and port != {"pump": "SIM-PUMP", "pump2": "SIM-PUMP2", "adc": "SIM-BZY"}[channel]:
                    raise ControlError("SIM_PORT_ONLY")
                self.connecting.add(channel)
            try:
                device = self.factory(port, c[channel], c[keys[2]])
            except Exception:
                with self.lock:
                    self.connecting.discard(channel)
                raise
            with self.lock:
                self.connecting.discard(channel)
                for key in keys:
                    if self.config[key] != c[key]:
                        self.saved = False
                    self.config[key] = c[key]
                self.devices[channel] = device
                self.connected = True
                now = self.clock()
                if channel != "adc":
                    self.pumps[channel] = dict(valid=False, last_status=None, status=0, rpm=None, error="")
                    if self.state == "DISCONNECTED":
                        self.state, self.fault = "IDLE", "NONE"
                    self.pump_valid = False
                    self.last_status = None
                    self.applied_rpm = None
                    self.next_status = now
                else:
                    self.last_sample = None
                    self.adc_valid = False
                    self.next_adc = now
                self.last_tick = now
                if self.recorder is None or self.recorder.closed:
                    try:
                        self.recorder = CsvRecorder(self.folder / "records", self.simulation)
                    except OSError as exc:
                        self._event("RECORD_ERROR", exc, "error")
                self._event("CONNECTED", channel)

    def disconnect_channel(self, channel):
        if channel not in CHANNEL_FIELDS:
            raise ControlError("CHANNEL")
        with self.lifecycle:
            with self.lock:
                if channel not in self.devices:
                    return
                if channel != "adc" and (self.state not in ("IDLE", "FAULT") or self.stop_required
                        or not self._individual_fresh(channel, self.clock()) or self.pumps[channel]["status"] & 1):
                    raise ControlError("STOP_AND_WAIT")
                if channel == "adc":
                    if self.commission:
                        raise ControlError("STOP_AND_WAIT")
                    self._voltage_failed(self.clock())
                pending = self.pending.pop("pump" if channel != "adc" else "adc", None)
                device = self.devices.pop(channel)
                if channel != "adc":
                    if not any(key in self.devices for key in ("pump", "pump2")):
                        self.state, self.fault = "DISCONNECTED", "NONE"
                    self.pumps[channel]["valid"] = False
                    self.pump_valid = False
                    self.applied_rpm = None
                    self.target = self.flow = 0
                else:
                    self.adc_valid = False
                    self.last_sample = None
                self.connected = bool(self.devices)
            if pending:
                try:
                    pending[0].result(timeout=3)
                except Exception as exc:
                    with self.lock:
                        self._event("DISCONNECT_TRANSACTION", exc, "warning")
            device.close()
            if not self.connected and self.recorder:
                self.recorder.close()
            with self.lock:
                self._event("DISCONNECTED", channel)

    def disconnect(self):
        with self.lifecycle:
            with self.lock:
                if not self.connected:
                    return
                if (self.state not in ("IDLE", "FAULT") or self.stop_required
                        or not self._pump_fresh(self.clock()) or self.status & 1):
                    raise ControlError("STOP_AND_WAIT")
                self.connected = False
                pending = list(self.pending.values())
                self.pending.clear()
                devices = self.devices
                self.devices = {}
                self.state = "DISCONNECTED"
                self.adc_valid = self.pump_valid = False
                recorder = self.recorder
                self.epoch += 1
            for future, _, _, _ in pending:
                try:
                    future.result(timeout=3)
                except Exception as exc:
                    with self.lock:
                        self._event("DISCONNECT_TRANSACTION", exc, "warning")
            for device in devices.values():
                device.close()
            if recorder:
                recorder.close()
            with self.lock:
                self._event("DISCONNECTED")

    def command(self, action, payload=None):
        payload = payload or {}
        if action in ("connect_channel", "disconnect_channel"):
            if action == "connect_channel":
                self.connect_channel(payload.get("channel"), payload.get("settings"))
            else:
                self.disconnect_channel(payload.get("channel"))
            return "OK"
        if action == "connect":
            self.connect()
            return "CONNECTED"
        if action == "disconnect":
            self.disconnect()
            return "DISCONNECTED"
        with self.lock:
            now = self.clock()
            if action == "apply":
                self._require_idle()
                c = validate_config(payload.get("config"))
                for channel, keys in CHANNEL_FIELDS.items():
                    if channel in self.devices and any(c[k] != self.config[k] for k in keys):
                        raise ControlError("DISCONNECT_FIRST")
                self.config, self.saved = c, False
                self.epoch += 1
                self.last_sample = None
                self.adc_valid = False
                self.stage = 0
                self.candidate = None
                self.next_adc = self.next_status = now
                self._event("CONFIG_APPLIED")
                return "APPLIED"
            if action == "save":
                self._require_idle()
                self.folder.mkdir(parents=True, exist_ok=True)
                temporary = self.folder / "config.json.tmp"
                with temporary.open("w", encoding="utf-8") as file:
                    json.dump(dict(schema="pump-pc-v2", config=self.config), file, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.folder / "config.json")
                self.saved = True
                self._event("CONFIG_SAVED")
                return "SAVED"
            if not self.connected:
                raise ControlError("NOT_CONNECTED")
            if action not in ("commission",) and not any(key in self.devices for key in self._pump_keys()):
                raise ControlError("PUMP_NOT_CONNECTED")
            if action == "set_speed":
                rpm = payload.get("rpm_x100")
                if self.state != "MANUAL" or not self._pump_fresh(now):
                    raise ControlError("MANUAL_ONLY")
                if type(rpm) is not int or not self.config["rpm_min"] <= rpm <= self.config["rpm_max"]:
                    raise ControlError("RPM_RANGE")
                self.manual_rpm = rpm
                return "SPEED_UPDATED"
            if action in ("start", "manual"):
                if action == "start":
                    if "adc" not in self.devices:
                        raise ControlError("ADC_NOT_CONNECTED")
                    if any(key not in self.devices for key in ("pump", "pump2")):
                        raise ControlError("PUMPS_REQUIRED")
                self._require_stopped(now)
                if action == "start":
                    if not self.config["speed_mode"] and not self.config["k"]:
                        raise ControlError("CALIBRATION_REQUIRED")
                    if not self._voltage_fresh(now):
                        raise ControlError("VOLTAGE_INVALID")
                    self._begin_flow(now)
                else:
                    rpm = payload.get("rpm_x100")
                    if type(rpm) is not int or not self.config["rpm_min"] <= rpm <= self.config["rpm_max"]:
                        raise ControlError("RPM_RANGE")
                    self.manual_rpm = rpm
                    direction = payload.get("direction", self.config["direction"])
                    direction2 = payload.get("direction2", self.config["direction2"])
                    if any(type(d) is not int or d not in (0, 1) for d in (direction, direction2)):
                        raise ControlError("BUS")
                    if self.config["direction"] != direction:
                        self.config["direction"], self.saved = direction, False
                    if self.config["direction2"] != direction2:
                        self.config["direction2"], self.saved = direction2, False
                self.manual = action == "manual"
                self.state, self.fault, self.start_step = "STARTING", "NONE", 0
                self._event("START_REQUESTED", action)
                return "STARTING"
            if action == "stop":
                self._request_stop("NONE", now)
                return "STOP_REQUESTED"
            if action == "resume":
                if not (self.state == "HOLD" and self.fault == "VOLTAGE"
                        and self._voltage_fresh(now) and self._pump_fresh(now)
                        and self.status & 1 and not self.status & 12
                        and self.applied_rpm is not None and (self.config["speed_mode"] or self.config["k"])):
                    raise ControlError("CANNOT_RESUME")
                self._begin_flow(now)
                self.flow = self.applied_rpm if self.config["speed_mode"] else self.applied_rpm * self.config["k"] / 100
                self.state, self.fault = "RUN", "NONE"
                self._event("RESUMED")
                return "RESUMED"
            if action == "reset":
                if not (self.state == "FAULT" and not self.stop_required
                        and self._pump_fresh(now) and not self.status & 13):
                    raise ControlError("FAULT_NOT_CLEARED")
                self.state, self.fault = "IDLE", "NONE"
                self._event("RESET")
                return "RESET"
            if action == "commission":
                self._require_stopped(now)
                if "adc" not in self.devices:
                    raise ControlError("ADC_NOT_CONNECTED")
                address, baud = payload.get("old_address"), payload.get("old_baud")
                if type(address) is not int or not 1 <= address <= 247 or baud not in BAUDS:
                    raise ControlError("COMMISSION_ARGUMENTS")
                self.commission = dict(old_address=address, old_baud=baud, step=0)
                self.state = "COMMISSION"
                self.adc_valid = False
                self._event("COMMISSION_STARTED")
                return "COMMISSIONING"
            raise ControlError("UNKNOWN_COMMAND")

    def _request_stop(self, reason, now):
        if self.commission:
            self.commission = None
            self._event("COMMISSION_CANCELLED", "Some registers may already have changed", "warning")
        if reason != "NONE":
            self.fault = reason
        new_state = "STOPPING" if self.fault == "NONE" else "FAULT"
        if self.state != new_state or not self.stop_required:
            self._event("STOP_REQUESTED", self.fault, "error" if self.fault != "NONE" else "info")
        self.state = new_state
        self.stop_required = True
        self.next_stop = now
        self.candidate = None

    def _voltage_failed(self, now):
        self.adc_valid = False
        self.candidate = None
        if self.state == "RUN":
            self.state, self.fault = "HOLD", "VOLTAGE"
            self._event("VOLTAGE_HOLD", "Last acknowledged speed retained", "error")
        elif self.state == "STARTING" and not self.manual:
            self._request_stop("VOLTAGE", now)

    def _begin_flow(self, now):
        self.stage = sum(self.filtered >= threshold for threshold in rule_thresholds(self.config))
        self.candidate = None
        self.target = self.flow = self._stage_target()
        self.ramp_at = now

    def _stage_target(self):
        if self.config["thresholds"]:
            return self.config["speeds"][self.stage]
        return self.config[("r" if self.config["speed_mode"] else "q") + str(self.stage)]

    def _observe(self, raw, now):
        c = self.config
        self.raw = raw
        self.mv = raw * c["full_mv"] / c["raw_full"]
        if not 0 <= raw <= c["raw_full"] or not c["vmin"] <= self.mv <= c["vmax"]:
            self._voltage_failed(now)
            return
        self.filtered = self.mv if self.last_sample is None else self.filtered + (self.mv - self.filtered) / 4
        self.last_sample, self.adc_valid = now, True
        stage = self.stage
        thresholds = rule_thresholds(c)
        while stage < len(thresholds) and self.filtered >= thresholds[stage]:
            stage += 1
        while stage > 0 and self.filtered < thresholds[stage - 1] - c["hyst"]:
            stage -= 1
        if stage == self.stage:
            self.candidate = None
        else:
            if self.candidate != stage:
                self.candidate, self.candidate_since = stage, now
            if (now - self.candidate_since) * 1000 >= c["debounce"]:
                self.stage, self.candidate = stage, None
                self._event("STAGE_CHANGED", stage + 1)
        if self.state in ("RUN", "STARTING"):
            self.target = self._stage_target()

    def _submit(self, channel, operation, value=None):
        c = dict(self.config)
        if channel == "pump" and "pump2" in self.devices:
            devices = {key: self.devices[key] for key in self._pump_keys() if key in self.devices}
            future = self.executor.submit(self._pump_call, devices, operation, value, c)
        else:
            future = self.executor.submit(self.devices[channel].call, operation, value, c)
        self.pending[channel] = (future, operation, value, self.epoch)

    def _pump_call(self, devices, operation, value, config):
        # Each port has its own worker. Collect every reply even if one port fails,
        # so a failed stop cannot prevent a stop request reaching the healthy pump.
        futures = {}
        for key, device in devices.items():
            settings = dict(config)
            if key == "pump2":
                settings.update(pump=config["pump2"], baud=config["pump2_baud"], direction=config["direction2"])
            futures[key] = self.pump_executor.submit(device.call, operation, value, settings)
        replies = {}
        for key, future in futures.items():
            try:
                replies[key] = dict(value=future.result(), error="")
            except Exception as exc:
                replies[key] = dict(value=None, error=str(exc) or type(exc).__name__)
        return replies

    def _pump_reply(self, replies, operation, value, now):
        errors = []
        for key, reply in replies.items():
            p = self.pumps[key]
            p["error"] = reply["error"]
            if reply["error"]:
                p["valid"] = False
                if operation != "status":
                    p["rpm"] = None
                errors.append(key + ": " + reply["error"])
            elif operation == "status":
                p.update(status=reply["value"], valid=True, last_status=now)
                if not reply["value"] & 1 and self.state in ("STOPPING", "FAULT"):
                    p["rpm"] = 0
            elif operation in ("initial_speed", "speed"):
                p["rpm"] = value
        if errors:
            raise ControlError("; ".join(errors))
        if operation != "status":
            return value
        statuses = [reply["value"] for reply in replies.values()]
        controlling = self.state in ("RUN", "MANUAL") or (self.state == "HOLD" and self.fault == "VOLTAGE")
        controlling |= self.state == "STARTING" and self.start_step == 4
        if controlling:
            if any(status & 1 for status in statuses) and not all(status & 1 for status in statuses):
                self._request_stop("PUMP_MISMATCH", now)
            for key, reply in replies.items():
                expected = self.config["direction2" if key == "pump2" else "direction"]
                if (reply["value"] >> 1) & 1 != expected:
                    self._request_stop("DIRECTION_CHANGED", now)
        # A group is stopped only after EVERY connected pump reports stopped.
        return (int(any(s & 1 for s in statuses)) | (self.config["direction"] << 1)
                | (4 if any(s & 4 for s in statuses) else 0) | (8 if any(s & 8 for s in statuses) else 0))

    def _complete(self, channel, now):
        pending = self.pending.get(channel)
        if not pending or not pending[0].done():
            return
        future, op, value, epoch = self.pending.pop(channel)
        try:
            result = future.result()
            if epoch != self.epoch:
                return
            if channel == "pump":
                replies = result if isinstance(result, dict) else {"pump": dict(value=result, error="")}
                result = self._pump_reply(replies, op, value, now)
        except Exception as exc:
            if epoch != self.epoch:
                return
            if channel == "adc":
                self.adc_errors += 1
                self._voltage_failed(now)
                if op == "commission" and self.commission:
                    self.commission = None
                    self.state = "IDLE"
                    self._event("COMMISSION_FAILED", exc, "error")
            else:
                if "pump2" not in self.devices:
                    self.pumps["pump"]["valid"] = False
                    self.pumps["pump"]["error"] = str(exc)
                    if op != "status":
                        self.pumps["pump"]["rpm"] = None
                self.pump_errors += 1
                self.pump_valid = False
                if op != "status":
                    self.applied_rpm = None
                if self.state in ACTIVE:
                    self._request_stop("PUMP_COMM", now)
            self._event("COMM_ERROR", channel + ": " + str(exc), "error")
            return
        if epoch != self.epoch:
            return
        if op == "adc":
            if self.state != "COMMISSION":
                self._observe(result, now)
        elif op == "commission":
            if self.commission:
                self.commission["step"] += 1
                if self.commission["step"] == 4:
                    self.commission = None
                    self.state = "IDLE"
                    self.next_adc = now + 5
                    self._event("COMMISSION_DONE_POWER_CYCLE_ADC")
        elif op == "status":
            self.status = result
            self.last_status, self.pump_valid = now, True
            if result & 12:
                self._request_stop("LEAK" if result & 4 else "STALL", now)
            if self.state in ("STOPPING", "FAULT"):
                if not result & 1:
                    self.stop_required = False
                    self.applied_rpm = 0
                    for key in self._pump_keys():
                        self.pumps[key]["rpm"] = 0
                    self.flow = self.target = 0
                    if self.state == "STOPPING":
                        self.state = "IDLE"
                        self._event("STOP_CONFIRMED")
                else:
                    self.stop_required = True
            elif self.state == "STARTING" and self.start_step == 4:
                if not result & 1:
                    self._request_stop("UNEXPECTED_STOP", now)
                elif (result >> 1) & 1 != self.config["direction"]:
                    self._request_stop("DIRECTION_CHANGED", now)
                else:
                    self.state = "MANUAL" if self.manual else "RUN"
                    self._event("RUN_CONFIRMED", self.state)
            elif self.state in ("RUN", "MANUAL") or (self.state == "HOLD" and self.fault == "VOLTAGE"):
                if not result & 1:
                    self._request_stop("UNEXPECTED_STOP", now)
                elif (result >> 1) & 1 != self.config["direction"]:
                    self._request_stop("DIRECTION_CHANGED", now)
            elif self.state == "IDLE" and result & 1:
                self.state, self.fault = "HOLD", "EXTERNAL_RUN"
                self.applied_rpm = None
                self._event("EXTERNAL_RUN", "No automatic takeover", "error")
            elif self.state == "COMMISSION" and result & 1:
                self._request_stop("EXTERNAL_RUN", now)
        elif op == "stop":
            self.next_status = now
        elif op in ("initial_speed", "speed"):
            self.applied_rpm = value
            if op == "initial_speed" and self.state == "STARTING":
                if self.manual:
                    self.target = self.flow = 0
                self.start_step += 1
        elif op in ("mode", "direction", "run") and self.state == "STARTING":
            self.start_step += 1

    def step(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if not self.connected:
                return
            # A sleep/resume gap invalidates automatic control; never catch up with a large jump.
            if now - self.last_tick > 2 and self.state in ACTIVE:
                if self.state == "STARTING":
                    self._request_stop("SYSTEM_PAUSE", now)
                elif self.state in ("RUN", "MANUAL"):
                    self.state, self.fault = "HOLD", "SYSTEM_PAUSE"
                    self._event("SYSTEM_PAUSE", "Stop and restart after checking devices", "error")
            self.last_tick = now
            self._complete("pump", now)
            self._complete("adc", now)
            if (self.state == "RUN" or (self.state == "STARTING" and not self.manual)) and not self._voltage_fresh(now):
                self._voltage_failed(now)
            if (self.state in ("RUN", "MANUAL", "HOLD", "COMMISSION")
                    and self.fault not in ("SYSTEM_PAUSE", "EXTERNAL_RUN")
                    and all(key in self.devices for key in self._pump_keys()) and not self._pump_fresh(now)):
                self._request_stop("PUMP_COMM", now)
            if self.state == "RUN" and now - self.ramp_at >= 0.1:
                step = self.config["ramp"] * min(now - self.ramp_at, 0.25)
                self.flow += max(-step, min(step, self.target - self.flow))
                self.ramp_at = now
            if any(key in self.devices for key in self._pump_keys()) and "pump" not in self.pending:
                if self.stop_required and now >= self.next_stop:
                    self._submit("pump", "stop")
                    self.next_stop = now + 1
                elif self.state == "STARTING":
                    op = ("mode", "direction", "initial_speed", "run", "status")[self.start_step]
                    rpm = self.manual_rpm if self.manual else flow_to_rpm(self.config, self.flow)
                    self._submit("pump", op, rpm if op == "initial_speed" else None)
                elif now >= self.next_status:
                    self._submit("pump", "status")
                    self.next_status = now + 0.5
                elif self.state == "RUN" and now >= self.next_speed:
                    rpm = flow_to_rpm(self.config, self.flow)
                    if not self.config["rpm_min"] <= rpm <= self.config["rpm_max"]:
                        self._request_stop("RPM_RANGE", now)
                    elif rpm != self.applied_rpm:
                        self._submit("pump", "speed", rpm)
                    self.next_speed = now + 0.25
                elif self.state == "MANUAL" and now >= self.next_speed:
                    if self.manual_rpm != self.applied_rpm:
                        self._submit("pump", "speed", self.manual_rpm)
                    self.next_speed = now + 0.25
            if "adc" in self.devices and "adc" not in self.pending:
                if self.commission:
                    step = self.commission["step"]
                    values = (0, self.config["adc"], 0, BAUDS.index(self.config["adc_baud"]))
                    value = dict(self.commission, register=(0x31, 0x32, 0x34, 0x33)[step], value=values[step])
                    self._submit("adc", "commission", value)
                elif now >= self.next_adc:
                    self._submit("adc", "adc")
                    self.next_adc = now + self.config["sample"] / 1000
            if now >= self.next_record:
                self.seq += 1
                t = self._telemetry(now)
                point = dict(t, seq=self.seq, time=datetime.now().astimezone().isoformat(),
                             estimate=(t["rpm_x100"] * self.config["k"] / 100000)
                             if t["pump_valid"] and t["rpm_known"] and self.config["k"] and not self.config["speed_mode"] else None)
                self.history.append(point)
                if self.recorder:
                    self.recorder.record(t, self.config)
                self.next_record = now + 0.5

    def _telemetry(self, now):
        return dict(
            ms=max(0, int((now - self.started) * 1000)), state=self.state, fault=self.fault,
            raw=self.raw, mv=round(self.mv), filtered_mv=round(self.filtered),
            voltage_valid=int(self._voltage_fresh(now)),
            sample_age_ms=int((now - self.last_sample) * 1000) if self.last_sample is not None else None,
            pump_valid=int(self._pump_fresh(now)), status=self.status, stage=self.stage,
            target_ul_min=0 if self.config["speed_mode"] else round(self.target),
            command_ul_min=0 if self.config["speed_mode"] else round(self.flow),
            rpm_x100=self.applied_rpm or 0, rpm_known=int(self.applied_rpm is not None),
            target_rpm_x100=round(self.target) if self.config["speed_mode"] else flow_to_rpm(self.config, self.target),
            adc_errors=self.adc_errors, pump_errors=self.pump_errors, saved=int(self.saved),
            stop_required=self.stop_required,
            pumps={key: dict(connected=key in self.devices, valid=int(self._individual_fresh(key, now)),
                             status=p["status"], rpm_x100=p["rpm"] or 0,
                             rpm_known=int(p["rpm"] is not None), error=p["error"])
                   for key, p in self.pumps.items()},
        )

    def snapshot(self, since=0):
        with self.lock:
            return dict(
                server_id=self.server_id, connected=self.connected, simulation=self.simulation,
                config=deepcopy(self.config), t=self._telemetry(self.clock()),
                channels={key: key in self.devices for key in ("pump", "adc", "pump2")},
                connecting=sorted(self.connecting),
                events=list(self.events), history=[dict(p) for p in self.history if p["seq"] > since],
                log_name=self.recorder.path.name if self.recorder else None,
                log_error=self.recorder.error if self.recorder else None,
                simulation_inputs=(dict(voltage_mv=self.plant.voltage_mv, adc_online=self.plant.adc_online,
                                        pump_online=self.plant.pump_online) if self.plant else None),
            )

    def _loop(self):
        while not self.shutdown_event.wait(0.02):
            try:
                self.step()
            except Exception as exc:
                with self.lock:
                    self._event("CONTROL_ERROR", exc, "error")
                    if self.connected:
                        self._request_stop("CONTROL_ERROR", self.clock())

    def close(self):
        self.shutdown_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.executor.shutdown(wait=True)
        self.pump_executor.shutdown(wait=True)
        for device in self.devices.values():
            device.close()
        if self.recorder:
            self.recorder.close()
        # Exiting cannot guarantee a physical stop. Never invent a successful stop acknowledgement.
