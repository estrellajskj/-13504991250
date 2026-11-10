"""Multi-stage and dual-port behavior, with no physical device access."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import json
import threading

from pc_control import ControlError, Controller, DEFAULTS, ModbusDevice, validate_config
from test_pc_control import InlineExecutor, WireSerial


class DualPumpTests(TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.now = 100.0
        self.c = Controller(self.folder.name, simulation=True, clock=lambda: self.now,
                            executor=InlineExecutor(), autostart=False)
        self.c.plant.delay = 0
        self.config = dict(deepcopy(DEFAULTS), dual_pump=1, speed_mode=1,
                           pump_port="SIM-PUMP", pump2_port="SIM-PUMP2", adc_port="SIM-BZY",
                           direction2=1, vmin=0, vmax=10000,
                           thresholds=[820, 876, 1200], speeds=[12000, 9000, 6000, 3000])
        self.c.command("apply", {"config": self.config})

    def tearDown(self):
        self.c.close()
        self.folder.cleanup()

    def advance(self, seconds=1):
        for _ in range(round(seconds / 0.01)):
            self.now += 0.01
            self.c.step()

    def connect(self, adc=False):
        for channel in ("pump", "pump2") + (("adc",) if adc else ()):
            self.c.connect_channel(channel)
        self.advance()

    def start(self, auto=False):
        self.connect(adc=auto)
        self.c.command("start" if auto else "manual", {"rpm_x100": 5000})
        self.advance()
        self.assertEqual(self.c.state, "RUN" if auto else "MANUAL")

    def assert_rpm(self, rpm):
        self.assertEqual((self.c.plant.rpm, self.c.plant.second.rpm), (rpm, rpm))

    def test_manual_two_pumps_opposite_directions_and_live_update(self):
        self.start()
        self.assert_rpm(5000)
        self.assertEqual((self.c.plant.status & 3, self.c.plant.second.status & 3), (1, 3))
        self.assertFalse(self.c.snapshot()["channels"]["adc"])
        self.c.command("set_speed", {"rpm_x100": 7550})
        self.advance()
        self.assert_rpm(7550)
        self.c.command("stop")
        self.advance(1.5)
        self.assertEqual(self.c.state, "IDLE")
        self.assertFalse(self.c.plant.status & 1 or self.c.plant.second.status & 1)

    def test_three_decimal_thresholds_four_stages_and_hysteresis(self):
        self.c.plant.voltage_mv = 700
        self.start(auto=True)
        for mv, stage, rpm in ((700, 0, 12000), (850, 1, 9000), (1000, 2, 6000), (1300, 3, 3000),
                               (1190, 3, 3000), (1150, 2, 6000), (800, 1, 9000), (780, 0, 12000)):
            with self.subTest(mv=mv):
                self.c.plant.update({"voltage_mv": mv})
                self.advance(5)
                self.assertEqual(self.c.stage, stage)
                self.assert_rpm(rpm)

    def test_any_stage_count_one_to_32_and_save_reload(self):
        for thresholds in ([820], list(range(100, 3300, 100))):
            config = dict(self.config, thresholds=thresholds, speeds=[5000] * (len(thresholds) + 1))
            self.c.command("apply", {"config": config})
            self.c.command("save")
            other = Controller(self.folder.name, simulation=True, autostart=False)
            try:
                self.assertEqual(other.config["thresholds"], thresholds)
                self.assertEqual(other.config["speeds"], config["speeds"])
                self.assertEqual(other.config["direction2"], 1)
                self.assertFalse(other.connected)
            finally:
                other.close()

    def test_rule_validation_is_atomic_and_does_not_share_mutable_lists(self):
        invalid = [dict(thresholds=[876, 820]), dict(thresholds=[820, 820]), dict(thresholds=[820, 840]),
                   dict(thresholds=[0]), dict(thresholds=[10000]), dict(speeds=[1000]),
                   dict(speeds=[12000, 9000, 6000, 35001]), dict(thresholds=[820.5]),
                   dict(thresholds=[True]), dict(thresholds=list(range(100, 3400, 100)), speeds=[1000] * 34)]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ControlError):
                self.c.command("apply", {"config": dict(self.config, **change)})
            self.assertEqual(self.c.config, self.config)
        self.config["thresholds"][0] = 700
        self.assertEqual(self.c.config["thresholds"][0], 820)
        snapshot = self.c.snapshot()
        snapshot["config"]["speeds"][0] = 1
        self.assertEqual(self.c.config["speeds"][0], 12000)

    def test_legacy_config_retained_and_converted_by_ui_contract(self):
        old = {key: value for key, value in DEFAULTS.items()
               if key not in ("thresholds", "speeds", "dual_pump", "pump2", "pump2_baud", "pump2_port", "direction2")}
        old.update(t1=820, t2=876, speed_mode=1, r0=10000, r1=6000, r2=3000)
        (Path(self.folder.name) / "config.json").write_text(json.dumps(dict(schema="pump-pc-v1", config=old)))
        other = Controller(self.folder.name, simulation=True, autostart=False)
        try:
            self.assertEqual((other.config["t1"], other.config["t2"]), (820, 876))
            self.assertEqual(other.config["r1"], 6000)
            self.assertEqual(other.config["thresholds"], [])
            self.assertFalse(other.config["dual_pump"])
        finally:
            other.close()

    def test_either_pump_alone_can_start_update_and_stop_manually(self):
        for key in ("pump", "pump2"):
            with self.subTest(channel=key):
                self.c.connect_channel(key)
                self.advance()
                self.c.command("manual", {"rpm_x100": 5000})
                self.advance()
                plant = self.c.plant.second if key == "pump2" else self.c.plant
                self.assertEqual((self.c.state, plant.rpm), ("MANUAL", 5000))
                self.assertEqual(plant.status & 3, 3 if key == "pump2" else 1)
                self.c.command("set_speed", {"rpm_x100": 7550})
                self.advance()
                self.assertEqual(plant.rpm, 7550)
                self.assertTrue(self.c.snapshot()["t"]["pumps"][key]["valid"])
                self.c.command("stop")
                self.advance(2)
                self.assertEqual(self.c.state, "IDLE")
                self.assertFalse(plant.status & 1)
                self.c.disconnect_channel(key)
                self.assertEqual(self.c.state, "DISCONNECTED")

    def test_auto_requires_both_pumps_even_with_legacy_single_config(self):
        self.c.command("apply", {"config": dict(self.config, dual_pump=0)})
        self.c.connect_channel("adc")
        self.c.connect_channel("pump2")
        self.advance()
        with self.assertRaisesRegex(ControlError, "PUMPS_REQUIRED"):
            self.c.command("start")
        self.assertEqual(self.c.plant.second.writes, [])

    def test_unconnected_pump_does_not_receive_any_manual_commands(self):
        self.c.connect_channel("pump2")
        self.advance()
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance()
        self.assertEqual(self.c.plant.writes, [])
        with self.assertRaisesRegex(ControlError, "STOP_AND_WAIT"):
            self.c.connect_channel("pump")
        self.c.command("stop")
        self.advance(2)
        self.c.connect_channel("pump")
        self.advance()
        self.c.command("manual", {"rpm_x100": 6000})
        self.advance()
        self.assert_rpm(6000)

    def test_shorten_rules_with_adc_connected_then_run_second_pump_only(self):
        self.c.plant.voltage_mv = 1300
        self.start(auto=True)
        self.assertEqual(self.c.stage, 3)
        self.c.command("stop")
        self.advance(2)
        self.c.disconnect_channel("pump")
        self.c.command("apply", {"config": dict(self.config, thresholds=[820], speeds=[12000, 9000])})
        self.advance()
        self.c.command("manual", {"rpm_x100": 4250})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.plant.second.rpm), ("MANUAL", 4250))
        self.assertLessEqual(self.c.stage, 1)

    def test_external_run_before_second_connection_is_not_taken_over(self):
        self.c.plant.update({"external_run": True})
        self.c.connect_channel("pump")
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "EXTERNAL_RUN"))
        self.assertEqual(self.c.plant.writes, [])
        self.c.connect_channel("pump2")
        self.advance()
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "EXTERNAL_RUN"))
        self.assertEqual(self.c.plant.writes, [])
        self.assertEqual(self.c.plant.second.writes, [])
        self.c.command("stop")
        self.advance(2)
        self.c.command("reset")
        self.assertEqual(self.c.state, "IDLE")

    def test_connecting_second_pump_does_not_clear_existing_alarm(self):
        self.c.plant.update({"stall": True})
        self.c.connect_channel("pump")
        self.advance()
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "STALL"))
        self.c.connect_channel("pump2")
        self.advance()
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "STALL"))
        self.c.plant.update({"stall": False})
        self.advance()
        self.c.command("reset")
        self.assertEqual(self.c.state, "IDLE")

    def test_same_port_and_communication_changes_require_disconnect(self):
        self.c.connect_channel("pump")
        self.advance()
        with self.assertRaisesRegex(ControlError, "SAME_PORT"):
            self.c.connect_channel("pump2", dict(pump2_port="sim-pump", pump2=1, pump2_baud=115200))
        with self.assertRaisesRegex(ControlError, "DISCONNECT_FIRST"):
            self.c.command("apply", {"config": dict(self.config, baud=9600)})

    def test_run_operations_are_sent_to_ports_in_parallel(self):
        self.connect()
        entered = {key: threading.Event() for key in ("pump", "pump2")}
        for key, device in self.c.devices.items():
            original = device.call
            def call(op, value, config, key=key, original=original):
                if op == "run":
                    entered[key].set()
                    peer = "pump2" if key == "pump" else "pump"
                    if not entered[peer].wait(1):
                        raise OSError("Ports were not dispatched concurrently")
                return original(op, value, config)
            device.call = call
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance()
        self.assertEqual(self.c.state, "MANUAL")
        self.assertTrue(all(event.is_set() for event in entered.values()))

    def test_failed_start_ack_stops_both_and_latches(self):
        self.connect()
        original = self.c.devices["pump2"].call
        def call(op, value, config):
            result = original(op, value, config)
            if op == "run":
                raise OSError("Lost run reply")
            return result
        self.c.devices["pump2"].call = call
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "PUMP_COMM"))
        self.assertFalse(self.c.plant.status & 1 or self.c.plant.second.status & 1)
        self.assertIn("stop", [op for op, _ in self.c.plant.writes])
        self.assertIn("stop", [op for op, _ in self.c.plant.second.writes])

    def test_offline_pump_does_not_prevent_healthy_pump_stop(self):
        self.start()
        self.c.plant.update({"pump2_online": False})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "PUMP_COMM"))
        self.assertFalse(self.c.plant.status & 1)
        self.assertTrue(self.c.plant.second.status & 1)
        self.assertTrue(self.c.stop_required)
        self.assertFalse(self.c.snapshot()["t"]["pumps"]["pump2"]["valid"])
        with self.assertRaisesRegex(ControlError, "FAULT_NOT_CLEARED"):
            self.c.command("reset")
        self.c.plant.update({"pump2_online": True})
        self.advance(2)
        self.assertFalse(self.c.plant.second.status & 1)
        self.c.command("reset")
        self.assertEqual(self.c.state, "IDLE")

    def test_stop_waits_for_both_physical_status_replies(self):
        self.start()
        original = self.c.devices["pump2"].call
        def call(op, value, config):
            return None if op == "stop" else original(op, value, config)
        self.c.devices["pump2"].call = call
        self.c.command("stop")
        self.advance(2)
        self.assertEqual(self.c.state, "STOPPING")
        self.assertTrue(self.c.stop_required)
        self.assertFalse(self.c.plant.status & 1)
        self.assertTrue(self.c.plant.second.status & 1)
        self.c.devices["pump2"].call = original
        self.advance(2)
        self.assertEqual(self.c.state, "IDLE")

    def test_alarm_and_unexpected_stop_on_second_pump_stop_both(self):
        self.start()
        self.c.plant.update({"pump2_stall": True})
        self.advance(2)
        self.assertEqual(self.c.fault, "STALL")
        self.assertFalse(self.c.plant.status & 1 or self.c.plant.second.status & 1)
        self.c.plant.update({"pump2_stall": False})
        self.advance()
        self.c.command("reset")
        self.c.command("manual", {"rpm_x100": 5000})
        self.advance()
        self.c.plant.update({"pump2_external_run": False})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("FAULT", "PUMP_MISMATCH"))
        self.assertFalse(self.c.plant.status & 1)

    def test_adc_loss_holds_both_and_requires_manual_resume(self):
        self.start(auto=True)
        self.c.plant.update({"adc_online": False})
        self.advance(2)
        self.assertEqual((self.c.state, self.c.fault), ("HOLD", "VOLTAGE"))
        held = (self.c.plant.rpm, self.c.plant.second.rpm)
        self.advance(2)
        self.assertEqual((self.c.plant.rpm, self.c.plant.second.rpm), held)
        self.c.plant.update({"adc_online": True, "voltage_mv": 700})
        self.advance(3)
        self.assertEqual(self.c.state, "HOLD")
        self.c.command("resume")
        self.advance(2)
        self.assertEqual(self.c.state, "RUN")
        self.assert_rpm(12000)

    def test_stop_works_when_only_second_port_is_connected(self):
        self.c.connect_channel("pump2")
        self.advance()
        self.c.plant.update({"pump2_external_run": True})
        self.advance()
        self.c.command("stop")
        self.advance(2)
        self.assertFalse(self.c.plant.second.status & 1)

    def test_running_pumps_cannot_be_removed_or_rules_changed(self):
        self.start()
        for key in ("pump", "pump2"):
            with self.assertRaisesRegex(ControlError, "STOP_AND_WAIT"):
                self.c.disconnect_channel(key)
        with self.assertRaisesRegex(ControlError, "STOP_AND_WAIT"):
            self.c.command("apply", {"config": self.config})

    def test_second_port_uses_its_own_modbus_address_baud_and_direction(self):
        import minimalmodbus
        minimalmodbus._serialports.clear()
        with mock.patch("minimalmodbus.serial.Serial", side_effect=lambda **kw: WireSerial(**kw)):
            devices = {key: ModbusDevice("WIRE-" + key, 1, 115200) for key in ("pump", "pump2")}
            try:
                config = dict(self.config, pump2=7, pump2_baud=19200)
                replies = self.c._pump_call(devices, "direction", None, config)
                self.assertTrue(all(not reply["error"] for reply in replies.values()))
                first = devices["pump"].instrument.serial
                second = devices["pump2"].instrument.serial
                self.assertEqual(first.frames[-1][:6], bytes.fromhex("010500020000"))
                self.assertEqual(second.frames[-1][:6], bytes.fromhex("07050002ff00"))
                self.assertEqual(second.baudrate, 19200)
            finally:
                for device in devices.values():
                    device.close()
                minimalmodbus._serialports.clear()
