"""Opt-in native infrastructure probe; its synthetic PNG grants no visual approval."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
from typing import Any
import uuid

# These fixed programs execute only inside the reviewed containers. No package,
# build backend, Git config/hook, or candidate helper is imported/run on the host.
INSTALL = r'''
import errno, hashlib, json, os, pathlib, socket, subprocess, sys
settings=json.loads(sys.argv[1]);root=pathlib.Path('/work');out=root/'probe-evidence';out.mkdir()
assert list(sys.version_info[:2])==settings['python']
status=dict(line.split(':',1)for line in pathlib.Path('/proc/self/status').read_text().splitlines()if':'in line)
assert os.getuid()!=0 and int(status['CapEff'].strip(),16)==0 and status['NoNewPrivs'].strip()=='1'
assert not any(name in os.environ for name in ['GITHUB_TOKEN','GH_TOKEN','ACTIONS_RUNTIME_TOKEN','GITHUB_OUTPUT','GITHUB_ENV','GITHUB_WORKSPACE','RUNNER_TEMP'])
assert not pathlib.Path('/var/run/docker.sock').exists()
for name in ['/etc/atk-native-write-probe','/trusted/.atk-native-write-probe']:
 try:pathlib.Path(name).write_text('must be read-only')
 except OSError as error:assert error.errno in [errno.EROFS,errno.EACCES]
 else:raise AssertionError('Writable root/trusted mount')
pathlib.Path('/tmp/native-write-probe').write_text('tmpfs works')
subprocess.run(['git','init','/work'],check=True)
(root/'.gitignore').write_text('.venv/\n')
subprocess.run([sys.executable,'-m','venv','/work/.venv'],check=True)
assert '.venv' not in subprocess.check_output(['git','status','--porcelain'],text=True)
subprocess.run(['/work/.venv/bin/python','-m','pip','install','Pillow>=10,<13','resvg-py>=0.5,<0.6'],check=True)
refused={}
for address in [settings['gateway'],'169.254.169.254','10.23.45.67','100.64.0.1','172.16.0.1','192.168.0.1','198.18.0.1']:
 try:
  connection=socket.create_connection((address,settings['port'] if address==settings['gateway']else 80),timeout=2)
 except OSError as error:refused[address]={'type':type(error).__name__,'errno':error.errno}
 else:connection.close();raise AssertionError('Private/host egress succeeded: '+address)
ipv6_refused={}
for address in ['2606:4700:4700::1111','fe80::1','fc00::1','::ffff:169.254.169.254','::ffff:'+settings['gateway']]:
 try:
  with socket.socket(socket.AF_INET6,socket.SOCK_STREAM)as connection:
   connection.settimeout(2);connection.connect((address,80))
 except OSError as error:ipv6_refused[address]={'type':type(error).__name__,'errno':error.errno,'reason':str(error)}
 else:raise AssertionError('Installer IPv6 egress succeeded: '+address)
network_facts={name:pathlib.Path('/proc/net/'+name).read_text()if pathlib.Path('/proc/net/'+name).exists()else None for name in ['if_inet6','ipv6_route','route']}
report={'python':sys.version,'uid':os.getuid(),'gid':os.getgid(),'cap_effective':status['CapEff'].strip(),'no_new_privileges':status['NoNewPrivs'].strip(),'git':subprocess.check_output(['git','--version'],text=True).strip(),'private_refusals':refused,'installer_ipv6_refusals':ipv6_refused,'network_facts':network_facts,'ipv6_limit':'A missing address/route or disabled protocol is not proof that a firewall rule was exercised. Only recorded owned-rule counter deltas establish native rule hits.','public_pip_install':True}
(out/'install.json').write_text(json.dumps(report,indent=2))
'''

CAPTURE = r'''
import hashlib,io,json,os,pathlib,socket,sys
import PIL,resvg_py
from PIL import Image,ImageStat
settings=json.loads(sys.argv[1]);out=pathlib.Path('/work/probe-evidence')
assert sorted(p.name for p in pathlib.Path('/sys/class/net').iterdir())==['lo']
for family,address in [(socket.AF_INET,'1.1.1.1'),(socket.AF_INET,settings['gateway']),(socket.AF_INET6,'2606:4700:4700::1111'),(socket.AF_INET6,'::ffff:169.254.169.254')]:
 try:
  with socket.socket(family,socket.SOCK_STREAM)as connection:
   connection.settimeout(2);connection.connect((address,443))
 except OSError:pass
 else:raise AssertionError('network-none escape: '+address)
fonts=sorted(p for p in pathlib.Path('/usr/share/fonts').rglob('*')if p.is_file()and p.suffix.lower()in{'.ttf','.otf'})
assert fonts,'No system fonts available for actual glyph rendering'
svg='<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800"><rect width="100%" height="100%" fill="black"/><text x="20" y="50" font-family="monospace" font-size="32" fill="white">Probe glyphs Ag09</text></svg>'
png=resvg_py.svg_to_bytes(svg_string=svg)
with Image.open(io.BytesIO(png))as image:
 assert image.size==(1280,800)
 assert any(high>0 for low,high in ImageStat.Stat(image.convert('RGB')).extrema),'Glyphs were not rendered'
(out/'infrastructure-only.png').write_bytes(png)
(out/'capture.json').write_text(json.dumps({'network':'none','interfaces':['lo'],'synthetic_infrastructure_only':True,'visual_approval':False,'pillow':PIL.__version__,'python':sys.version,'font_files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest()for p in fonts},'png_sha256':hashlib.sha256(png).hexdigest()},indent=2))
'''


def owned_counters(module: Any, sandbox: Any) -> dict[str, int]:
    raw = module.command(["sudo", "-n", "iptables-save", "-c", "-t", "filter"])
    result = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"\[(\d+):\d+\] (.*)", line)
        if match and (match[2].startswith("-A " + sandbox.chain + " ") or
                      " -i " + sandbox.bridge + " " in match[2]):
            result[match[2]] = int(match[1])
    return result


def main() -> None:
    trusted = Path(__file__).resolve().parents[2]
    path = trusted / ".github/agent/capture_isolation.py"
    spec = importlib.util.spec_from_file_location("fixed_native_isolation_transport", path)
    if spec is None or spec.loader is None:
        raise ValueError("Trusted probe transport missing")
    isolation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolation)
    module = isolation.trusted_module("capture_sandbox")
    artifact = isolation.trusted_module("capture_artifact")
    version = os.environ["PROBE_PYTHON"]
    if version not in module.IMAGES:
        raise ValueError("Unsupported native probe Python line")
    output = Path(os.environ["RUNNER_TEMP"]) / ("atk-native-probe-" + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    logs = output / "isolation-diagnostics"
    logs.mkdir(mode=0o700)
    candidate = output.parent / ("atk-native-synthetic-" + uuid.uuid4().hex)
    candidate.mkdir(mode=0o700)
    sandbox = module.Sandbox(candidate, trusted, logs / "ownership.json", version)
    report: dict[str, Any] = {"kind": "native-container-infrastructure-probe",
        "visual_approval": False, "ok": False, "python_line": version,
        "source_head": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "phases": [],
        "source_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (path, Path(__file__), path.with_name("capture_sandbox.py"),
                                   path.with_name("capture_artifact.py"))}}
    identity = {"candidate": os.environ["GITHUB_SHA"], "repository": os.environ["GITHUB_REPOSITORY"],
                "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    # Positive control proves the credential-free synthetic host endpoint is listening.
    with socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2):
        accepted, _address = listener.accept()
        accepted.close()

    def interrupted(_number: int, _frame: Any) -> None:
        raise TimeoutError("Native infrastructure probe interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(15 * 60)
    try:
        sandbox.setup()
        report["image"] = sandbox.image
        network = json.loads(module.command(["docker", "network", "inspect", sandbox.network_id]))[0]
        settings = {"gateway": network["IPAM"]["Config"][0]["Gateway"],
                    "port": listener.getsockname()[1], "python": [int(x) for x in version.split(".")]}
        before = owned_counters(module, sandbox)
        for phase, program in (("probe", INSTALL), ("capture", CAPTURE)):
            python = "python" if phase == "probe" else "/work/.venv/bin/python"
            container = sandbox.create(phase, [python, "-c", program, json.dumps(settings)], identity)
            sandbox.verify()
            sandbox.validate_container(container)
            phase_logs = logs / (phase + "-streams")
            phase_logs.mkdir(mode=0o700)
            outcome = isolation.contained_output(container, phase, phase_logs, 360,
                                                  quarantine_root=output.parent)
            outcome["stream_directory"] = phase + "-streams"
            report["phases"].append(outcome)
            sandbox.remove(container)
            if not outcome["ok"]:
                raise ValueError("Native contained " + phase + " failed")
        after = owned_counters(module, sandbox)
        input_rules = [key for key in after if key.startswith("-A INPUT ")]
        metadata_rules = [key for key in after if " -d 169.254.0.0/16 " in key]
        if not input_rules or not metadata_rules or any(
            after[key] <= before.get(key, 0) for key in [*input_rules, *metadata_rules]
        ):
            raise ValueError("Host/metadata refusals did not hit the owned native firewall guards")
        try:
            unexpected, _address = listener.accept()
        except TimeoutError:
            pass
        else:
            unexpected.close()
            raise ValueError("Contained probe reached synthetic host endpoint")
        report.update(ok=True, firewall_before=before, firewall_after=after)
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        signal.alarm(90)
        try:
            sandbox.cleanup()
            report["cleanup_complete"] = True
        except (Exception, KeyboardInterrupt) as exc:
            report.update(ok=False, cleanup_complete=False, cleanup_error=str(exc))
        listener.close()
        signal.alarm(0)
    report["uncertain_operations"] = sandbox.uncertain_operations()
    if report["ok"] and not sandbox.containers and not report["uncertain_operations"]:
        try:
            isolation.collect(candidate / "probe-evidence", output / "probe-evidence", artifact)
        except (Exception, KeyboardInterrupt) as exc:
            report.update(ok=False, collection_error=type(exc).__name__ + ": " + str(exc))
    (logs / "native-probe.json").write_text(json.dumps(report, indent=2) + "\n")
    if not isolation.UNSAFE_LOG_WRITER.is_set():
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("artifact_root=" + str(output) + "\n")
    if not report["ok"]:
        raise SystemExit("Native infrastructure probe failed; no activation or visual approval")


if __name__ == "__main__":
    main()
