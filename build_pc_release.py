"""Build a Windows x64 release without copying local settings or recordings."""
from importlib import metadata
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "release"
BUILD = ROOT / "output" / "build-windows"
PACKAGE_NAME = "水泵控制-Windows-x64-V4"

GUIDE = """水泵控制 V4：统一双泵页面、任意一台可手动控制（Windows 10/11，64位）

一、收到后怎么打开
1. 右键解压整个ZIP压缩包到桌面或文档目录，不要直接在压缩包里运行。
2. 双击“启动控制页.bat”，或直接双击 PumpControl.exe。
3. 程序会打开浏览器中的控制页面。保留后台黑色窗口。
   无需安装Python，也无需安装本软件的依赖。控制功能可离线使用。
4. 如果没有自动打开网页，复制后台窗口显示的 http://127.0.0.1:端口/pc-control.html 到浏览器。
   这是本机地址，不能把这个网址发给别人当成在线软件。

二、连接设备
只控制水泵：给水泵供电，插水泵的USB-RS485线，在正极泵或负极泵栏选择对应COM口后连接。
不再区分单泵/双泵模式，只接正极泵或只接负极泵都能手动控制。
不需要连接电压采集模块，也不需要第二个USB。
填写转速（rpm）和方向，点击“启动水泵”；运行中可更新转速，换方向前先停止。

电压控制：两台泵都连接正常后，另外给BZY模块供电，通过第三根USB-RS485线接电脑，再连接采集。
在电压联动区域设置电压阈值和每档转速，保存规则后启动电压联动。
点击“添加阈值”增加阈值；每个阈值右侧的叉号用于删除该阈值。
可以保留1～32个阈值，对应2～33档转速。阈值最多三位小数，例如0.82V或0.876V。
阈值从小到大填写，相邻至少间隔0.021V；首个阈值须大于0.020V。
电压有滤波和500ms确认，下降切换有0.020V回差；输入小数位不代表硬件测量精度。

双泵联动：页面始终显示正极泵、负极泵两个连接栏，可根据需要连接一台或两台。
两台泵各自供电、各接一根USB-RS485线；电压联动时另接一根线连接BZY。
总共三个不同COM口。两台泵的地址可以都为1，因为使用独立串口。
两台泵均连接正常并处于待机后，可手动同步控制，不需要BZY。
接好BZY后可使用同一套电压规则控制两台泵，两台泵目标转速相同，方向可分别设置。
手动“启动水泵”“更新转速”“停止水泵”作用于当前所有已连接的泵，分别显示已应答转速。
已连接但状态异常的泵不会被跳过；运行中增加或断开水泵前先停止并确认停机。
电压自动联动仍要求两台泵和BZY都连接正常，少连一台泵时禁止启动自动联动。
两路串口并行下发，不保证物理泵严格同时动作；相同转速不保证实际流量相同。
任一泵报警、运行状态异常或通信失败，会向两台都请求停止。
只有两台都应答并报告停止，才确认停机；断线的泵仍可能继续运行，需要现场确认。
BZY采集失效时保持两台最后已确认转速；恢复采集后需要手动点击“恢复联动”。
BZY使用0～10V型号，第1路电压输入V+/V-，原始满量程65535。
水泵默认115200、地址1；BZY默认9600、地址1，以实际设备参数为准。
换电脑后COM编号可能改变，需要重新选择。每台设备使用不同COM口。
默认规则只是示例，请按自己的实验要求修改。

三、找不到COM口怎么办
在Windows设备管理器的“端口(COM和LPT)”下确认USB-RS485设备。
若显示未知设备或黄色感叹号，请按转换器的实际芯片型号安装厂家驱动。
关闭原水泵上位机和串口助手，避免占用同一个COM口。
本包不附带不明型号的USB驱动。

四、保存与退出
规则和采集记录保存在程序旁的output目录，首次使用会自动创建。
本发行包不包含发送者的配置、COM选择和历史采集记录。
不要放到Program Files等普通用户无写权限的目录。
先停止充放电和水泵，确认现场已停，再断开设备并关闭后台窗口。
关闭网页不等于停泵；通信断开或后台退出时，物理泵可能维持最后转速。
自动控制期间电脑应保持开机、不休眠，后台窗口保持运行。

五、适用范围
本版本使用Kamoer M2-KK360水泵与BZY 0～10V RS485采集模块。
适用于Windows 10/11 x64电脑；不能直接在手机或macOS上运行这个EXE。
第三方软件许可见licenses目录。后台API版本4，配置兼容旧版并保存为pump-pc-v2。
"""


def write_licenses(target):
    target.mkdir(parents=True, exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(python_license)
    shutil.copy2(python_license, target / "Python-LICENSE.txt")
    for package in ("minimalmodbus", "pyserial"):
        dist = metadata.distribution(package)
        found = []
        for item in dist.files or []:
            if (".dist-info" in str(item) or ".egg-info" in str(item)) and any(
                    word in item.name.upper() for word in ("LICENSE", "COPYING", "NOTICE")):
                path = Path(dist.locate_file(item))
                if path.is_file():
                    shutil.copy2(path, target / (package + "-" + item.name))
                    found.append(str(item))
        if not found:
            # The pyserial 3.5 wheel omits its license; keep the upstream text.
            fallback = ROOT / "licenses" / (package + "-LICENSE.txt")
            if not fallback.is_file():
                raise RuntimeError("Missing third-party license: " + package)
            shutil.copy2(fallback, target / fallback.name)
    (target / "ABOUT.txt").write_text(
        "Bundled components: Python " + platform.python_version() + "; "
        + "; ".join(name + " " + metadata.version(name) for name in ("minimalmodbus", "pyserial"))
        + ".\nBuilt with PyInstaller (bootloader distribution exception).\n", encoding="utf-8")


def main():
    if sys.platform != "win32" or platform.machine().upper() not in ("AMD64", "X86_64"):
        raise SystemExit("Build with 64-bit Python on Windows x64.")
    for asset in ("pc-control.html", "pc-control.css", "pc-control.js"):
        if not (ROOT / asset).is_file():
            raise FileNotFoundError(ROOT / asset)
    build_exe = BUILD / "bin"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onefile", "--console", "--noupx", "--name", "PumpControl",
               "--distpath", str(build_exe), "--workpath", str(BUILD / "work"),
               "--specpath", str(BUILD), "--hidden-import", "serial.tools.list_ports_windows"]
    for asset in ("pc-control.html", "pc-control.css", "pc-control.js"):
        command += ["--add-data", str(ROOT / asset) + ";."]
    command.append(str(ROOT / "pc_server.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    package = OUT / PACKAGE_NAME
    package.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build_exe / "PumpControl.exe", package / "PumpControl.exe")
    (package / "使用说明.txt").write_text(GUIDE, encoding="utf-8-sig")
    (package / "启动控制页.bat").write_bytes(
        b'@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n"%~dp0PumpControl.exe" --open %*\r\n'
        b'if errorlevel 1 pause\r\n')
    write_licenses(package / "licenses")
    # Use an explicit allowlist: never include output, personal config or records.
    files = [package / name for name in ("PumpControl.exe", "使用说明.txt", "启动控制页.bat")]
    files += sorted(path for path in (package / "licenses").iterdir() if path.is_file())
    archive = OUT / (PACKAGE_NAME + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(OUT))
    print("Release ZIP: " + str(archive), flush=True)


if __name__ == "__main__":
    main()
