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

"""Own only explicitly labelled, local synthetic lab resources."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "target/hdfs-three-nn-lab"
TOOLS = Path(__file__).resolve().parent
CONTEXT = ROOT / "target/integration-context"


def run(argv, **kwargs):
  return subprocess.run(argv, check=True, **kwargs)


def state():
  data = json.loads((STATE / "state.json").read_text())
  if not data["project"].startswith("hdfs3nn-"):
    raise RuntimeError("unexpected lab project")
  return data


def compose(*args):
  data = state()
  return run(
    [
      "docker",
      "compose",
      "--project-name",
      data["project"],
      "--file",
      str(STATE / "compose.json"),
      *args,
    ]
  )


def prepare():
  os.umask(0o077)
  STATE.mkdir(mode=0o700, exist_ok=False)
  secret = STATE / "secrets"
  secret.mkdir(mode=0o700)
  data = {"id": str(uuid.uuid4()), "project": "hdfs3nn-" + secrets.token_hex(4)}
  (STATE / "state.json").write_text(json.dumps(data, indent=2) + "\n")
  for name in ("db-password", "api-password", "store-password"):
    (secret / name).write_text(secrets.token_urlsafe(24) + "\n")
  with open(os.devnull, "wb") as quiet:
    run(
      ["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(secret / "id_rsa")],
      stdout=quiet,
      stderr=quiet,
    )
    keytool = "keytool"
    if sys.platform == "darwin":
      java_dir = subprocess.check_output(
        ["/usr/libexec/java_home", "-v", "17"], text=True
      ).strip()
      keytool = str(Path(java_dir) / "bin/keytool")
    common = ["-storetype", "JKS", "-storepass:file", str(secret / "store-password")]
    run(
      [
        keytool,
        "-genkeypair",
        "-alias",
        "lab",
        "-keyalg",
        "RSA",
        "-keysize",
        "2048",
        "-validity",
        "3",
        "-dname",
        "CN=nn1.geo.test",
        "-ext",
        "SAN=dns:nn1.geo.test,dns:nn2.geo.test,dns:nn3.geo.test,dns:ambari.geo.test",
        "-keystore",
        str(secret / "keystore.jks"),
        "-keypass:file",
        str(secret / "store-password"),
        *common,
      ],
      stdout=quiet,
      stderr=quiet,
    )
    run(
      [
        keytool,
        "-exportcert",
        "-alias",
        "lab",
        "-keystore",
        str(secret / "keystore.jks"),
        "-file",
        str(secret / "lab.cer"),
        *common,
      ],
      stdout=quiet,
      stderr=quiet,
    )
    run(
      [
        keytool,
        "-importcert",
        "-noprompt",
        "-alias",
        "lab",
        "-file",
        str(secret / "lab.cer"),
        "-keystore",
        str(secret / "truststore.jks"),
        *common,
      ],
      stdout=quiet,
      stderr=quiet,
    )
  label = {"org.apache.ambari.lab.id": data["id"]}
  volume = {
    "type": "bind",
    "source": str(secret),
    "target": "/lab-secrets",
    "read_only": True,
  }
  services = {}
  services["db"] = {
    "image": "postgres:16",
    "pull_policy": "never",
    "hostname": "db.geo.test",
    "labels": label,
    "mem_limit": "384m",
    "environment": {
      "POSTGRES_USER": "ambari",
      "POSTGRES_DB": "ambari",
      "POSTGRES_PASSWORD_FILE": "/run/lab/db-password",
    },
    "volumes": [
      {
        "type": "bind",
        "source": str(secret / "db-password"),
        "target": "/run/lab/db-password",
        "read_only": True,
      },
      "db-data:/var/lib/postgresql/data",
    ],
    "networks": {"default": {"aliases": ["db.geo.test"]}},
    "command": ["postgres", "-c", "shared_buffers=64MB", "-c", "max_connections=80"],
  }
  for name, role, memory in [
    ("kdc", "kdc", "256m"),
    ("ambari", "server", "2g"),
    ("nn1", "agent", "1536m"),
    ("nn2", "agent", "1536m"),
    ("nn3", "agent", "1536m"),
  ]:
    mount = dict(volume)
    if name == "kdc":
      mount["read_only"] = False
    services[name] = {
      "image": "hdfs-three-nn-node:20261029"
      if role == "agent"
      else "hdfs-three-nn-control:20261029",
      "platform": "linux/amd64",
      "pull_policy": "never",
      "hostname": name + ".geo.test",
      "labels": label,
      "mem_limit": memory,
      "command": [role],
      "volumes": [
        mount,
        {
          "type": "bind",
          "source": str(TOOLS),
          "target": "/lab/source",
          "read_only": True,
        },
      ],
      "networks": {"default": {"aliases": [name + ".geo.test"]}},
    }
  services["ambari"]["entrypoint"] = ["python3", "/lab/source/container.py"]
  for name in ("nn1", "nn2", "nn3"):
    services[name]["init"] = True
    services[name]["entrypoint"] = ["python3", "/lab/source/container.py"]
  config = {
    "services": services,
    "networks": {"default": {"internal": True, "labels": label}},
    "volumes": {"db-data": {"labels": label}},
  }
  (STATE / "compose.json").write_text(json.dumps(config, indent=2) + "\n")
  print(
    "Prepared a private, labelled lab with an internal network and no published ports."
  )


def context():
  CONTEXT.mkdir(exist_ok=True)
  sources = {
    "ambari-server.tar.gz": ROOT
    / "ambari-server/target/ambari-server-3.0.0.0.0-dist.tar.gz",
    "ambari-agent.tar.gz": ROOT / "ambari-agent/target/ambari-agent-3.0.0.0.0.tar.gz",
  }
  for name, source in sources.items():
    shutil.copyfile(source, CONTEXT / name)
  for name in ("image_layout.py", "container.py"):
    shutil.copyfile(TOOLS / name, CONTEXT / name)
  return CONTEXT


def build_control():
  run(
    [
      "docker",
      "build",
      "--platform",
      "linux/amd64",
      "--file",
      str(TOOLS / "Dockerfile.control"),
      "--tag",
      "hdfs-three-nn-control:20261029",
      str(context()),
    ]
  )


def build_nodes():
  context()
  download = ROOT / "target/integration-downloads"
  for source_name, name in [
    ("hadoop-3.3.6.tar.gz", "hadoop.tar.gz"),
    ("apache-zookeeper-3.7.2-bin.tar.gz", "zookeeper.tar.gz"),
  ]:
    source = download / source_name
    checksum_name = source_name.replace("-mirror", "") + ".sha512"
    words = (download / checksum_name).read_text().replace("=", " ").split()
    expected = next(
      word
      for word in words
      if len(word) == 128 and all(c in "0123456789abcdefABCDEF" for c in word)
    )
    if hashlib.sha512(source.read_bytes()).hexdigest() != expected.lower():
      raise RuntimeError("distribution SHA-512 mismatch")
    shutil.copyfile(source, CONTEXT / name)
  selector_manifest = json.loads((TOOLS / "selector-sources.json").read_text())
  for name in ("distro-select", "conf-select"):
    if (
      hashlib.sha256((download / name).read_bytes()).hexdigest()
      != selector_manifest[name]["sha256"]
    ):
      raise RuntimeError("selector source digest mismatch")
    shutil.copyfile(download / name, CONTEXT / name)
  (CONTEXT / "params.py").write_text(
    'stack_root = "/usr/bigtop"\nstack_version = "3.3.0"\n'
  )
  run(
    [
      "docker",
      "build",
      "--platform",
      "linux/amd64",
      "--file",
      str(TOOLS / "Dockerfile.node"),
      "--tag",
      "hdfs-three-nn-node:20261029",
      str(CONTEXT),
    ]
  )


def container(name):
  data = state()
  output = subprocess.check_output(
    [
      "docker",
      "ps",
      "--all",
      "--filter",
      "label=com.docker.compose.project=" + data["project"],
      "--filter",
      "label=com.docker.compose.service=" + name,
      "--format",
      "{{.ID}}",
    ],
    text=True,
  ).split()
  if len(output) != 1:
    raise RuntimeError("lab container selection is not unique")
  labels = json.loads(
    subprocess.check_output(
      ["docker", "inspect", "--format", "{{json .Config.Labels}}", output[0]], text=True
    )
  )
  if labels.get("org.apache.ambari.lab.id") != data["id"]:
    raise RuntimeError("lab ownership label mismatch")
  return output[0]


def api(*phase):
  run(
    [
      "docker",
      "exec",
      container("ambari"),
      "python3",
      "/lab/source/controller.py",
      *phase,
    ]
  )


def check(name, *args):
  result = subprocess.run(
    ["docker", "exec", container(name), "python3", "/lab/source/node_check.py", *args],
    capture_output=True,
    text=True,
  )
  if result.returncode:
    raise RuntimeError(result.stderr[-2500:])
  return json.loads(result.stdout.splitlines()[-1])


def receipt(name, result):
  directory = STATE / "evidence"
  directory.mkdir(mode=0o700, exist_ok=True)
  (directory / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps(result), flush=True)


def snapshot():
  summary = {}
  networks = set()
  for name in ("db", "kdc", "ambari", "nn1", "nn2", "nn3"):
    identity = container(name)
    query = "{{json .State.Status}}|{{json .Image}}|{{json .HostConfig.Privileged}}|{{json .NetworkSettings.Ports}}|{{json .NetworkSettings.Networks}}"
    parts = (
      subprocess.check_output(
        ["docker", "inspect", "--format", query, identity], text=True
      )
      .strip()
      .split("|")
    )
    status, image, privileged, ports, membership = [
      json.loads(value) for value in parts
    ]
    if privileged or any(value for value in (ports or {}).values()):
      raise RuntimeError("lab isolation contract was not met")
    networks.update(v["NetworkID"] for v in membership.values())
    summary[name] = {
      "status": status,
      "image_id": image,
      "privileged": privileged,
      "published_ports": False,
    }
  for network in networks:
    query = "{{json .Internal}}|{{json .Labels}}"
    parts = (
      subprocess.check_output(
        ["docker", "network", "inspect", "--format", query, network], text=True
      )
      .strip()
      .split("|")
    )
    internal, labels = [json.loads(value) for value in parts]
    if not internal or labels.get("org.apache.ambari.lab.id") != state()["id"]:
      raise RuntimeError("lab network isolation/ownership contract was not met")
  receipt(
    "isolation-snapshot",
    {"containers": summary, "owned_internal_networks": len(networks)},
  )


def accept():
  import time

  def verify(name, expected, previous):
    roles = check("nn1", "roles")
    if roles != expected:
      raise RuntimeError(name + ": unexpected HA roles: " + repr(roles))
    data = check("nn1", "check", name)
    client = check("nn1", "client-status")
    if client["successes"] <= previous["successes"]:
      raise RuntimeError(name + ": retained FileSystem client did not make progress")
    receipt(name, {"data": data, "retained_client": client})
    return client

  if check("nn1", "roles") != {
    "primary": "active",
    "backup": "standby",
    "third": "standby",
  }:
    raise RuntimeError("acceptance requires the initial single Active on primary")
  client = check("nn1", "client-status")
  api("accept", "third-handoff")
  client = verify(
    "rolling-third-handoff",
    {"primary": "standby", "backup": "unavailable", "third": "active"},
    client,
  )
  api("accept", "restore-backup")
  api("accept", "stop-standbys")
  before = check("nn3", "process")
  api("accept", "refuse-active-stop")
  if check("nn3", "process") != before:
    raise RuntimeError("Active process changed during refused rolling stop")
  client = verify(
    "refused-active-stop",
    {"primary": "unavailable", "backup": "unavailable", "third": "active"},
    client,
  )
  api("accept", "restore-standbys")
  verify(
    "restored-standbys",
    {"primary": "standby", "backup": "standby", "third": "active"},
    client,
  )


def fence_test():
  import time

  deadline = time.monotonic() + 150
  while time.monotonic() < deadline:
    registrations = check("nn1", "registrations")
    if not any(value["safemode"] for value in registrations.values()):
      break
    time.sleep(3)
  else:
    raise RuntimeError(
      "Standby block reports did not make every NN ready for activation"
    )
  before = check("nn1", "client-status")
  roles = check("nn1", "failover-backup")
  if roles != {"primary": "standby", "backup": "active", "third": "standby"}:
    raise RuntimeError("controlled failover to backup did not complete")
  check("nn1", "check", "backup-active")
  check("nn2", "fault-freeze")
  failed = False
  try:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
      events = [
        event for name in ("nn1", "nn3") for event in check(name, "fault-events")
      ]
      if "nn2.geo.test failed" in events:
        failed = True
        break
      time.sleep(5)
    if not failed:
      raise RuntimeError("did not observe an actual failed SSH fencing attempt")
    roles = check("nn1", "roles")
    source = check("nn2", "fault-result")
    if (
      "active" in roles.values()
      or not source["old_nn_alive"]
      or "T" not in source["old_nn_state"]
    ):
      raise RuntimeError(
        "fencing unavailable did not retain a frozen old NN with no new Active"
      )
    denied = check("nn1", "fault-denied-write")
    receipt(
      "fencing-unavailable",
      {
        "roles": roles,
        "source": source,
        "new_write": denied,
        "actual_failed_fencing": True,
      },
    )
  finally:
    check("nn2", "fault-restore-ssh")
  deadline = time.monotonic() + 120
  while time.monotonic() < deadline:
    events = [event for name in ("nn1", "nn3") for event in check(name, "fault-events")]
    if "nn2.geo.test success" in events:
      break
    time.sleep(3)
  else:
    raise RuntimeError("restored SSH fencing did not succeed")
  source = check("nn2", "fault-result")
  roles = check("nn1", "roles")
  if source["old_nn_alive"] or list(roles.values()).count("active") != 1:
    raise RuntimeError("old NN was not fenced before a single new Active appeared")
  api("accept", "restart-backup")
  data = check("nn1", "check", "after-fencing")
  client = check("nn1", "client-status")
  if client["successes"] <= before["successes"]:
    raise RuntimeError("retained client did not recover after fencing")
  receipt(
    "fencing-recovered",
    {
      "data": data,
      "source": source,
      "retained_client": client,
      "actual_failed_then_successful_fencing": True,
    },
  )


def restart_proof():
  before = {name: check(name, "process") for name in ("nn1", "nn2", "nn3")}
  client_before = check("nn1", "client-status")
  with (STATE / "restart-proof.log").open("ab") as log:
    run(
      [
        "docker",
        "exec",
        container("ambari"),
        "python3",
        "/lab/source/container.py",
        "restart",
      ],
      stdout=log,
      stderr=subprocess.STDOUT,
    )
    for name in ("nn1", "nn2", "nn3"):
      run(
        ["docker", "exec", container(name), "/usr/sbin/ambari-agent", "restart"],
        stdout=log,
        stderr=subprocess.STDOUT,
      )
  api("state-summary")
  after = {name: check(name, "process") for name in ("nn1", "nn2", "nn3")}
  if before != after:
    raise RuntimeError("NameNode process changed during Ambari/agent restart")
  data = check("nn1", "check", "after-control-restart")
  client = check("nn1", "client-status")
  if (
    client["successes"] <= client_before["successes"]
    or client["failures"] != client_before["failures"]
  ):
    raise RuntimeError(
      "retained client did not continue cleanly during control-plane restarts"
    )
  receipt(
    "control-plane-restart",
    {
      "namenode_pids_unchanged": True,
      "data": data,
      "retained_client_before": client_before,
      "retained_client_after": client,
    },
  )


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "command",
    choices=[
      "prepare",
      "build-control",
      "build-nodes",
      "up-control",
      "up-nodes",
      "status",
      "probe",
      "bootstrap",
      "add-third",
      "accept",
      "stop",
      "diagnose",
      "server-errors",
      "restart-control",
      "repair-fixture",
      "repair-nodes",
      "baseline-check",
      "roles",
      "progress",
      "pause-bootstrap",
      "node-errors",
      "client-start",
      "client-status",
      "client-stop",
      "data-check",
      "repair-http",
      "audit-bootstrap",
      "safemode",
      "fence-test",
      "state-summary",
      "restart-agents",
      "fence-progress",
      "candidate-audit",
      "ha-debug",
      "restart-proof",
      "final-checks",
      "snapshot",
    ],
  )
  args = parser.parse_args()
  if args.command == "prepare":
    prepare()
  elif args.command == "build-control":
    build_control()
  elif args.command == "build-nodes":
    build_nodes()
  elif args.command == "up-control":
    compose("up", "--detach", "db", "kdc", "ambari")
  elif args.command == "up-nodes":
    compose("up", "--detach", "nn1", "nn2", "nn3")
  elif args.command == "status":
    compose("ps", "--all")
  elif args.command == "snapshot":
    snapshot()
  elif args.command == "accept":
    accept()
  elif args.command == "fence-test":
    fence_test()
  elif args.command == "restart-proof":
    restart_proof()
  elif args.command == "fence-progress":
    print(
      json.dumps(
        {
          "source": check("nn2", "fault-result"),
          "events": {name: check(name, "fault-events") for name in ("nn1", "nn3")},
        }
      ),
      flush=True,
    )
  elif args.command == "candidate-audit":
    source = (
      ROOT
      / "ambari-server/src/main/resources/stacks/BIGTOP/3.2.0/services/HDFS/package/scripts"
    )
    names = ("datanode.py", "params_linux.py", "utils.py", "ha_topology.py")
    expected = {
      name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names
    }
    for host in ("nn1", "nn2", "nn3"):
      if check(host, "candidate-hashes") != expected:
        raise RuntimeError("agent runtime candidate differs from source: " + host)
    receipt(
      "candidate-audit",
      {"agents_verified": 3, "runtime_modules_per_agent": 4, "source_sha256": expected},
    )
  elif args.command == "final-checks":
    receipt("final-checks", check("nn1", "final-checks"))
  elif args.command == "restart-agents":
    for name in ("nn1", "nn2", "nn3"):
      run(["docker", "exec", container(name), "/usr/sbin/ambari-agent", "restart"])
  elif args.command == "restart-control":
    run(
      [
        "docker",
        "exec",
        container("ambari"),
        "python3",
        "/lab/source/container.py",
        "restart",
      ]
    )
  elif args.command == "pause-bootstrap":
    result = subprocess.run(
      [
        "docker",
        "exec",
        container("ambari"),
        "pkill",
        "-f",
        "^python3 /lab/source/controller.py bootstrap$",
      ]
    )
    if result.returncode not in (0, 1):
      raise RuntimeError("could not pause the synthetic bootstrap controller")
  elif args.command == "ha-debug":
    for name in ("nn1", "nn2", "nn3"):
      print(name + ":", flush=True)
      run(
        [
          "docker",
          "exec",
          container(name),
          "python3",
          "/lab/source/node_check.py",
          "ha-debug",
        ]
      )
  elif args.command == "node-errors":
    for name in ("nn1", "nn2", "nn3"):
      print(name + ":", flush=True)
      run(
        [
          "docker",
          "exec",
          container(name),
          "python3",
          "/lab/source/node_check.py",
          "diagnose",
        ]
      )
  elif args.command == "repair-nodes":
    for name in ("nn1", "nn2", "nn3"):
      run(
        [
          "docker",
          "exec",
          container(name),
          "python3",
          "/lab/source/container.py",
          "repair-node",
        ]
      )
  elif args.command == "baseline-check":
    result = check("nn1", "baseline")
    if result["roles"] != {"primary": "active", "backup": "standby"}:
      raise RuntimeError("two-NN baseline roles were not accepted")
    receipt("baseline", result)
  elif args.command == "roles":
    print(json.dumps(check("nn1", "roles")), flush=True)
  elif args.command == "safemode":
    print(json.dumps(check("nn1", "safemode")), flush=True)
  elif args.command == "add-third":
    baseline = json.loads((STATE / "evidence/baseline.json").read_text())
    current = check("nn1", "check")
    if (
      current["identity"] != baseline["identity"]
      or current["control_sha256"] != baseline["control_sha256"]
    ):
      raise RuntimeError("baseline dataset/namespace guard failed")
    api("add-third")
  elif args.command == "data-check":
    import time

    result = check("nn1", "check", "accept-" + str(time.time_ns()))
    identities = [check(name, "identity") for name in ("nn1", "nn2", "nn3")]
    baseline = json.loads((STATE / "evidence/baseline.json").read_text())
    if any(value != baseline["identity"] for value in identities):
      raise RuntimeError("namespace identity changed after the transition")
    if sorted(result["roles"].values()) != ["active", "standby", "standby"]:
      raise RuntimeError("expected 1 Active and 2 Standby")
    result["three_matching_namespace_identities"] = True
    result["registrations"] = check("nn1", "registrations")
    receipt("data-" + str(time.time_ns()), result)
  elif args.command in ("client-start", "client-status", "client-stop"):
    identity = container("nn1")
    if args.command == "client-start":
      run(["docker", "exec", identity, "mkdir", "-p", "/var/tmp/geo-client"])
      run(
        [
          "docker",
          "cp",
          str(ROOT / "target/integration-tools/client-compile/RetainedClient.class"),
          identity + ":/var/tmp/geo-client/RetainedClient.class",
        ]
      )
    run(
      ["docker", "exec", identity, "python3", "/lab/source/node_check.py", args.command]
    )
  elif args.command == "server-errors":
    import io

    output = subprocess.check_output(
      [
        "docker",
        "cp",
        container("ambari") + ":/var/log/ambari-server/ambari-server.log",
        "-",
      ]
    )
    with tarfile.open(fileobj=io.BytesIO(output)) as archive:
      lines = (
        archive.extractfile(archive.getmembers()[0])
        .read()
        .decode(errors="replace")
        .splitlines()
      )
    selected = []
    for i, line in enumerate(lines):
      if " ERROR " in line or "Caused by:" in line:
        selected.extend(lines[i : i + 5])
    result = "\n".join(selected[-23:])
    for name in ("db-password", "api-password", "store-password"):
      result = result.replace(
        (STATE / "secrets" / name).read_text().strip(), "<redacted-lab-credential>"
      )
    print(result)
  elif args.command == "diagnose":
    password_paths = [
      STATE / "secrets" / name
      for name in ("db-password", "api-password", "store-password")
    ]
    tokens = [p.read_text().strip() for p in password_paths]
    for name in ("db", "kdc", "ambari", "nn1", "nn2", "nn3"):
      identity = container(name)
      if name == "db":
        print(
          "db: "
          + subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", identity], text=True
          ).strip()
        )
        continue
      output = subprocess.check_output(
        ["docker", "logs", "--tail", "18", identity],
        stderr=subprocess.STDOUT,
        text=True,
      )
      for token in tokens:
        output = output.replace(token, "<redacted-lab-credential>")
      print(name + ": " + output[-2500:])
      if name in ("ambari", "nn1", "nn2", "nn3"):
        import io

        logs = (
          (
            "/var/log/lab-server-setup.log",
            "/var/log/lab-server-start.log",
            "/var/log/ambari-server/ambari-server.log",
          )
          if name == "ambari"
          else (
            "/var/log/lab-agent-start.log",
            "/var/log/ambari-agent/ambari-agent.log",
          )
        )
        for log in logs:
          copied = subprocess.run(
            ["docker", "cp", identity + ":" + log, "-"], capture_output=True
          )
          if copied.returncode:
            continue
          with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as archive:
            text = (
              archive.extractfile(archive.getmembers()[0])
              .read()
              .decode(errors="replace")
            )
          for token in tokens:
            text = text.replace(token, "<redacted-lab-credential>")
          print(log + ": " + text[-3000:])
  elif args.command == "stop":
    for name in ("db", "kdc", "ambari", "nn1", "nn2", "nn3"):
      container(name)
    compose("stop")
  else:
    api(args.command)


if __name__ == "__main__":
  main()
