#!/usr/bin/env python3
"""selftest_system_status.py — prove the system-status server reads machines correctly.

The server's failure mode is not a crash. It is a plausible wrong number: a
drive counted seven times, a battery that reports a mouse's charge, a process
list shifted by one field because a name had a space in it. Nothing downstream
can tell those from the truth, so the parsing has to be checked against
machines whose answers are known in advance.

Most of those machines cannot be the one running the test. A CI runner has no
battery, no failing disk, nothing overheating and no reboot pending. So each
case builds a synthetic /proc, /sys, /etc and /run in a temp directory, points
the server's roots at it, replaces the system tools it asks with canned
answers, and asserts on what the server reports -- including the cases that
must stay silent, so a check that fires on a healthy machine fails too.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR, so a
PR cannot edit the server and this test together to hide a change.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_system_status.py
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "system-status" / "server.py"

GiB = 1024**3

CASES = []
FAILURES = []
running = "?"


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


def load_server():
    spec = importlib.util.spec_from_file_location("system_status_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStatvfs:
    """Just the fields the server reads, in the units statvfs reports them."""

    def __init__(self, total_bytes, free_bytes, available_bytes, block=4096):
        self.f_frsize = block
        self.f_blocks = total_bytes // block
        self.f_bfree = free_bytes // block
        self.f_bavail = available_bytes // block


def write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


HEALTHY_MEMINFO = (
    "MemTotal:       16000000 kB\n"
    "MemFree:         2000000 kB\n"
    "MemAvailable:   10000000 kB\n"
    "Cached:          6000000 kB\n"
    "SwapTotal:       8000000 kB\n"
    "SwapFree:        8000000 kB\n"
)
QUIET_PRESSURE = (
    "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
)


@contextlib.contextmanager
def machine(*, commands=None, statvfs=None, kernel="7.0.0-test", tools=None):
    """A synthetic, healthy machine. Cases then break exactly one thing."""
    server = load_server()
    with tempfile.TemporaryDirectory(prefix="system-status-selftest-") as tmp:
        root = pathlib.Path(tmp)
        write(root, "proc/meminfo", HEALTHY_MEMINFO)
        write(root, "proc/pressure/memory", QUIET_PRESSURE)
        write(root, "proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n")
        write(root, "proc/uptime", "3725.50 1000.00\n")
        write(root, "proc/self/mounts", "/dev/sda1 / ext4 rw,relatime 0 0\n")
        write(root, "etc/os-release", 'ID=testos\nPRETTY_NAME="Test OS"\n')
        (root / "run").mkdir()
        (root / "sys" / "class" / "power_supply").mkdir(parents=True)
        # The running kernel's modules are present unless a case removes them.
        (root / "lib" / "modules" / kernel).mkdir(parents=True)

        server.PROC = root / "proc"
        server.SYS = root / "sys"
        server.ETC = root / "etc"
        server.RUN = root / "run"
        server.MODULE_DIRS = (root / "lib" / "modules",)
        server.PCI_IDS = (root / "pci.ids",)
        server._kernel_release = lambda: kernel

        canned = dict(commands or {})
        installed = set(tools if tools is not None else ["systemctl", "ip", "nmcli", "timedatectl", "loginctl"])
        canned.setdefault(("systemctl", "list-units"), "[]")
        canned.setdefault(("systemctl", "--user"), "[]")
        canned.setdefault(("nmcli", "-t", "-f", "CONNECTIVITY"), "full\n")
        canned.setdefault(("timedatectl",), "Timezone=UTC\nNTPSynchronized=yes\n")

        def fake_run(args, *, accept=(0,)):
            if args[0] not in installed:
                return None
            for prefix in sorted(canned, key=len, reverse=True):
                if tuple(args[: len(prefix)]) == prefix:
                    return canned[prefix]
            return None

        server._run = fake_run
        server._which = lambda name: f"/usr/bin/{name}" if name in installed else None
        volumes = statvfs or {"/": FakeStatvfs(500 * GiB, 300 * GiB, 280 * GiB)}
        server._statvfs = lambda path: volumes[path]
        yield server, root


def health(server):
    return server.tool_get_system_health({})


# --- disks ------------------------------------------------------------------


@case
def btrfs_subvolumes_are_one_drive_not_seven():
    mounts = "".join(
        f"/dev/nvme0n1p3 {m} btrfs rw,subvol={m} 0 0\n"
        for m in ("/home", "/", "/var/log", "/var/cache")
    ) + (
        "/dev/nvme0n1p1 /boot/efi vfat rw 0 0\n"
        "/dev/loop3 /snap/core/1 squashfs ro 0 0\n"
        "tmpfs /tmp tmpfs rw 0 0\n"
        "proc /proc proc rw 0 0\n"
    )
    # As on real btrfs, statvfs at any subvolume reports the whole filesystem.
    # Modelling only "/" would make a regression crash on the fake instead of
    # failing an assertion about what the server reports.
    drive = FakeStatvfs(440 * GiB, 220 * GiB, 220 * GiB)
    volumes = {m: drive for m in ("/", "/home", "/var/log", "/var/cache")}
    volumes["/boot/efi"] = FakeStatvfs(4 * GiB, 4 * GiB, 4 * GiB)
    with machine(statvfs=volumes) as (server, root):
        write(root, "proc/self/mounts", mounts)
        fs = server.tool_get_disks({})["filesystems"]
        check(len(fs) == 2, f"four btrfs subvolumes and an EFI partition are two drives (got {len(fs)})")
        total = sum(f["total_bytes"] for f in fs)
        check(total == 444 * GiB, f"summing the report gives the real 444 GiB, not {total / GiB:.0f} GiB")
        btrfs = fs[0]
        check(btrfs["device"] == "/dev/nvme0n1p3", "the root drive is listed first")
        check(sorted(btrfs["mountpoints"]) == ["/", "/home", "/var/cache", "/var/log"],
              "every subvolume mount point is kept, on the one entry")
        check(btrfs["total_bytes"] == 440 * GiB, "capacity is counted once, not four times")
        check("note" in btrfs, "btrfs free space is marked as an estimate")
        check(all(f["fstype"] not in ("squashfs", "tmpfs", "proc") for f in fs),
              "snaps, tmpfs and proc are not reported as storage")


@case
def percent_used_follows_df_not_raw_blocks():
    # 100 GiB with 10 GiB free to root, 5 GiB of that reserved for root: used is
    # 90 GiB and a user can reach 5, so df reports 90 / (90 + 5) = 94.7%.
    volumes = {"/": FakeStatvfs(100 * GiB, 10 * GiB, 5 * GiB)}
    with machine(statvfs=volumes) as (server, _):
        fs = server.tool_get_disks({})["filesystems"][0]
        check(fs["percent_used"] == 94.7, f"percent used is df's figure (got {fs['percent_used']})")
        check(fs["free_bytes"] == 5 * GiB, "free is what an unprivileged user can actually use")


@case
def nearly_full_disks_are_reported_by_severity():
    for total, free, severity, why in (
        (100, 0.5, "critical", "99.5% used"),
        (100, 8.0, "warning", "92% used"),
        # 88.75% used is under the percentage bar; the absolute rule catches it.
        (40, 4.5, "warning", "under 5 GiB free on a drive big enough to hold more"),
        (100, 30.0, None, "70% used"),
    ):
        volumes = {"/": FakeStatvfs(int(total * GiB), int(free * GiB), int(free * GiB))}
        with machine(statvfs=volumes) as (server, _):
            disk = [f for f in health(server)["findings"] if f["area"] == "disk"]
            got = disk[0]["severity"] if disk else None
            check(got == severity, f"{free} GiB free of {total} GiB ({why}) is {severity or 'fine'} (got {got})")


@case
def a_read_only_root_is_flagged():
    with machine() as (server, root):
        write(root, "proc/self/mounts", "/dev/sda1 / ext4 ro,relatime 0 0\n")
        messages = [f["message"] for f in health(server)["findings"]]
        check(any("read-only" in m for m in messages), "a root filesystem mounted read-only is reported")


# --- battery ----------------------------------------------------------------


def battery(root, name, **props):
    for key, value in props.items():
        write(root, f"sys/class/power_supply/{name}/{key}", f"{value}\n")


@case
def an_energy_reporting_battery_computes_time_and_health():
    with machine() as (server, root):
        battery(root, "BAT0", type="Battery", status="Discharging", capacity=60,
                energy_now=30_000_000, energy_full=45_000_000, energy_full_design=60_000_000,
                power_now=10_000_000, cycle_count=412)
        battery(root, "AC", type="Mains", online=0)
        info = server.tool_get_battery({})
        bat = info["batteries"][0]
        check(info["present"] is True, "the battery is found")
        check(info["on_ac_power"] is False, "the adapter is reported unplugged")
        check(bat["time_to_empty"] == "3h 0m", f"30 Wh at 10 W lasts three hours (got {bat['time_to_empty']})")
        check(bat["health_percent"] == 75.0, "45 of 60 Wh design capacity is 75% health")
        check(bat["draw_watts"] == 10.0, "power draw is reported in watts")


@case
def a_charge_reporting_battery_with_negative_current_still_works():
    # Some firmware reports charge (µAh) rather than energy, and signs current.
    with machine() as (server, root):
        battery(root, "BAT1", type="Battery", status="Charging", capacity=50,
                charge_now=2_000_000, charge_full=4_000_000, charge_full_design=4_000_000,
                current_now=-1_000_000)
        bat = server.tool_get_battery({})["batteries"][0]
        check(bat["time_to_full"] == "2h 0m", f"2 Ah short at 1 A is two hours (got {bat['time_to_full']})")


@case
def a_mouse_battery_is_not_the_system_battery():
    with machine() as (server, root):
        battery(root, "hidpp_battery_0", type="Battery", scope="Device", capacity=15,
                status="Discharging", model_name="Wireless Mouse")
        info = server.tool_get_battery({})
        check(info["present"] is False, "a peripheral battery does not make the machine battery-powered")
        check(info["peripheral_batteries"][0]["model"] == "Wireless Mouse", "it is reported separately")
        messages = [f["message"] for f in health(server)["findings"]]
        check(any("Wireless Mouse" in m for m in messages), "its low charge is mentioned, as info")


@case
def low_battery_is_reported_only_while_discharging():
    with machine() as (server, root):
        battery(root, "BAT0", type="Battery", status="Discharging", capacity=8,
                energy_now=4_000_000, energy_full=50_000_000, energy_full_design=50_000_000,
                power_now=8_000_000)
        findings = [f for f in health(server)["findings"] if f["area"] == "battery"]
        check(findings and findings[0]["severity"] == "critical", "8% and discharging is critical")
    with machine() as (server, root):
        battery(root, "BAT0", type="Battery", status="Charging", capacity=8,
                energy_now=4_000_000, energy_full=50_000_000, energy_full_design=50_000_000)
        findings = [f for f in health(server)["findings"] if f["area"] == "battery"]
        check(not findings, "8% while charging is not an emergency")


@case
def no_battery_is_an_answer_not_a_failure():
    with machine() as (server, _):
        info = server.tool_get_battery({})
        check(info == {"present": False, "on_ac_power": None, "batteries": [], "peripheral_batteries": []},
              "a desktop reports present: false")
        report = health(server)
        check("battery" in report["checked"], "the battery check still ran")
        check("battery: none present" in report["unavailable"], "and says why it found nothing")


# --- processes ---------------------------------------------------------------


def proc_stat(pid, name, ticks, rss_pages):
    # Fields 3..24 after "pid (comm) "; utime=14 and stime=15 split the ticks.
    fields = ["S", "1"] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 8 + [str(rss_pages)]
    return f"{pid} ({name}) " + " ".join(fields) + "\n"


@case
def a_process_name_with_spaces_and_parens_does_not_shift_fields():
    with machine() as (server, root):
        tricky = "evil) name (x"
        write(root, "proc/4242/stat", proc_stat(4242, tricky, 500, 256))
        write(root, "proc/4242/status", "Name:\tevil\nUid:\t0\t0\t0\t0\n")
        sample = server._process_sample()
        row = sample.get(4242)
        check(row is not None, "the process is read")
        check(row and row["name"] == tricky, f"its name is intact (got {row and row['name']!r})")
        check(row and row["ticks"] == 500, "CPU ticks come from the right field")
        check(row and row["rss_pages"] == 256, "resident pages come from the right field")


@case
def command_lines_are_withheld_unless_asked_for():
    with machine() as (server, root):
        write(root, "proc/4242/stat", proc_stat(4242, "mysql", 10, 10))
        write(root, "proc/4242/status", "Uid:\t0\t0\t0\t0\n")
        write(root, "proc/4242/cmdline", "mysql\0-pSECRET\0")
        server.os.sysconf = lambda name: 100 if name == "SC_CLK_TCK" else 4096
        quiet = server.tool_get_top_processes({"sample_seconds": 0.1})
        check(all("command" not in p for p in quiet["processes"]), "no command line by default")
        loud = server.tool_get_top_processes({"sample_seconds": 0.1, "include_command_line": True})
        rows = [p for p in loud["processes"] if p["pid"] == 4242]
        check(rows and rows[0]["command"] == "mysql -pSECRET", "the command line is there on request")


@case
def bad_process_arguments_are_refused_plainly():
    with machine() as (server, _):
        for args, text in (({"sort_by": "disk"}, "sort_by"), ({"include_command_line": "yes"}, "true or false")):
            result = server._call_tool("get_top_processes", args)
            check(result["isError"] and text in result["content"][0]["text"], f"{args} is rejected")


# --- network -----------------------------------------------------------------


@case
def an_ssid_containing_colons_is_split_correctly():
    with machine() as (server, _):
        fields = server._nmcli_fields(r"yes:Cafe\:Guest\:5G:72:WPA2:36")
        check(fields == ["yes", "Cafe:Guest:5G", "72", "WPA2", "36"], f"escaped colons stay in the SSID (got {fields})")


@case
def a_captive_portal_is_reported():
    with machine(commands={("nmcli", "-t", "-f", "CONNECTIVITY"): "portal\n"}) as (server, _):
        net = server.tool_get_network({})
        check(net["online"] is False and net["connectivity"] == "portal", "portal is not 'online'")
        messages = [f["message"] for f in health(server)["findings"]]
        check(any("captive portal" in m for m in messages), "and health says what to do")


@case
def missing_network_tools_degrade_to_unknown():
    with machine(tools=["systemctl"]) as (server, _):
        net = server.tool_get_network({})
        check(net["online"] is None and net["interfaces"] == [], "no ip/nmcli reads as unknown, not offline")


# --- sensors, GPUs, services, reboot ------------------------------------------


@case
def temperature_sentinels_are_ignored_and_crit_margins_apply():
    with machine() as (server, root):
        hw = "sys/class/hwmon/hwmon0"
        write(root, f"{hw}/name", "coretemp\n")
        for n, value, crit in ((1, 98_000, 100_000), (2, -273_000, None), (3, 255_000, None), (4, 50_000, 100_000)):
            write(root, f"{hw}/temp{n}_input", f"{value}\n")
            write(root, f"{hw}/temp{n}_label", f"Core {n}\n")
            if crit:
                write(root, f"{hw}/temp{n}_crit", f"{crit}\n")
        temps = server.tool_get_temperatures({})
        labels = [s["label"] for s in temps["sensors"]]
        check(labels == ["Core 1", "Core 4"], f"-273 and 255 placeholders are dropped (kept {labels})")
        check(temps["hottest"]["celsius"] == 98.0, "the hottest reading is the real one")
        findings = [f for f in health(server)["findings"] if f["area"] == "temperature"]
        check(len(findings) == 1 and findings[0]["severity"] == "warning",
              "98°C against a 100°C critical point is one warning")


@case
def an_undriven_gpu_is_still_listed_and_named():
    with machine() as (server, root):
        dev = "sys/bus/pci/devices/0000:01:00.0"
        write(root, f"{dev}/class", "0x030200\n")
        write(root, f"{dev}/vendor", "0x10de\n")
        write(root, f"{dev}/device", "0x1c8d\n")
        write(root, "pci.ids", "10de  NVIDIA Corporation\n\t1c8d  GP107M [GeForce GTX 1050 Mobile]\n")
        gpus = server._gpus()
        check(len(gpus) == 1, "a GPU with no driver bound is found on the PCI bus")
        check(gpus[0]["driver"] is None, "and is reported as having no driver")
        check(gpus[0]["model"] == "GP107M [GeForce GTX 1050 Mobile]", "its name comes from pci.ids")


@case
def failed_units_carry_the_command_to_investigate():
    units = json.dumps([{"unit": "nvidia-persistenced.service", "sub": "failed",
                         "description": "NVIDIA Persistence Daemon"}])
    with machine(commands={("systemctl", "list-units"): units}) as (server, _):
        failed = server.tool_get_failed_services({})
        check([u["unit"] for u in failed["system"]] == ["nvidia-persistenced.service"], "the failed unit is listed")
        check("journalctl -u nvidia-persistenced.service" in failed["system"][0]["investigate"],
              "with the command to read its log")
        report = health(server)
        check(report["status"] == "warning", "a failed service makes the machine 'warning'")


@case
def a_replaced_kernel_and_the_debian_flag_both_mean_reboot():
    with machine(kernel="7.0.0-test") as (server, root):
        (root / "lib" / "modules" / "7.0.0-test").rmdir()
        (root / "lib" / "modules" / "7.0.1-test").mkdir()
        write(root, "run/reboot-required", "")
        write(root, "run/reboot-required.pkgs", "linux-image libc6\n")
        reasons = server._reboot_required()["reasons"]
        check(len(reasons) == 2, "both reasons are reported")
        check(any(r.get("installed_kernels") == ["7.0.1-test"] for r in reasons), "the new kernel is named")
        check(any(r.get("packages") == ["linux-image", "libc6"] for r in reasons), "the flagging packages are listed")


# --- memory, cpu, identity --------------------------------------------------------


@case
def memory_is_judged_by_available_and_stalls_not_by_used():
    with machine() as (server, root):
        # 95% "used" is mostly cache: plenty available, nothing to report.
        write(root, "proc/meminfo", HEALTHY_MEMINFO.replace("MemFree:         2000000", "MemFree:          800000"))
        check(not [f for f in health(server)["findings"] if f["area"] == "memory"], "cache-heavy RAM is not a finding")
    with machine() as (server, root):
        write(root, "proc/meminfo", HEALTHY_MEMINFO.replace("MemAvailable:   10000000", "MemAvailable:     500000"))
        write(root, "proc/pressure/memory", "some avg10=40.0 avg60=30.0 avg300=10.0 total=1\n"
                                             "full avg10=12.0 avg60=9.0 avg300=3.0 total=1\n")
        findings = [f for f in health(server)["findings"] if f["area"] == "memory"]
        check(any(f["severity"] == "critical" for f in findings), "3% available is critical")
        check(any("stalled" in f["message"] for f in findings), "and the stall time is reported too")


@case
def zram_use_is_not_mistaken_for_disk_swapping():
    with machine() as (server, root):
        write(root, "proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n"
                                  "/dev/zram0\tpartition\t8000000\t7000000\t100\n"
                                  "/dev/sda2\tpartition\t8000000\t6000000\t-1\n")
        devices = server.tool_get_memory({})["swap"]["devices"]
        check(devices[0]["kind"] == "zram", "zram is identified as zram")
        swap = [f["message"] for f in health(server)["findings"] if "swap" in f["message"]]
        check(len(swap) == 1 and "/dev/sda2" in swap[0], "only the disk swap is flagged")


@case
def disabled_boost_is_information_not_an_alarm():
    with machine() as (server, root):
        write(root, "sys/devices/system/cpu/intel_pstate/no_turbo", "1\n")
        write(root, "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "2200000\n")
        check(server._cpu_static()["boost_enabled"] is False, "no_turbo=1 reads as boost disabled")
        findings = [f for f in health(server)["findings"] if "boost" in f["message"]]
        check(len(findings) == 1 and findings[0]["severity"] == "info", "reported once, as info")
        check("2200 MHz" in findings[0]["message"], "naming the clock it is capped at")


@case
def the_package_manager_follows_the_distro():
    with machine(tools=["pacman", "apt"]) as (server, root):
        write(root, "etc/os-release", 'ID=cachyos\nID_LIKE="arch"\n')
        check(server._package_managers(server._os_info())["primary"] == "pacman",
              "an Arch derivative with apt also installed still uses pacman")
    with machine(tools=["apt"]) as (server, root):
        write(root, "etc/os-release", 'ID=someremix\nID_LIKE="ubuntu debian"\n')
        check(server._package_managers(server._os_info())["primary"] == "apt",
              "an unknown ID falls through to ID_LIKE")


@case
def a_healthy_machine_reports_nothing():
    with machine() as (server, _):
        report = health(server)
        check(report["status"] == "ok", f"status is ok (got {report['status']}: {report['findings']})")
        check(report["summary"] == "Nothing needs attention.", "and says so")
        check(len(report["checked"]) >= 9, "while showing every check that ran")


@case
def one_broken_check_does_not_hide_the_others():
    with machine() as (server, _):
        def explode():
            raise RuntimeError("sensor bus fell over")
        server._temperatures = explode
        report = health(server)
        check(any("sensor bus fell over" in u for u in report["unavailable"]), "the failure is reported")
        check("disks" in report["checked"] and "failed_services" in report["checked"],
              "and the other checks still ran")


@case
def every_tool_answers_over_json_rpc():
    with machine() as (server, _):
        server.os.sysconf = lambda name: 100 if name == "SC_CLK_TCK" else 4096
        for tool in server.TOOLS:
            args = {"sample_seconds": 0.1} if tool["name"] in ("get_cpu", "get_top_processes") else {}
            reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": tool["name"], "arguments": args}})
            result = reply["result"]
            ok = not result["isError"]
            if ok:
                json.loads(result["content"][0]["text"])
            check(ok, f"{tool['name']} returns a JSON document")


def main() -> int:
    global running
    if not SERVER.is_file():
        print(f"FAIL: {SERVER} not found")
        return 1

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        # A case that raises is a failure, not the end of the run: a crash in
        # one would otherwise hide every result after it.
        try:
            fn()
        except Exception as exc:
            check(False, f"raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: system-status self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
