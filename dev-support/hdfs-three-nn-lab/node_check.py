#!/usr/bin/env python3
"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

"""Authenticated checks on synthetic HDFS only; emit aggregate evidence."""
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HOSTS = ["nn1.geo.test", "nn2.geo.test", "nn3.geo.test"]
HDFS = "/usr/bigtop/current/hadoop-client/bin/hdfs"
DATA = b"Synthetic geo HDFS acceptance data.\n" * 32768
CHECKSUM = hashlib.sha256(DATA).hexdigest()
CONTROL = "/lab/control.bin"
EVIDENCE = Path("/var/tmp/geo-lab-evidence.json")


def run(argv, data=None, allowed=(0,)):
  result = subprocess.run(
    ["runuser", "-u", "hdfs", "--", *argv], input=data, capture_output=True, timeout=90
  )
  if result.returncode not in allowed:
    raise RuntimeError(
      "synthetic client command failed: "
      + repr(argv[:4])
      + ": "
      + result.stderr.decode(errors="replace")[-1800:]
    )
  return result


def login():
  if socket.getfqdn() not in HOSTS:
    raise RuntimeError("unexpected synthetic host")
  props = {
    p.findtext("name"): p.findtext("value")
    for p in ET.parse("/etc/hadoop/conf/core-site.xml").findall("property")
  }
  if (
    props.get("fs.defaultFS") != "hdfs://warehouse"
    or props.get("hadoop.security.authentication") != "kerberos"
  ):
    raise RuntimeError("synthetic Kerberos configuration guard failed")
  run(
    [
      "/usr/bin/kinit",
      "-kt",
      "/etc/security/keytabs/hdfs.headless.keytab",
      "hdfs@GEO.TEST",
    ]
  )


def roles():
  ids = ["primary", "backup"]
  props = {
    p.findtext("name"): p.findtext("value")
    for p in ET.parse("/etc/hadoop/conf/hdfs-site.xml").findall("property")
  }
  ids = props["dfs.ha.namenodes.warehouse"].split(",")
  result = {}
  for ident in ids:
    query = run(
      [HDFS, "haadmin", "-ns", "warehouse", "-getServiceState", ident],
      allowed=(0, 255, 1),
    )
    result[ident] = (
      query.stdout.decode().strip() if query.returncode == 0 else "unavailable"
    )
  return result


def identity():
  result = {}
  for line in Path("/data/nn/current/VERSION").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
      key, value = line.split("=", 1)
      if key in ("namespaceID", "clusterID", "blockpoolID"):
        result[key] = value
  if len(result) != 3:
    raise RuntimeError("incomplete synthetic namespace identity")
  return result


def nn_process():
  pid = int(Path("/var/run/hadoop/hdfs/hadoop-hdfs-namenode.pid").read_text())
  argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
  if b"org.apache.hadoop.hdfs.server.namenode.NameNode" not in argv:
    raise RuntimeError("synthetic NameNode process identity guard failed")
  return {"namenode_pid": pid}


def fault(action):
  import signal

  marker = Path("/var/tmp/geo-fault.json")
  if action == "freeze":
    state = roles()
    ident = ["primary", "backup", "third"][HOSTS.index(socket.getfqdn())]
    if state[ident] != "active" or list(state.values()).count("active") != 1:
      raise RuntimeError("fault injection requires the single Active on this host")
    nn_pid = nn_process()["namenode_pid"]
    ssh_pid = int(Path("/run/sshd.pid").read_text())
    if (Path("/proc") / str(ssh_pid) / "comm").read_text().strip() != "sshd":
      raise RuntimeError("SSH listener identity guard failed")
    marker.write_text(
      json.dumps({"nn_pid": nn_pid, "ssh_pid": ssh_pid, "host": socket.getfqdn()})
    )
    marker.chmod(0o600)
    os.kill(ssh_pid, signal.SIGSTOP)
    os.kill(nn_pid, signal.SIGSTOP)
    print(json.dumps({"frozen_nn": ident, "ssh_listener_paused": True}), flush=True)
  elif action == "restore-ssh":
    value = json.loads(marker.read_text())
    if value["host"] != socket.getfqdn():
      raise RuntimeError("fault owner mismatch")
    os.kill(value["ssh_pid"], signal.SIGCONT)
    print(json.dumps({"ssh_listener_restored": True}), flush=True)
  elif action == "result":
    value = json.loads(marker.read_text())
    cmd = Path("/proc") / str(value["nn_pid"]) / "cmdline"
    old_alive = cmd.exists() and bool(cmd.read_bytes())
    events = Path("/var/tmp/geo-fence.events")
    status = Path("/proc") / str(value["nn_pid"]) / "status"
    state = (
      next(
        (line for line in status.read_text().splitlines() if line.startswith("State:")),
        "",
      )
      if status.exists()
      else ""
    )
    print(
      json.dumps(
        {
          "old_nn_alive": old_alive,
          "old_nn_state": state,
          "fence_events": events.read_text().splitlines() if events.exists() else [],
        }
      ),
      flush=True,
    )
  elif action == "denied-write":
    result = run(
      [
        HDFS,
        "dfs",
        "-Dipc.client.rpc-timeout.ms=2000",
        "-Ddfs.client.failover.max.attempts=3",
        "-Ddfs.client.failover.sleep.max.millis=100",
        "-mkdir",
        "/lab/forbidden-during-fencing",
      ],
      allowed=(0, 1, 255),
    )
    if result.returncode == 0:
      raise RuntimeError("a write succeeded while fencing was unavailable")
    print(json.dumps({"new_write_refused": True}), flush=True)
  elif action == "events":
    events = Path("/var/tmp/geo-fence.events")
    print(
      json.dumps(events.read_text().splitlines() if events.exists() else []), flush=True
    )


def registrations():
  result = {}
  for host in HOSTS:
    query = run(
      [
        "/usr/bin/curl",
        "-fsSk",
        "--negotiate",
        "-u",
        ":",
        "--connect-timeout",
        "3",
        "--max-time",
        "10",
        "https://" + host + ":9871/jmx?qry=Hadoop:service=NameNode,name=NameNodeInfo",
      ]
    )
    bean = json.loads(query.stdout)["beans"][0]
    result[host] = {
      "live_datanodes": len(json.loads(bean["LiveNodes"])),
      "blockpoolID": bean["BlockPoolId"],
      "safemode": bean.get("Safemode", ""),
    }
  if any(value["live_datanodes"] != 3 for value in result.values()):
    raise RuntimeError(
      "not all three DataNodes registered with each NN: " + repr(result)
    )
  return result


def control(create=False, suffix=""):
  if create:
    run([HDFS, "dfs", "-mkdir", "-p", "/lab"])
    exists = run([HDFS, "dfs", "-test", "-e", CONTROL], allowed=(0, 1))
    if exists.returncode != 0:
      run([HDFS, "dfs", "-put", "-", CONTROL], DATA)
  result = run([HDFS, "dfs", "-cat", CONTROL]).stdout
  if hashlib.sha256(result).hexdigest() != CHECKSUM:
    raise RuntimeError("control dataset hash mismatch")
  if suffix:
    run([HDFS, "dfs", "-put", "-", "/lab/write-" + suffix], DATA[:65536])
    if run([HDFS, "dfs", "-cat", "/lab/write-" + suffix]).stdout != DATA[:65536]:
      raise RuntimeError("new write readback mismatch")
  states = roles()
  if list(states.values()).count("active") != 1:
    raise RuntimeError("expected exactly one observed Active: " + repr(states))
  result = {
    "control_sha256": CHECKSUM,
    "bytes": len(DATA),
    "roles": states,
    "new_write": bool(suffix),
    "identity": identity(),
  }
  EVIDENCE.write_text(json.dumps(result))
  print(json.dumps(result), flush=True)


def diagnose():
  tokens = [
    (Path("/lab-secrets") / name).read_text().strip()
    for name in ("db-password", "api-password", "store-password")
  ]
  for path in sorted(Path("/var/log/hadoop/hdfs").glob("*")):
    if path.suffix not in (".log", ".out", ".err"):
      continue
    text = path.read_text(errors="replace")
    if path.suffix == ".log":
      lines = text.splitlines()
      text = "\n".join(
        line
        for line in lines
        if any(term in line for term in ("ERROR", "Exception", "Caused by:", "FATAL"))
      )
    text = re.sub(r"[A-Za-z0-9+/=]{80,}", "<opaque-lab-token>", text)[-1600:]
    for token in tokens:
      text = text.replace(token, "<redacted-lab-credential>")
    if text.strip():
      print(path.name + ": " + text, flush=True)
  print(
    "pid_files: " + repr([p.name for p in Path("/var/run/hadoop/hdfs").glob("*.pid")])
  )
  wrapper = Path("/usr/bigtop/3.3.0/usr/lib/hadoop/sbin/hadoop-daemon.sh")
  print(
    "dn_pid_wrapper_present: "
    + str("HADOOP_IDENT_STRING=hdfs-root" in wrapper.read_text())
  )


def retained_client(action):
  import shutil

  directory = Path("/var/tmp/geo-client")
  directory.mkdir(exist_ok=True)
  shutil.chown(directory, "hdfs", "hadoop")
  if action == "start":
    if (directory / "pid").exists():
      raise RuntimeError("retained client was already launched")
    classpath = run([HDFS, "classpath", "--glob"]).stdout.decode().strip()
    with (directory / "client.log").open("wb") as log:
      child = subprocess.Popen(
        [
          "runuser",
          "-u",
          "hdfs",
          "--",
          "/usr/lib/jvm/jre-1.8.0-openjdk/bin/java",
          "-Xmx128m",
          "-XX:ActiveProcessorCount=2",
          "-cp",
          str(directory) + ":" + classpath,
          "RetainedClient",
          CHECKSUM,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
      )
    (directory / "pid").write_text(str(child.pid))
    for _ in range(60):
      if (directory / "status.json").exists():
        break
      time.sleep(1)
  elif action == "stop":
    (directory / "stop").touch()
  if (directory / "status.json").exists():
    result = json.loads((directory / "status.json").read_text())
    if action != "stop" and time.time() * 1000 - result["last_update_ms"] > 20000:
      raise RuntimeError("retained client progress is stale")
    print(json.dumps(result), flush=True)
  else:
    raise RuntimeError("retained client has no progress receipt")


def candidate_hashes():
  directory = Path(
    "/var/lib/ambari-agent/cache/stacks/BIGTOP/3.2.0/services/HDFS/package/scripts"
  )
  return {
    name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
    for name in ("datanode.py", "params_linux.py", "utils.py", "ha_topology.py")
  }


def final_checks():
  missing_ticket = subprocess.run(
    [
      "runuser",
      "-u",
      "hdfs",
      "--",
      "env",
      "KRB5CCNAME=FILE:/var/tmp/geo-client/absent-ticket",
      HDFS,
      "dfs",
      "-ls",
      "/lab",
    ],
    capture_output=True,
    timeout=45,
  )
  errors = missing_ticket.stderr.decode(errors="replace")
  if missing_ticket.returncode == 0 or not any(
    term in errors for term in ("KERBEROS", "GSS", "credentials", "authenticate")
  ):
    raise RuntimeError("ticketless client was not rejected by the Kerberos boundary")
  report = run([HDFS, "fsck", "/lab"]).stdout.decode()
  if "Status: HEALTHY" not in report:
    raise RuntimeError("synthetic dataset fsck was not healthy")
  print(
    json.dumps(
      {
        "ticketless_client_rejected": True,
        "fsck_healthy": True,
        "fsck_summary": [
          line.strip()
          for line in report.splitlines()
          if any(
            term in line
            for term in (
              "Total blocks",
              "Default replication",
              "Average block replication",
              "Under-replicated",
              "Missing replicas",
              "Corrupt blocks",
            )
          )
        ],
      }
    ),
    flush=True,
  )


if __name__ == "__main__":
  try:
    if sys.argv[1] == "diagnose":
      diagnose()
      sys.exit(0)
    if sys.argv[1] == "candidate-hashes":
      print(json.dumps(candidate_hashes()), flush=True)
      sys.exit(0)
    if sys.argv[1] == "ha-debug":
      rows = []
      for path in Path("/var/log/hadoop/hdfs").glob("*zkfc*.log"):
        lines = path.read_text(errors="replace").splitlines()
        rows += [
          line
          for line in lines
          if any(
            term.lower() in line.lower()
            for term in ("fenc", "health", "election", "active", "standby", "ERROR")
          )
        ]
      text = "\n".join(rows[-20:])
      print(
        re.sub(r"[A-Za-z0-9+/=]{80,}", "<opaque-lab-token>", text)[-4500:], flush=True
      )
      sys.exit(0)
    login()
    if sys.argv[1].startswith("fault-"):
      fault(sys.argv[1].split("-", 1)[1])
    elif sys.argv[1] == "failover-backup":
      states = roles()
      active = next(k for k, v in states.items() if v == "active")
      run([HDFS, "haadmin", "-ns", "warehouse", "-failover", active, "backup"])
      print(json.dumps(roles()), flush=True)
    elif sys.argv[1].startswith("client-"):
      retained_client(sys.argv[1].split("-", 1)[1])
    elif sys.argv[1] == "baseline":
      control(True, "baseline")
    elif sys.argv[1] == "check":
      control(False, sys.argv[2] if len(sys.argv) > 2 else "")
    elif sys.argv[1] == "roles":
      print(json.dumps(roles()), flush=True)
    elif sys.argv[1] == "identity":
      print(json.dumps(identity()), flush=True)
    elif sys.argv[1] == "process":
      print(json.dumps(nn_process()), flush=True)
    elif sys.argv[1] == "registrations":
      print(json.dumps(registrations()), flush=True)
    elif sys.argv[1] == "safemode":
      result = run(
        [HDFS, "dfsadmin", "-fs", "hdfs://nn1.geo.test:8020", "-safemode", "get"],
        allowed=(0, 1, 255),
      )
      print(
        json.dumps(
          {
            "exit": result.returncode,
            "stdout": result.stdout.decode()[-1500:],
            "stderr": result.stderr.decode()[-1500:],
          }
        ),
        flush=True,
      )
    elif sys.argv[1] == "final-checks":
      final_checks()
    else:
      raise RuntimeError("unsupported check")
  except Exception as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
