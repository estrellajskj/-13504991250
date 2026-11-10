"""Exercise the extracted Windows EXE, without Python in PATH or physical I/O."""
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener, ProxyHandler
import json
import os
import socket
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "output/release/水泵控制-Windows-x64-V4.zip"


def main():
    http = build_opener(ProxyHandler({}))
    with TemporaryDirectory(prefix="PumpRelease_独立测试 ") as temporary:
        folder = Path(temporary)
        with zipfile.ZipFile(ARCHIVE) as bundle:
            assert bundle.testzip() is None
            assert not any("/output/" in name or name.endswith((".csv", "config.json")) for name in bundle.namelist())
            bundle.extractall(folder)
        package = folder / "水泵控制-Windows-x64-V4"
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PATH")}
        windows = Path(next((v for k, v in env.items() if k.upper() == "SYSTEMROOT"), "C:/Windows"))
        env["PATH"] = str(windows / "System32")
        checks = []
        for index, simulation in enumerate((True, True, False)):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = "http://127.0.0.1:" + str(port)
            args = [str(package / "PumpControl.exe"), "--no-open", "--port", str(port)]
            if simulation:
                args.append("--simulate")
            log_path = folder / (str(port) + ".log")
            with log_path.open("wb") as log:
                process = subprocess.Popen(args, cwd=folder, env=env, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                def get(path):
                    with http.open(base + path, timeout=3) as response:
                        return json.load(response)

                def wait(predicate, timeout=12):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        state = get("/api/state?since=2147483647")
                        if predicate(state):
                            return state
                        time.sleep(0.1)
                    raise AssertionError(state["t"])

                deadline = time.monotonic() + 25
                while True:
                    try:
                        info = get("/api/info")
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError(log_path.read_text(errors="replace"))
                        time.sleep(0.2)
                assert info["version"] == 4 and info["simulation"] is simulation
                for asset in ("pc-control.html", "pc-control.css", "pc-control.js"):
                    with http.open(base + "/" + asset, timeout=3) as response:
                        assert response.read() == (ROOT / asset).read_bytes()
                state = get("/api/state?since=2147483647")
                assert not state["connected"] and not any(state["channels"].values())
                ports = get("/api/ports")["ports"]
                if not simulation:
                    assert isinstance(ports, list)
                    assert not state["config"]["pump_port"] and not state["config"]["adc_port"]
                    checks.append("real-mode COM enumeration; no hardware opened")
                    continue
                assert {p["device"] for p in ports} == {"SIM-PUMP", "SIM-PUMP2", "SIM-BZY"}
                if index == 1:
                    assert state["config"]["thresholds"] == [820, 876, 1200]
                    checks.append("three-decimal rules persist after EXE restart")
                    continue
                token = state["token"]
                def command(payload):
                    request = Request(base + "/api/command", data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json", "X-Control-Token": token})
                    with http.open(request, timeout=5) as response:
                        return json.load(response)

                config = dict(state["config"], dual_pump=0, speed_mode=1, direction2=1,
                              pump_port="SIM-PUMP", pump2_port="SIM-PUMP2", adc_port="SIM-BZY",
                              vmin=0, vmax=10000, thresholds=[820, 876, 1200], speeds=[12000, 9000, 6000, 3000])
                command(dict(action="apply", config=config))
                command(dict(action="save"))
                saved = package / "output/pc-control-sim/config.json"
                assert json.loads(saved.read_text())["schema"] == "pump-pc-v2"
                assert not (folder / "output").exists()
                for only in ("pump", "pump2"):
                    command(dict(action="connect_channel", channel=only))
                    wait(lambda s: s["t"]["pump_valid"])
                    command(dict(action="manual", rpm_x100=4250))
                    wait(lambda s: s["t"]["state"] == "MANUAL" and s["t"]["pumps"][only]["rpm_x100"] == 4250)
                    command(dict(action="set_speed", rpm_x100=6525))
                    wait(lambda s: s["t"]["pumps"][only]["rpm_x100"] == 6525)
                    command(dict(action="stop"))
                    wait(lambda s: s["t"]["state"] == "IDLE" and not s["t"]["stop_required"])
                    command(dict(action="disconnect_channel", channel=only))
                checks.append("either pump alone can start, update speed and stop without mode selection")
                for channel in ("pump", "pump2", "adc"):
                    command(dict(action="connect_channel", channel=channel))
                wait(lambda s: s["t"]["pump_valid"] and s["t"]["voltage_valid"])
                command(dict(action="start"))
                state = wait(lambda s: s["t"]["state"] == "RUN")
                assert state["t"]["pumps"]["pump"]["status"] & 3 == 1
                assert state["t"]["pumps"]["pump2"]["status"] & 3 == 3
                command(dict(action="stop"))
                wait(lambda s: s["t"]["state"] == "IDLE" and not s["t"]["status"] & 1)
                for channel in ("pump", "pump2", "adc"):
                    command(dict(action="disconnect_channel", channel=channel))
                checks.append("extracted EXE, dual auto start/stop, separate directions, portable settings")
            finally:
                subprocess.run([str(windows / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
                process.wait(timeout=10)
        print(json.dumps(dict(result="PASS", checks=checks, zip_bytes=ARCHIVE.stat().st_size)))


if __name__ == "__main__":
    main()
