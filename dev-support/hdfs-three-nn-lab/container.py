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

"""Runtime entry point for private-network, synthetic lab containers."""
import configparser
import crypt
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time

SECRET = Path("/lab-secrets")
REALM = "GEO.TEST"
HOSTS = ["nn1.geo.test", "nn2.geo.test", "nn3.geo.test"]
JAVA8 = "/usr/lib/jvm/jre-1.8.0-openjdk"
JAVA17 = "/usr/lib/jvm/jre-17-openjdk"


def run(args, log=None, **kwargs):
  try:
    if log:
      with open(log, "ab") as output:
        return subprocess.run(
          args, stdout=output, stderr=subprocess.STDOUT, check=True, **kwargs
        )
    return subprocess.run(
      args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True, **kwargs
    )
  except subprocess.CalledProcessError as error:
    raise RuntimeError(
      "lab command failed: " + str(args[0]) + " (exit " + str(error.returncode) + ")"
    ) from None


def kerberos_config():
  Path("/etc/krb5.conf").write_text(f"""[libdefaults]
 default_realm = {REALM}
 dns_lookup_kdc = false
 dns_lookup_realm = false
 rdns = false
 udp_preference_limit = 1
[realms]
 {REALM} = {{
  kdc = kdc.geo.test
  admin_server = kdc.geo.test
 }}
[domain_realm]
 .geo.test = {REALM}
 geo.test = {REALM}
""")


def kdc():
  os.umask(0o077)
  kerberos_config()
  Path("/var/kerberos/krb5kdc/kdc.conf").write_text(f"""[kdcdefaults]
 kdc_ports = 88
 kdc_tcp_ports = 88
[realms]
 {REALM} = {{
  database_name = /var/kerberos/krb5kdc/principal
  key_stash_file = /var/kerberos/krb5kdc/.k5.{REALM}
  max_life = 1d
  max_renewable_life = 7d
  supported_enctypes = aes256-cts-hmac-sha1-96:normal aes128-cts-hmac-sha1-96:normal
 }}
""")
  if (SECRET / "kdc-ready").is_file() and Path(
    "/var/kerberos/krb5kdc/principal"
  ).is_file():
    os.execv("/usr/sbin/krb5kdc", ["krb5kdc", "-n", "-r", REALM])
  run(
    ["kdb5_util", "create", "-s", "-r", REALM, "-P", secrets.token_urlsafe(32)],
    "/var/log/lab-kdc-setup.log",
  )
  identities = [("hdfs", "hdfs.headless.keytab"), ("ambari-qa", "smoke.keytab")]
  for host in HOSTS:
    for service in ("nn", "dn", "jn", "HTTP", "zookeeper"):
      identities.append((service + "/" + host, host + "." + service + ".keytab"))
  for principal, filename in identities:
    run(
      [
        "kadmin.local",
        "-r",
        REALM,
        "-q",
        "addprinc -randkey " + principal + "@" + REALM,
      ],
      "/var/log/lab-kdc-setup.log",
    )
    run(
      [
        "kadmin.local",
        "-r",
        REALM,
        "-q",
        "ktadd -norandkey -k " + str(SECRET / filename) + " " + principal + "@" + REALM,
      ],
      "/var/log/lab-kdc-setup.log",
    )
  (SECRET / "kdc-ready").write_text("ready\n")
  os.execv("/usr/sbin/krb5kdc", ["krb5kdc", "-n", "-r", REALM])


def wait_for_kdc():
  for _ in range(120):
    if (SECRET / "kdc-ready").is_file():
      return
    time.sleep(1)
  raise RuntimeError("isolated KDC was not ready")


def server():
  import psycopg2

  os.umask(0o077)
  kerberos_config()
  password = (SECRET / "db-password").read_text().strip()
  connection = None
  for _ in range(120):
    try:
      connection = psycopg2.connect(
        host="db.geo.test", dbname="ambari", user="ambari", password=password
      )
      break
    except psycopg2.OperationalError:
      time.sleep(1)
  if connection is None:
    raise RuntimeError("isolated database was not ready")
  with connection:
    with connection.cursor() as cursor:
      cursor.execute("SELECT to_regclass('public.metainfo')")
      if cursor.fetchone()[0] is None:
        cursor.execute(
          Path(
            "/var/lib/ambari-server/resources/Ambari-DDL-Postgres-CREATE.sql"
          ).read_text()
        )
  connection.close()
  properties = Path("/etc/ambari-server/conf/ambari.properties")
  with properties.open("a") as f:
    f.write(
      "\npackages.pre.installed=true\nserver.execution.scheduler.maxThreads=2\nagent.task.timeout=180\n"
    )
    f.write("server.jdbc.postgres.schema=public\nserver.fqdn.service.url=\n")
  setup = [
    "/usr/sbin/ambari-server",
    "setup",
    "-s",
    "-j",
    JAVA8,
    "--ambari-java-home",
    JAVA17,
    "--database=postgres",
    "--databasehost=db.geo.test",
    "--databaseport=5432",
    "--databasename=ambari",
    "--postgresschema=public",
    "--databaseusername=ambari",
    "--databasepassword=" + password,
  ]
  run(setup, "/var/log/lab-server-setup.log")
  with Path("/var/lib/ambari-server/ambari-env.sh").open("a") as f:
    f.write(
      '\nexport AMBARI_JVM_ARGS="$AMBARI_JVM_ARGS -Xmx1024m -XX:ActiveProcessorCount=2"\n'
    )
  run(["/usr/sbin/ambari-server", "start"], "/var/log/lab-server-start.log")
  while True:
    time.sleep(5)


def agent():
  kerberos_config()
  repair_node_layout()
  wait_for_kdc()
  host = socket.getfqdn()
  if host not in HOSTS:
    raise RuntimeError("unexpected synthetic lab hostname")
  directory = Path("/etc/security/keytabs")
  directory.mkdir(parents=True, exist_ok=True)
  for service, filename, owner in [
    ("nn", "nn.service.keytab", "hdfs"),
    ("dn", "dn.service.keytab", "hdfs"),
    ("jn", "jn.service.keytab", "hdfs"),
    ("HTTP", "spnego.service.keytab", "hdfs"),
    ("zookeeper", "zk.service.keytab", "zookeeper"),
  ]:
    target = directory / filename
    shutil.copyfile(SECRET / (host + "." + service + ".keytab"), target)
    shutil.chown(target, owner, "hadoop")
    target.chmod(0o400)
  for source, target_name, owner in [
    ("hdfs.headless.keytab", "hdfs.headless.keytab", "hdfs"),
    ("smoke.keytab", "smoke.keytab", "ambari-qa"),
  ]:
    target = directory / target_name
    shutil.copyfile(SECRET / source, target)
    shutil.chown(target, owner, "hadoop")
    target.chmod(0o400)
  home = Path("/home/hdfs/.ssh")
  home.mkdir(mode=0o700, exist_ok=True)
  for source, target in [("id_rsa", "id_rsa"), ("id_rsa.pub", "authorized_keys")]:
    shutil.copyfile(SECRET / source, home / target)
    (home / target).chmod(0o600)
    shutil.chown(home / target, "hdfs", "hadoop")
  shutil.chown(home, "hdfs", "hadoop")
  run(
    [
      "usermod",
      "--password",
      crypt.crypt(secrets.token_urlsafe(32), crypt.mksalt(crypt.METHOD_SHA512)),
      "hdfs",
    ]
  )
  run(["ssh-keygen", "-A"])
  Path("/etc/ssh/sshd_config.d/99-lab.conf").write_text(
    "PasswordAuthentication no\nPermitRootLogin no\nAllowUsers hdfs\nUsePAM no\n"
  )
  run(["/usr/sbin/sshd"])
  for filename in ("keystore.jks", "truststore.jks"):
    target = Path("/etc/hadoop/conf") / filename
    shutil.copyfile(SECRET / filename, target)
    shutil.chown(target, "hdfs", "hadoop")
    target.chmod(0o400)
  config = configparser.ConfigParser()
  path = Path("/etc/ambari-agent/conf/ambari-agent.ini")
  config.read(path)
  config["server"]["hostname"] = "ambari.geo.test"
  config["network"]["use_system_proxy_settings"] = "false"
  with path.open("w") as f:
    config.write(f)
  run(["/usr/sbin/ambari-agent", "start"], "/var/log/lab-agent-start.log")
  while True:
    time.sleep(5)


def restart_server():
  if socket.getfqdn() != "ambari.geo.test":
    raise RuntimeError("server restart is restricted to the synthetic Ambari host")
  for directory in Path("/proc").iterdir():
    if not directory.name.isdigit():
      continue
    try:
      argv = (directory / "cmdline").read_bytes().split(b"\0")
      if b"org.apache.ambari.server.controller.AmbariServer" in argv:
        os.kill(int(directory.name), signal.SIGTERM)
        for _ in range(20):
          if not (directory / "cmdline").read_bytes():
            break
          time.sleep(0.5)
        else:
          os.kill(int(directory.name), signal.SIGKILL)
    except FileNotFoundError:
      pass
  Path("/var/run/ambari-server/ambari-server.pid").unlink(missing_ok=True)
  run(["/usr/sbin/ambari-server", "start"], "/var/log/lab-server-start.log")


def repair_node_layout():
  if socket.getfqdn() not in HOSTS:
    raise RuntimeError("node fixture is restricted to synthetic hosts")
  # Docker's PTR names are container IDs, while host keytabs use the explicit
  # synthetic FQDNs. Supply forward/reverse canonical names to Java as well.
  host_file = Path("/etc/hosts")
  previous = host_file.read_text()
  begin, end = "# geo-lab-canonical-begin\n", "# geo-lab-canonical-end\n"
  if begin in previous:
    before, rest = previous.split(begin, 1)
    previous = before + rest.split(end, 1)[1]
  # Stopped peers have no Docker DNS record. Keep this node alive while the
  # other owned containers are starting, including after a full lab stop.
  for attempt in range(60):
    try:
      entries = "".join(socket.gethostbyname(host) + " " + host + "\n" for host in HOSTS)
      break
    except socket.gaierror:
      if attempt == 59:
        raise RuntimeError("synthetic peer DNS did not become ready") from None
      time.sleep(1)
  host_file.write_text(begin + entries + end + previous)
  for name in ("hadoop", "hdfs"):
    target = Path("/usr/bin") / name
    if target.is_symlink():
      target.unlink()
    target.write_text(
      "#!/bin/bash\nexec /usr/bigtop/current/hadoop-client/bin/" + name + ' "$@"\n'
    )
    target.chmod(0o755)
  # The raw Apache tarball names non-root SASL DN pid files differently from
  # the BIGTOP package contract expected by stock Ambari status_params.py.
  daemon = Path("/usr/bigtop/3.3.0/usr/lib/hadoop/sbin/hadoop-daemon.sh")
  original = daemon.with_name("hadoop-daemon.sh.apache")
  if not original.exists():
    daemon.rename(original)
    daemon.write_text(
      '#!/bin/bash\nfor arg in "$@"; do\n  if [[ "$arg" == datanode ]]; then export HADOOP_IDENT_STRING=hdfs-root; fi\ndone\nexec "$(dirname "$0")/hadoop-daemon.sh.apache" "$@"\n'
    )
    daemon.chmod(0o755)
  old_pid = Path("/var/run/hadoop/hdfs/hadoop-hdfs-datanode.pid")
  if old_pid.exists():
    pid = int(old_pid.read_text())
    cmdline = Path("/proc") / str(pid) / "cmdline"
    if (
      cmdline.exists()
      and b"org.apache.hadoop.hdfs.server.datanode.DataNode"
      in cmdline.read_bytes().split(b"\0")
    ):
      os.kill(pid, signal.SIGTERM)
      for _ in range(40):
        if not cmdline.exists():
          break
        time.sleep(0.25)
      else:
        raise RuntimeError("synthetic old-format DN did not stop")
    old_pid.unlink()


if __name__ == "__main__":
  {
    "kdc": kdc,
    "server": server,
    "agent": agent,
    "restart": restart_server,
    "repair-node": repair_node_layout,
  }[sys.argv[1]]()
