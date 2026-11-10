"""Controller regression tests; all devices are simulated or use in-memory serial ports."""
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main, mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import csv
import io
import json
import threading
import time

import minimalmodbus

from pc_control import (BAUDS, COMM_FIELDS, DEFAULTS, ControlError, Controller, ModbusDevice,
                        SimDevice, SimPlant, validate_config)
from pc_server import create_server


class InlineExecutor:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True):
        pass


class ControlTests(TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.now = 100.0
        self.c = Controller(self.folder.name, simulation=True, clock=lambda: self.now,
                            executor=InlineExecutor(), autostart=False)
        self.c.plant.delay = 0
        self.config = dict(DEFAULTS, pump_port="SIM-PUMP", pump2_port="SIM-PUMP2", adc_port="SIM-BZY")
        self.c.command("apply", {"config": self.config})

    def tearDown(self):
        self.c.close()
        self.folder.cleanup()

    def advance(self, seconds):
        for _ in range(round(seconds / 0.01)):
            self.now += 0.01
            self.c.step()

    def connect(self, calibrated=True):
        if calibrated:
            self.config["k"] = 1000
            self.c.command("apply", {"config": self.config})
        self.c.connect()
        self.advance(1)

    def start(self):
        self.connect()
        self.c.command("start")
        self.advance(1)
        self.assertEqual(self.c.state, "RUN")

    def stop(self, expected="IDLE"):
        self.c.command("stop")
        self.advance(1.2)
        self.assertEqual(self.c.state, expected)
        self.assertFalse(self.c.plant.status & 1)

    def test_validation_and_atomic_apply(self):
        self.assertEqual(validate_config(self.config)["pump"], validate_config(self.config)["adc"])
        for change in (dict(t1=1600), dict(adc_baud=12345), dict(k=True), dict(q0=0), dict(hyst=0)):
            with self.subTest(change=change), self.assertRaises(ControlError):
                self.c.command("apply", {"config": dict(self.config, **change)})
            self.assertEqual(self.c.config, self.config)
        with self.assertRaises(ControlError):
            validate_config(dict(self.config, unexpected=1))

    def test_same_port_rejected(self):
        self.c.command("apply", {"config": dict(self.config, adc_port="sim-pump")})
        with self.assertRaisesRegex(ControlError, "SAME_PORT"):
            self.c.connect()
        self.assertFalse(self.c.connected)

    def test_10v_voltage_conversion_reference_points(self):
        self.assertEqual(DEFAULTS["full_mv"], 10000)
        self.c.command("apply", {"config": dict(self.config, full_mv=10000, vmin=0, vmax=10000)})
        for raw, expected_mv in ((6554, 1000), (8520, 1300), (10223, 1560), (65535, 10000)):
            with self.subTest(raw=raw):
                self.c._observe(raw, self.now)
                self.assertAlmostEqual(self.c.mv, expected_mv, delta=0.2)
                self.assertTrue(self.c.adc_valid)

    def test_pump_only_manual_start_update_stop(self):
        self.c.command("connect_channel", {"channel": "pump"})
        self.advance(1)
        self.assertEqual(self.c.snapshot()["channels"], {"pump": True, "adc": False, "pump2": False})
        self.c.command("manual", {"rpm_x100": 5000, "direction": 1})
        self.advance(1)
        self.assertEqual((self.c.state, self.c.plant.rpm, self.c.plant.status & 3), ("MANUAL", 5000, 3))
        self.c.command("set_speed", {"rpm_x100": 8050})
        self.advance(1)
        self.assertEqual(self.c.plant.rpm, 8050)
        self.assertEqual(self.c.adc_errors, 0)
        self.stop()
        self.c.disconnect_channel("pump")
        self.assertFalse(self.c.connected)

    def test_adc_only_then_pump_and_independent_disconnect(self):
        self.c.connect_channel("adc")
        self.advance(1)
        self.assertTrue(self.c.snapshot()["t"]["voltage_valid"])
        self.assertEqual(self.c.plant.writes, [])
        self.assertEqual(self.c.pump_errors, 0)
        with self.assertRaisesRegex(ControlError, "PUMP_NOT_CONNECTED"):
            self.c.command("manual", {"rpm_x100": 1000})
        self.c.connect_channel("pump")
        self.advance(1)
        self.c.disconnect_channel("pump")
        self.advance(1)
        self.assertEqual(self.c.snapshot()["channels"], {"pump": False, "adc": True, "pump2": False})
        self.assertTrue(self.c.snapshot()["t"]["voltage_valid"])
        self.c.disconnect_channel("adc")
        self.assertFalse(self.c.connected)

    def test_direct_rpm_rules_need_no_calibration(self):
        self.config.update(speed_mode=1, k=0, r0=8500, r1=5500, r2=2000)
        self.c.command("apply", {"config": self.config})
        self.c.connect_channel("pump")
        self.advance(1)
        with self.assertRaisesRegex(ControlError, "ADC_NOT_CONNECTED"):
            self.c.command("start")
        self.c.connect_channel("adc")
        self.c.connect_channel("pump2")
        self.advance(1)
        self.c.command("start")
        self.advance(1)
        self.assertEqual((self.c.state, self.c.plant.rpm), ("RUN", 8500))
        self.c.plant.update({"voltage_mv": 1650})
        self.advance(5)
        self.assertEqual(self.c.plant.rpm, 2000)
        self.c.disconnect_channel("adc")
        self.advance(1)
        self.assertEqual((self.c.state, self.c.fault, self.c.plant.rpm), ("HOLD", "VOLTAGE", 2000))
        self.c.connect_channel("adc")
        self.advance(1)
        self.assertEqual(self.c.state, "HOLD")
        self.c.command("resume")
        self.advance(1)
        self.assertEqual(self.c.state, "RUN")
        self.stop()

    def test_adc_loss_does_not_change_manual_running(self):
        self.c.connect_channel("pump")
        self.advance(1)
        self.c.command("manual", {"rpm_x100": 4200})
        self.advance(1)
        self.c.connect_channel("adc")
        self.advance(1)
        self.c.plant.update({"adc_online": False})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault, self.c.plant.rpm), ("MANUAL", "NONE", 4200))
        self.c.disconnect_channel("adc")
        self.advance(1)
        self.assertEqual(self.c.state, "MANUAL")

    def test_failed_adc_open_preserves_running_pump(self):
        self.c.connect_channel("pump")
        self.advance(1)
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance(1)
        with mock.patch.object(self.c, "factory", side_effect=OSError("missing ADC")):
            with self.assertRaises(OSError):
                self.c.connect_channel("adc")
        self.advance(1)
        self.assertEqual(self.c.state, "MANUAL")
        self.assertEqual(self.c.snapshot()["channels"], {"pump": True, "adc": False, "pump2": False})
        self.assertFalse(self.c.connecting)
        self.stop()

    def test_individual_connection_settings_and_duplicate_ports(self):
        self.c.command("connect_channel", {"channel": "pump", "settings": {
            "pump_port": "SIM-PUMP", "pump": 2, "baud": 9600}})
        self.advance(1)
        self.assertEqual((self.c.config["pump"], self.c.config["baud"]), (2, 9600))
        with self.assertRaisesRegex(ControlError, "SAME_PORT"):
            self.c.command("connect_channel", {"channel": "adc", "settings": {
                "adc_port": "SIM-PUMP", "adc": 1, "adc_baud": 9600}})
        self.assertEqual(self.c.config["adc_port"], "SIM-BZY")
        self.assertEqual(self.c.snapshot()["channels"], {"pump": True, "adc": False, "pump2": False})

    def test_cannot_disconnect_running_pump_individually(self):
        self.start()
        with self.assertRaisesRegex(ControlError, "STOP_AND_WAIT"):
            self.c.disconnect_channel("pump")
        self.assertTrue(self.c.snapshot()["channels"]["pump"])

    def test_old_saved_config_migrates_speed_fields(self):
        old = {key: value for key, value in self.config.items() if key not in ("speed_mode", "r0", "r1", "r2")}
        (Path(self.folder.name) / "config.json").write_text(json.dumps({"schema": "pump-pc-v1", "config": old}), encoding="utf-8")
        other = Controller(self.folder.name, simulation=True, executor=InlineExecutor(), autostart=False)
        try:
            self.assertEqual(other.config["r0"], 12000)
            self.assertFalse(other.config["speed_mode"])
            self.assertTrue(other.saved)
        finally:
            other.close()

    def test_start_preconditions(self):
        self.connect(calibrated=False)
        with self.assertRaisesRegex(ControlError, "CALIBRATION_REQUIRED"):
            self.c.command("start")
        self.config["k"] = 1000
        self.c.command("apply", {"config": self.config})
        with self.assertRaisesRegex(ControlError, "VOLTAGE_INVALID"):
            self.c.command("start")

    def test_stage_ramp_and_hysteresis(self):
        self.start()
        self.assertEqual(self.c.plant.rpm, 12000)
        self.c.plant.update({"voltage_mv": 1650})
        self.advance(5)
        self.assertEqual((self.c.stage, self.c.plant.rpm), (2, 3000))
        self.c.plant.update({"voltage_mv": 1550})
        self.advance(5)
        self.assertEqual(self.c.stage, 2)
        self.c.plant.update({"voltage_mv": 1500})
        self.advance(5)
        self.assertEqual((self.c.stage, self.c.plant.rpm), (1, 6000))
        self.stop()

    def test_voltage_loss_hold_and_manual_resume(self):
        self.start()
        rpm = self.c.plant.rpm
        writes = len([w for w in self.c.plant.writes if w[0] == "speed"])
        self.c.plant.update({"adc_online": False})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "VOLTAGE"))
        self.assertEqual(self.c.plant.rpm, rpm)
        self.assertEqual(len([w for w in self.c.plant.writes if w[0] == "speed"]), writes)
        with self.assertRaisesRegex(ControlError, "CANNOT_RESUME"):
            self.c.command("resume")
        self.c.plant.update({"adc_online": True})
        self.advance(1)
        self.assertEqual(self.c.state, "HOLD")
        self.c.command("resume")
        self.advance(0.5)
        self.assertEqual(self.c.state, "RUN")

    def test_out_of_range_and_stale_data(self):
        self.start()
        self.c.plant.update({"voltage_mv": 0})
        self.advance(1)
        t = self.c.snapshot()["t"]
        self.assertFalse(t["voltage_valid"])
        self.assertEqual(t["state"], "HOLD")
        self.assertEqual(t["mv"], 0)
        self.assertTrue(self.c.plant.status & 1)

    def test_pump_disconnection_cannot_guarantee_stop(self):
        self.start()
        self.c.plant.update({"pump_online": False})
        self.advance(2)
        self.assertEqual(self.c.state, "FAULT")
        self.assertTrue(self.c.plant.status & 1)
        self.assertTrue(self.c.snapshot()["t"]["voltage_valid"])
        self.c.plant.update({"pump_online": True})
        self.advance(2)
        self.assertFalse(self.c.plant.status & 1)
        self.assertEqual(self.c.state, "FAULT")
        self.c.command("reset")
        self.assertEqual(self.c.state, "IDLE")

    def test_pump_alarms(self):
        self.start()
        self.c.plant.update({"leak": True})
        self.advance(1.2)
        self.assertEqual(self.c.fault, "LEAK")
        self.assertFalse(self.c.plant.status & 1)
        with self.assertRaisesRegex(ControlError, "FAULT_NOT_CLEARED"):
            self.c.command("reset")
        self.c.plant.update({"leak": False})
        self.advance(1)
        self.c.command("reset")
        self.c.command("start")
        self.advance(1)
        self.c.plant.update({"stall": True})
        self.advance(1.2)
        self.assertEqual(self.c.fault, "STALL")
        self.assertFalse(self.c.plant.status & 1)

    def test_external_running_pump_not_taken_over(self):
        self.c.plant.update({"external_run": True})
        self.connect()
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "EXTERNAL_RUN"))
        self.assertEqual(self.c.plant.writes, [])
        with self.assertRaises(ControlError):
            self.c.command("resume")
        self.stop("FAULT")
        self.c.command("reset")
        self.assertEqual(self.c.state, "IDLE")

    def test_fault_latch_stops_an_external_restart(self):
        self.start()
        self.c.plant.update({"stall": True})
        self.advance(1.5)
        self.c.plant.update({"stall": False})
        self.advance(1)
        self.assertEqual(self.c.state, "FAULT")
        self.assertFalse(self.c.stop_required)
        self.c.plant.update({"external_run": True})
        self.advance(2)
        self.assertEqual(self.c.state, "FAULT")
        self.assertFalse(self.c.plant.status & 1)

    def test_unexpected_stop_latches_fault(self):
        self.start()
        self.c.plant.update({"external_run": False})
        self.advance(1.5)
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "PUMP_MISMATCH"))

    def test_stop_during_start_and_disconnect_lock(self):
        self.connect()
        self.c.command("start")
        self.advance(0.02)
        with self.assertRaises(ControlError):
            self.c.disconnect()
        self.stop()
        self.assertNotIn("run", [x[0] for x in self.c.plant.writes])
        self.c.disconnect()
        self.assertEqual(self.c.state, "DISCONNECTED")

    def test_manual_without_calibration_or_voltage(self):
        self.c.plant.update({"adc_online": False})
        self.connect(calibrated=False)
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance(1)
        self.assertEqual((self.c.state, self.c.plant.rpm), ("MANUAL", 5000))
        self.stop()

    def test_communication_changes_require_disconnect(self):
        self.connect()
        for key in COMM_FIELDS:
            value = "other" if key.endswith("_port") else (9600 if key == "baud" else 115200 if key == "adc_baud" else 2)
            with self.subTest(key=key), self.assertRaisesRegex(ControlError, "DISCONNECT_FIRST"):
                self.c.command("apply", {"config": dict(self.c.config, **{key: value})})

    def test_commission_is_adc_only_and_can_be_cancelled(self):
        self.connect()
        self.c.command("commission", {"old_address": 1, "old_baud": 9600})
        self.advance(0.2)
        writes = [value for op, value in self.c.plant.writes if op == "commission"]
        self.assertEqual([(v["register"], v["value"]) for v in writes], [(0x31, 0), (0x32, 1), (0x34, 0), (0x33, 1)])
        self.assertEqual(self.c.state, "IDLE")
        self.assertEqual(self.c.config["baud"], 115200)
        self.c.command("commission", {"old_address": 1, "old_baud": 9600})
        self.advance(0.01)
        self.stop()
        self.assertTrue(any(e["event"] == "COMMISSION_CANCELLED" for e in self.c.events))
        self.assertEqual(len([op for op, _ in self.c.plant.writes if op == "commission"]), 5)

    def test_sleep_gap_does_not_auto_resume(self):
        self.start()
        self.now += 3
        self.c.step()
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "SYSTEM_PAUSE"))
        self.advance(1)
        with self.assertRaises(ControlError):
            self.c.command("resume")
        self.stop("FAULT")

    def test_background_csv_and_invalid_values_are_blank(self):
        self.start()
        self.c.plant.update({"adc_online": False})
        self.advance(2)
        self.c.recorder.queue.join()
        rows = list(csv.DictReader(io.StringIO(self.c.recorder.download().decode("utf-8-sig"))))
        self.assertGreater(len(rows), 5)
        invalid = [r for r in rows if r["state"] == "HOLD"]
        self.assertTrue(invalid)
        self.assertTrue(all(r["voltage_mv"] == r["filtered_mv"] == "" for r in invalid))
        self.assertTrue(all(r["rpm_x100"] == "12000" for r in invalid))

    def test_save_reload_and_corruption_default(self):
        self.config["k"] = 1000
        self.c.command("apply", {"config": self.config})
        self.c.command("save")
        other = Controller(self.folder.name, simulation=True, executor=InlineExecutor(), autostart=False)
        try:
            self.assertEqual(other.config["k"], 1000)
            self.assertEqual(other.state, "DISCONNECTED")
            self.assertFalse(other.connected)
        finally:
            other.close()
        for content in ("corrupt", "[]", "null", '{"schema":"wrong"}'):
            with self.subTest(content=content):
                (Path(self.folder.name) / "config.json").write_text(content, encoding="utf-8")
                other = Controller(self.folder.name, simulation=True, executor=InlineExecutor(), autostart=False)
                try:
                    self.assertEqual(other.config["k"], 0)
                    self.assertFalse(other.saved)
                    self.assertEqual(other.events[-1]["event"], "CONFIG_LOAD_FAILED")
                finally:
                    other.close()

    def test_direction_changed_and_unexpected_stop(self):
        self.start()
        self.c.plant.status |= 2
        self.advance(1.2)
        self.assertEqual(self.c.fault, "DIRECTION_CHANGED")
        self.assertFalse(self.c.plant.status & 1)

    def test_lost_speed_ack_stops_and_latches(self):
        self.start()
        original = self.c.devices["pump"].call

        def call(op, value, config):
            result = original(op, value, config)
            if op == "speed":
                raise OSError("Lost acknowledgement after write")
            return result

        self.c.devices["pump"].call = call
        self.c.plant.update({"voltage_mv": 1650})
        self.advance(5)
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "PUMP_COMM"))
        self.assertFalse(self.c.plant.status & 1)


class WireSerial:
    """MinimalModbus talks to this fake serial port using actual RTU request/reply bytes."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.is_open = True
        self.frames = []
        self.reply = b""
        self.corrupt = False
        self.fail = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self.reply = b""

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.reply)

    def flush(self):
        pass

    def write(self, data):
        self.frames.append(data)
        if self.fail:
            self.reply = b""
            return len(data)
        if data[1] == 4:
            body = data[:2] + b"\x02\x3d\x70"
        else:
            body = data[:6]
        self.reply = body + minimalmodbus._calculate_crc(body)
        if self.corrupt:
            self.reply = self.reply[:-1] + bytes([self.reply[-1] ^ 1])
        return len(data)

    def read(self, size):
        data, self.reply = self.reply[:size], self.reply[size:]
        return data


class ProtocolTests(TestCase):
    def setUp(self):
        minimalmodbus._serialports.clear()
        self.patch = mock.patch("minimalmodbus.serial.Serial", side_effect=lambda **kw: WireSerial(**kw))
        self.patch.start()
        self.device = ModbusDevice("WIRE-TEST", 1, 115200)
        self.wire = self.device.instrument.serial
        self.config = dict(DEFAULTS)

    def tearDown(self):
        self.device.close()
        self.patch.stop()
        minimalmodbus._serialports.clear()

    def test_actual_crc_and_pump_register_wire_encoding(self):
        self.device.call("initial_speed", 10000, self.config)
        self.assertEqual(self.wire.frames[-1].hex(), "0110400100020400002710199c")
        self.device.call("mode", None, self.config)
        self.assertEqual(self.wire.frames[-1][1:9], bytes.fromhex("1040070001020002"))
        self.device.call("run", None, self.config)
        self.assertEqual(self.wire.frames[-1][1:6], bytes.fromhex("050001ff00"))
        self.device.call("status", None, self.config)
        self.assertEqual(self.wire.frames[-1][1:6], bytes.fromhex("0430030001"))

    def test_bzy_scale_request_and_configuration_wire(self):
        self.assertEqual(self.device.call("adc", None, self.config), 15728)
        self.assertEqual(self.wire.frames[-1].hex(), "01040000000131ca")
        self.assertEqual(self.wire.baudrate, 9600)
        self.device.call("commission", {"old_address": 3, "old_baud": 19200, "register": 0x31, "value": 0}, self.config)
        self.assertEqual(self.wire.frames[-1][:6], bytes.fromhex("030600310000"))
        self.assertEqual(self.wire.baudrate, 19200)

    def test_bad_crc_and_timeout_rejected_with_bounded_retries(self):
        self.wire.corrupt = True
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            self.device.call("adc", None, self.config)
        self.assertEqual(len(self.wire.frames), 2)
        self.wire.corrupt = False
        self.wire.fail = True
        with self.assertRaises(minimalmodbus.NoResponseError):
            self.device.call("status", None, self.config)
        self.assertEqual(len(self.wire.frames), 4)


class IntegrationTests(TestCase):
    def test_slow_adc_does_not_block_pump_stop(self):
        with TemporaryDirectory() as folder:
            plant = SimPlant()
            gate = threading.Event()
            gate.set()
            entered = threading.Event()

            class GatedDevice(SimDevice):
                def call(self, op, value, config):
                    if op == "adc" and not gate.is_set():
                        entered.set()
                        if not gate.wait(3):
                            raise OSError("ADC wait timeout")
                    return super().call(op, value, config)

            c = Controller(folder, factory=lambda port, address, baud: GatedDevice(plant, port))
            try:
                c.command("apply", {"config": dict(DEFAULTS, k=1000, pump_port="SIM-PUMP", pump2_port="SIM-PUMP2", adc_port="SIM-BZY")})
                c.connect()

                def wait_for(predicate, timeout=3):
                    deadline = time.monotonic() + timeout
                    while not predicate():
                        if time.monotonic() >= deadline:
                            self.fail("Timed out waiting for controller state")
                        time.sleep(0.02)

                wait_for(lambda: c.snapshot()["t"]["pump_valid"] and c.snapshot()["t"]["voltage_valid"])
                c.command("start")
                wait_for(lambda: c.state == "RUN")
                gate.clear()
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                c.command("stop")
                wait_for(lambda: c.state == "IDLE", timeout=1)
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(gate.is_set())
                self.assertFalse(plant.status & 1)
            finally:
                gate.set()
                c.close()

    def test_http_contract_and_local_control_guard(self):
        with TemporaryDirectory() as folder:
            server = create_server(0, folder, simulation=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)

            def request(path, data=None, token=None, origin=None):
                headers = {}
                if data is not None:
                    headers["Content-Type"] = "application/json"
                if token:
                    headers["X-Control-Token"] = token
                if origin:
                    headers["Origin"] = origin
                return urlopen(Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                       headers=headers), timeout=3)

            try:
                state = json.load(request("/api/state"))
                self.assertFalse(state["connected"])
                self.assertTrue(state["simulation"])
                with self.assertRaises(HTTPError) as exc:
                    request("/api/command", {"action": "stop"})
                self.assertEqual(exc.exception.code, 403)
                with self.assertRaises(HTTPError) as exc:
                    request("/api/command", {"action": "save"}, state["token"], "https://untrusted.example")
                self.assertEqual(exc.exception.code, 403)
                config = dict(DEFAULTS, pump_port="SIM-PUMP", adc_port="SIM-BZY")
                self.assertEqual(json.load(request("/api/command", {"action": "apply", "config": config}, state["token"]))["result"], "APPLIED")
                self.assertEqual(json.load(request("/api/command", {"action": "connect"}, state["token"]))["result"], "CONNECTED")
                time.sleep(0.3)
                self.assertTrue(json.load(request("/api/state"))["t"]["voltage_valid"])
                self.assertIn(b"pc-control.js", request("/pc-control.html").read())
                for path in ("/pc_control.py", "/tmp/conversation_shared.json", "/output/pc-control/config.json"):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as exc:
                        request(path)
                    self.assertEqual(exc.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                server.controller.close()
                thread.join(timeout=2)


if __name__ == "__main__":
    main(verbosity=2)
