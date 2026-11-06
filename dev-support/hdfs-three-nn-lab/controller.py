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

"""Ambari API driver restricted to the synthetic private-network lab."""
import json
import re
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import requests

SECRET = Path("/lab-secrets")
HOSTS = ["nn1.geo.test", "nn2.geo.test", "nn3.geo.test"]
BASE = "http://ambari.geo.test:8080/api/v1"
CLUSTER = "/clusters/geo"
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({"X-Requested-By": "isolated-three-nn-lab"})
TOKENS = [
  (SECRET / name).read_text().strip()
  for name in ("api-password", "store-password", "db-password")
]
SESSION.auth = ("admin", TOKENS[0])


def clean(value):
  text = str(value)
  for token in TOKENS:
    text = text.replace(token, "<redacted-lab-credential>")
  return re.sub(r"[A-Za-z0-9+/=]{80,}", "<opaque-lab-token>", text)


def api(method, path, payload=None, allowed=(200, 201, 202)):
  if not path.startswith("/") or "://" in path:
    raise RuntimeError("unexpected lab API path")
  r = SESSION.request(method, BASE + path, json=payload, timeout=45)
  if r.status_code not in allowed:
    raise RuntimeError(clean(f"{method} {path}: HTTP {r.status_code}: {r.text[:1800]}"))
  return r.json() if r.content else {}


def login():
  for _ in range(120):
    try:
      r = SESSION.get(BASE + "/users/admin", timeout=5)
      if r.status_code == 200:
        return
      if r.status_code == 403 or r.status_code == 401:
        SESSION.auth = ("admin", "admin")
        r = SESSION.get(BASE + "/users/admin", timeout=5)
        if r.status_code == 200:
          api(
            "PUT",
            "/users/admin",
            {"Users": {"password": TOKENS[0], "old_password": "admin"}},
          )
          SESSION.auth = ("admin", TOKENS[0])
          return
        SESSION.auth = ("admin", TOKENS[0])
    except requests.RequestException:
      pass
    time.sleep(2)
  raise RuntimeError("local Ambari API did not become ready")


def defaults():
  result = {}
  path = Path(
    "/var/lib/ambari-server/resources/stacks/BIGTOP/3.2.0/configuration/cluster-env.xml"
  )
  result["cluster-env"] = {
    p.findtext("name"): p.findtext("value") or ""
    for p in ET.parse(path).findall("property")
  }
  for name in ("stack_features", "stack_tools", "stack_packages"):
    value = json.loads(
      (path.parent.parent / "properties" / (name + ".json")).read_text()
    )
    result["cluster-env"][name] = json.dumps(value)
  for service in ("HDFS", "ZOOKEEPER", "KERBEROS"):
    rows = api(
      "GET",
      "/stacks/BIGTOP/versions/3.3.0/services/"
      + service
      + "/configurations?fields=StackConfigurations/*",
    )["items"]
    for row in rows:
      p = row["StackConfigurations"]
      typ = p["type"].removesuffix(".xml")
      result.setdefault(typ, {})[p["property_name"]] = str(p.get("property_value", ""))
  return result


def exists(path):
  r = SESSION.get(BASE + path, timeout=30)
  if r.status_code == 404:
    return False
  if r.status_code != 200:
    raise RuntimeError(clean(f"GET {path}: HTTP {r.status_code}: {r.text[:1800]}"))
  return True


def wait_request(result, expect_failure=False, required_error=None):
  if "Requests" not in result:
    return
  request_id = result["Requests"]["id"]
  path = CLUSTER + "/requests/" + str(request_id)
  deadline = time.monotonic() + 300
  while time.monotonic() < deadline:
    request = api("GET", path + "?fields=Requests/request_status")["Requests"]
    status = request["request_status"]
    if status in ("COMPLETED", "FAILED", "ABORTED", "TIMEDOUT"):
      tasks = api("GET", path + "/tasks?fields=Tasks/status,Tasks/stderr")["items"]
      errors = [
        clean(x["Tasks"].get("stderr", "")[-4000:])
        for x in tasks
        if x["Tasks"]["status"] != "COMPLETED"
      ]
      directory = Path("/var/lib/ambari-server/lab-requests")
      directory.mkdir(exist_ok=True)
      (directory / (str(request_id) + ".json")).write_text(
        json.dumps({"request": request_id, "status": status, "errors": errors})
      )
      if (status != "COMPLETED") != expect_failure:
        raise RuntimeError(f"local request {request_id}: {status}: {errors}")
      if required_error and not any(required_error in error for error in errors):
        raise RuntimeError(
          f"local request {request_id}: failed for an unexpected reason: {errors}"
        )
      print(json.dumps({"request": request_id, "status": status}), flush=True)
      return status
    time.sleep(2)
  raise RuntimeError("local request deadline exceeded: " + str(request_id))


def component(host, name, desired):
  if (SECRET / "pause-bootstrap").exists():
    raise RuntimeError("synthetic bootstrap paused before the next component operation")
  if host not in HOSTS:
    raise RuntimeError("unexpected lab host")
  path = CLUSTER + "/hosts/" + host + "/host_components/" + name
  if not exists(path):
    api("POST", path, {})
  current = api("GET", path + "?fields=HostRoles/state")["HostRoles"]["state"]
  if current != desired:
    wait_request(api("PUT", path, {"HostRoles": {"state": desired}}))


def install_component(host, name):
  path = CLUSTER + "/hosts/" + host + "/host_components/" + name
  current = api("GET", path + "?fields=HostRoles/state")["HostRoles"]["state"]
  if current not in ("INSTALLED", "STARTED"):
    component(host, name, "INSTALLED")


def command(
  host,
  component_name,
  action,
  parameters=None,
  expect_failure=False,
  required_error=None,
):
  if host not in HOSTS:
    raise RuntimeError("unexpected lab host")
  service = "ZOOKEEPER" if component_name.startswith("ZOOKEEPER") else "HDFS"
  payload = {
    "RequestInfo": {
      "command": action,
      "context": "Isolated synthetic lab: " + action,
      "parameters": parameters or {},
    },
    "Requests/resource_filters": [
      {"service_name": service, "component_name": component_name, "hosts": host}
    ],
  }
  return wait_request(
    api("POST", CLUSTER + "/requests", payload), expect_failure, required_error
  )


def rolling_restart(host, expect_failure=False):
  command(
    host,
    "NAMENODE",
    "RESTART",
    {"upgrade_type": "rolling_upgrade", "version": "3.3.0"},
    expect_failure,
    "Cannot restart the Active NameNode without an available Standby"
    if expect_failure
    else None,
  )


def acceptance(action):
  if (
    current_properties("hdfs-site")["dfs.ha.namenodes.warehouse"]
    != "primary,backup,third"
  ):
    raise RuntimeError("three-NN acceptance configuration guard failed")
  if action == "third-handoff":
    component(HOSTS[1], "NAMENODE", "INSTALLED")
    rolling_restart(HOSTS[0])
  elif action == "restore-backup":
    component(HOSTS[1], "NAMENODE", "STARTED")
  elif action == "stop-standbys":
    component(HOSTS[0], "NAMENODE", "INSTALLED")
    component(HOSTS[1], "NAMENODE", "INSTALLED")
  elif action == "refuse-active-stop":
    rolling_restart(HOSTS[2], expect_failure=True)
  elif action == "restore-standbys":
    for host in HOSTS[:2]:
      component(host, "NAMENODE", "STARTED")
  elif action == "restart-backup":
    command(HOSTS[1], "NAMENODE", "RESTART")
  else:
    raise RuntimeError("unsupported acceptance action")


def configure(properties, tag):
  tag = tag + "-" + str(time.time_ns())
  for typ, props in properties.items():
    api(
      "PUT",
      CLUSTER,
      {"Clusters": {"desired_config": {"type": typ, "tag": tag, "properties": props}}},
    )


def initial_properties():
  p = defaults()
  # The shipped HDFS script reads these users even when their services are absent.
  for typ, users in {
    "falcon-env": {"falcon_user": "falcon"},
    "yarn-env": {"yarn_user": "yarn"},
    "hbase-env": {"hbase_user": "hbase"},
    "oozie-env": {"oozie_user": "oozie"},
    "hive-env": {"webhcat_user": "hcat", "hive_user": "hive"},
    "mapred-env": {"mapred_user": "mapred"},
  }.items():
    p[typ] = users
  p["cluster-env"].update(
    {
      "security_enabled": "true",
      "ignore_groupsusers_create": "true",
      "sysprep_skip_create_users_and_groups": "true",
      "smokeuser_principal_name": "ambari-qa@GEO.TEST",
      "smokeuser_keytab": "/etc/security/keytabs/smoke.keytab",
      "dfs_ha_initial_namenode_active": HOSTS[0],
      "dfs_ha_initial_namenode_standby": HOSTS[1],
      "recovery_enabled": "false",
    }
  )
  p["kerberos-env"].update(
    {
      "realm": "GEO.TEST",
      "kdc_type": "mit-kdc",
      "kdc_hosts": "kdc.geo.test",
      "admin_server_host": "kdc.geo.test",
      "manage_identities": "false",
      "manage_auth_to_local": "false",
      "install_packages": "false",
    }
  )
  p["krb5-conf"]["manage_krb5_conf"] = "false"
  p["core-site"].update(
    {
      "fs.defaultFS": "hdfs://warehouse",
      "hadoop.security.authentication": "kerberos",
      "hadoop.security.authorization": "true",
      "hadoop.security.auth_to_local": "RULE:[2:$1@$0]([ndj]n@GEO.TEST)s/.*/hdfs/\nDEFAULT",
      "ha.zookeeper.quorum": ",".join(h + ":2181" for h in HOSTS),
      "ha.zookeeper.session-timeout.ms": "10000",
      "ipc.client.connect.timeout": "1000",
      "ipc.client.connect.max.retries": "0",
      "ipc.client.connect.max.retries.on.timeouts": "0",
    }
  )
  p["core-site"].update(
    {
      "hadoop.http.authentication.type": "kerberos",
      "hadoop.http.authentication.kerberos.principal": "HTTP/_HOST@GEO.TEST",
      "hadoop.http.authentication.kerberos.keytab": "/etc/security/keytabs/spnego.service.keytab",
    }
  )
  p["hadoop-env"].update(
    {
      "hadoop_heapsize": "128",
      "namenode_heapsize": "256m",
      "namenode_opt_newsize": "32m",
      "namenode_opt_maxnewsize": "32m",
      "dtnode_heapsize": "128m",
      "hdfs_user_keytab": "/etc/security/keytabs/hdfs.headless.keytab",
      "hdfs_principal_name": "hdfs@GEO.TEST",
    }
  )
  p["hadoop-env"]["content"] += (
    '\nexport HADOOP_JOURNALNODE_OPTS="$HADOOP_JOURNALNODE_OPTS -Xmx128m -XX:ActiveProcessorCount=2"\nexport HADOOP_ZKFC_OPTS="$HADOOP_ZKFC_OPTS -Xmx96m -XX:ActiveProcessorCount=2"\nexport HADOOP_OPTS="$HADOOP_OPTS -XX:ActiveProcessorCount=2"\n'
  )
  p["hadoop-env"]["content"] = p["hadoop-env"]["content"].replace(
    "export HADOOP_IDENT_STRING=$USER",
    "export HADOOP_IDENT_STRING=${HADOOP_IDENT_STRING:-$USER}",
  )
  s = p["hdfs-site"]
  s.update(
    {
      "dfs.nameservices": "legacy,warehouse",
      "dfs.internal.nameservices": "warehouse",
      "dfs.ha.namenodes.legacy": "east,west",
      "dfs.namenode.rpc-address.legacy.east": "east.invalid:8020",
      "dfs.namenode.rpc-address.legacy.west": "west.invalid:8020",
      "dfs.ha.namenodes.warehouse": "primary,backup",
      "dfs.namenode.shared.edits.dir": "qjournal://"
      + ";".join(h + ":8485" for h in HOSTS)
      + "/warehouse",
      "dfs.namenode.name.dir": "/data/nn",
      "dfs.datanode.data.dir": "/data/dn",
      "dfs.journalnode.edits.dir": "/data/jn",
      "dfs.client.failover.proxy.provider.warehouse": "org.apache.hadoop.hdfs.server.namenode.ha.ConfiguredFailoverProxyProvider",
      "dfs.ha.automatic-failover.enabled": "true",
      "dfs.ha.fencing.methods": "shell(/lab/source/fence.sh)",
      "dfs.ha.fencing.ssh.connect-timeout": "3000",
      "dfs.replication": "3",
      "dfs.namenode.safemode.min.datanodes": "1",
      "dfs.namenode.safemode.extension": "0",
      "dfs.namenode.handler.count": "4",
      "dfs.namenode.service.handler.count": "2",
      "dfs.namenode.http-address": "0.0.0.0:9870",
      "dfs.namenode.https-address": "0.0.0.0:9871",
      "dfs.datanode.address": "0.0.0.0:9866",
      "dfs.datanode.ipc.address": "0.0.0.0:9867",
      "dfs.datanode.http.address": "0.0.0.0:9864",
      "dfs.datanode.https.address": "0.0.0.0:9865",
      "dfs.http.policy": "HTTPS_ONLY",
      "dfs.https.enable": "true",
      "dfs.data.transfer.protection": "authentication",
      "dfs.namenode.kerberos.principal": "nn/_HOST@GEO.TEST",
      "dfs.namenode.keytab.file": "/etc/security/keytabs/nn.service.keytab",
      "dfs.datanode.kerberos.principal": "dn/_HOST@GEO.TEST",
      "dfs.datanode.keytab.file": "/etc/security/keytabs/dn.service.keytab",
      "dfs.journalnode.kerberos.principal": "jn/_HOST@GEO.TEST",
      "dfs.journalnode.keytab.file": "/etc/security/keytabs/jn.service.keytab",
      "dfs.web.authentication.kerberos.principal": "HTTP/_HOST@GEO.TEST",
      "dfs.web.authentication.kerberos.keytab": "/etc/security/keytabs/spnego.service.keytab",
      "dfs.namenode.kerberos.internal.spnego.principal": "${dfs.web.authentication.kerberos.principal}",
      "dfs.journalnode.kerberos.internal.spnego.principal": "${dfs.web.authentication.kerberos.principal}",
      "dfs.datanode.max.transfer.threads": "16",
      "dfs.client.failover.sleep.base.millis": "100",
      "dfs.client.failover.sleep.max.millis": "500",
      "dfs.client.failover.max.attempts": "30",
    }
  )
  for ident, host in zip(("primary", "backup"), HOSTS):
    s["dfs.namenode.rpc-address.warehouse." + ident] = host + ":8020"
    s["dfs.namenode.https-address.warehouse." + ident] = host + ":9871"
    s["dfs.namenode.http-address.warehouse." + ident] = host + ":9870"
  p["zookeeper-env"].update(
    {
      "zk_server_heapsize": "64",
      "zookeeper_principal_name": "zookeeper/_HOST@GEO.TEST",
      "zookeeper_keytab_path": "/etc/security/keytabs/zk.service.keytab",
    }
  )
  p["zookeeper-env"]["content"] += (
    '\nexport SERVER_JVMFLAGS="$SERVER_JVMFLAGS -XX:ActiveProcessorCount=2 -Dzookeeper.nio.numWorkerThreads=4"\n'
  )
  p["zoo.cfg"].update(
    {
      "dataDir": "/data/zk",
      "clientPort": "2181",
      "4lw.commands.whitelist": "ruok,srvr",
      "authProvider.1": "org.apache.zookeeper.server.auth.SASLAuthenticationProvider",
    }
  )
  p["ssl-server"].update(
    {
      "ssl.server.keystore.location": "/etc/hadoop/conf/keystore.jks",
      "ssl.server.keystore.password": TOKENS[1],
      "ssl.server.keystore.keypassword": TOKENS[1],
      "ssl.server.truststore.location": "/etc/hadoop/conf/truststore.jks",
      "ssl.server.truststore.password": TOKENS[1],
    }
  )
  p["ssl-client"].update(
    {
      "ssl.client.truststore.location": "/etc/hadoop/conf/truststore.jks",
      "ssl.client.truststore.password": TOKENS[1],
    }
  )
  return p


def bootstrap():
  for _ in range(90):
    hosts = {x["Hosts"]["host_name"] for x in api("GET", "/hosts")["items"]}
    if set(HOSTS) <= hosts:
      break
    time.sleep(2)
  else:
    raise RuntimeError("not all three synthetic agents registered")
  if not any(
    x["Clusters"]["cluster_name"] == "geo" for x in api("GET", "/clusters")["items"]
  ):
    api("POST", CLUSTER, {"Clusters": {"version": "BIGTOP-3.3.0"}})
  repo_path = "/stacks/BIGTOP/versions/3.3.0/repository_versions"
  repos = api("GET", repo_path)["items"]
  if not repos:
    api(
      "POST",
      repo_path,
      {
        "RepositoryVersions": {
          "repository_version": "3.3.0",
          "display_name": "Synthetic preinstalled 3.3.0",
        },
        "operating_systems": [
          {
            "OperatingSystems": {
              "os_type": "redhat9",
              "ambari_managed_repositories": False,
            },
            "repositories": [
              {
                "Repositories": {
                  "repo_id": "BIGTOP-3.3.0",
                  "repo_name": "bigtop",
                  "base_url": "http://packages.geo.test/preinstalled",
                }
              }
            ],
          }
        ],
      },
    )
    repos = api("GET", repo_path)["items"]
  repo_id = repos[0]["RepositoryVersions"]["id"]
  for service in ("ZOOKEEPER", "HDFS", "KERBEROS"):
    path = CLUSTER + "/services/" + service
    if not exists(path):
      api("POST", path, {"ServiceInfo": {"desired_repository_version_id": repo_id}})
  if not Path("/var/lib/ambari-server/lab-baseline-configured").is_file():
    configure(initial_properties(), "lab-baseline")
    Path("/var/lib/ambari-server/lab-baseline-configured").touch()
  for host in HOSTS:
    if not exists(CLUSTER + "/hosts/" + host):
      api("POST", CLUSTER + "/hosts/" + host, {})
  for service, names in [
    ("ZOOKEEPER", ("ZOOKEEPER_SERVER", "ZOOKEEPER_CLIENT")),
    ("HDFS", ("NAMENODE", "DATANODE", "JOURNALNODE", "ZKFC", "HDFS_CLIENT")),
    ("KERBEROS", ("KERBEROS_CLIENT",)),
  ]:
    for name in names:
      path = CLUSTER + "/services/" + service + "/components/" + name
      if not exists(path):
        api("POST", path, {})
  # Resolve every host assignment before an install script reads clusterHostInfo.
  for host in HOSTS:
    names = [
      "KERBEROS_CLIENT",
      "ZOOKEEPER_CLIENT",
      "HDFS_CLIENT",
      "ZOOKEEPER_SERVER",
      "JOURNALNODE",
      "DATANODE",
    ]
    if host != HOSTS[2]:
      names += ["NAMENODE", "ZKFC"]
    for name in names:
      path = CLUSTER + "/hosts/" + host + "/host_components/" + name
      if not exists(path):
        api("POST", path, {})
  for host in HOSTS:
    for name in (
      "KERBEROS_CLIENT",
      "ZOOKEEPER_CLIENT",
      "HDFS_CLIENT",
      "ZOOKEEPER_SERVER",
      "JOURNALNODE",
      "DATANODE",
    ):
      install_component(host, name)
    if host != HOSTS[2]:
      for name in ("NAMENODE", "ZKFC"):
        install_component(host, name)
  for name in ("ZOOKEEPER_SERVER", "JOURNALNODE"):
    for host in HOSTS:
      component(host, name, "STARTED")
  for host in HOSTS:
    component(host, "DATANODE", "STARTED")
  component(HOSTS[0], "ZKFC", "STARTED")
  for host in HOSTS[:2]:
    component(host, "NAMENODE", "STARTED")
  for host in HOSTS[:2]:
    component(host, "ZKFC", "STARTED")
  print(
    "Two-NameNode Kerberos baseline started through native Ambari commands.", flush=True
  )


def repair_fixture():
  # Only pre-daemon lab configuration, never a production configuration source.
  for host in HOSTS[:2]:
    path = (
      CLUSTER + "/hosts/" + host + "/host_components/NAMENODE?fields=HostRoles/state"
    )
    if exists(path) and api("GET", path)["HostRoles"]["state"] == "STARTED":
      raise RuntimeError("fixture reset is forbidden after a NameNode is started")
  configure(initial_properties(), "lab-fixture")
  print("Synthetic fixture configuration updated.", flush=True)


def current_properties(typ):
  desired = api("GET", CLUSTER + "?fields=Clusters/desired_configs")["Clusters"][
    "desired_configs"
  ]
  tag = desired[typ]["tag"]
  rows = api("GET", CLUSTER + "/configurations?type=" + typ + "&tag=" + tag)["items"]
  if len(rows) != 1:
    raise RuntimeError("configuration generation selection was not unique")
  return rows[0]["properties"]


def repair_http_auth():
  component(HOSTS[0], "ZKFC", "INSTALLED")
  component(HOSTS[0], "NAMENODE", "INSTALLED")
  core = current_properties("core-site")
  core.update(
    {
      "hadoop.http.authentication.type": "kerberos",
      "hadoop.http.authentication.kerberos.principal": "HTTP/_HOST@GEO.TEST",
      "hadoop.http.authentication.kerberos.keytab": "/etc/security/keytabs/spnego.service.keytab",
    }
  )
  configure({"core-site": core}, "lab-http-kerberos")
  for host in HOSTS:
    command(host, "JOURNALNODE", "RESTART")
  print(
    "HTTP Kerberos configuration delivered to synthetic journal servers.", flush=True
  )


def add_third():
  marker = Path("/var/lib/ambari-server/lab-third-configured")
  if not marker.exists():
    old = current_properties("hdfs-site")
    if old["dfs.ha.namenodes.warehouse"] != "primary,backup":
      raise RuntimeError("unexpected initial NN IDs")
    for host in HOSTS[:2]:
      component(host, "ZKFC", "INSTALLED")
    for host in HOSTS[:2]:
      component(host, "NAMENODE", "INSTALLED")
    old["dfs.ha.namenodes.warehouse"] = "primary,backup,third"
    old["dfs.namenode.rpc-address.warehouse.third"] = HOSTS[2] + ":8020"
    old["dfs.namenode.http-address.warehouse.third"] = HOSTS[2] + ":9870"
    old["dfs.namenode.https-address.warehouse.third"] = HOSTS[2] + ":9871"
    env = current_properties("cluster-env")
    env.pop("dfs_ha_initial_namenode_active", None)
    env.pop("dfs_ha_initial_namenode_standby", None)
    env["hdfs_namenode_format_disabled"] = "true"
    configure({"hdfs-site": old, "cluster-env": env}, "lab-third")
    for name in ("NAMENODE", "ZKFC"):
      component(HOSTS[2], name, "INSTALLED")
    # With both source NN stopped, bootstrap must fail without force/format.
    command(
      HOSTS[2],
      "NAMENODE",
      "BOOTSTRAP_STANDBY",
      expect_failure=True,
      required_error="Unable to fetch namespace information from any remote NN",
    )
    marker.touch()
  component(HOSTS[0], "ZKFC", "STARTED")
  for host in HOSTS[:2]:
    component(host, "NAMENODE", "STARTED")
  component(HOSTS[1], "ZKFC", "STARTED")
  bootstrapped = Path("/var/lib/ambari-server/lab-third-bootstrapped")
  if not bootstrapped.exists():
    command(HOSTS[2], "NAMENODE", "BOOTSTRAP_STANDBY")
    bootstrapped.touch()
  component(HOSTS[2], "NAMENODE", "STARTED")
  component(HOSTS[2], "ZKFC", "STARTED")
  for host in HOSTS:
    command(host, "DATANODE", "RECONFIGURE")
    command(host, "DATANODE", "REFRESH_NAMENODES")
  props = current_properties("hdfs-site")
  assert props["dfs.ha.namenodes.legacy"] == "east,west"
  assert props["dfs.namenode.rpc-address.legacy.east"] == "east.invalid:8020"
  assert props["dfs.ha.namenodes.warehouse"] == "primary,backup,third"
  print(
    "Third NN installed, bootstrapped and started through Ambari; DataNode refresh commands completed.",
    flush=True,
  )


def probe():
  props = defaults()
  print(
    json.dumps(
      {
        "api": "ready",
        "config_types": {k: len(v) for k, v in props.items()},
        "hosts": [x["Hosts"]["host_name"] for x in api("GET", "/hosts")["items"]],
      }
    )
  )


def progress():
  rows = api("GET", CLUSTER + "/requests?fields=Requests/id,Requests/request_status")[
    "items"
  ]
  for row in sorted(rows, key=lambda r: r["Requests"]["id"])[-1:]:
    request = row["Requests"]
    tasks = api(
      "GET",
      CLUSTER
      + "/requests/"
      + str(request["id"])
      + "/tasks?fields=Tasks/status,Tasks/role,Tasks/command,Tasks/stdout,Tasks/stderr",
    )["items"]
    print(
      json.dumps(
        {
          "request": request,
          "tasks": [
            {
              "role": t["Tasks"]["role"],
              "status": t["Tasks"]["status"],
              "stdout_tail": clean(t["Tasks"].get("stdout", "")[-1500:]),
              "stderr_tail": clean(t["Tasks"].get("stderr", "")[-1500:]),
            }
            for t in tasks
          ],
        }
      ),
      flush=True,
    )


def audit_bootstrap_failure():
  rows = api("GET", CLUSTER + "/requests?fields=Requests/id,Requests/request_status")[
    "items"
  ]
  results = []
  for row in rows:
    if row["Requests"]["request_status"] != "FAILED":
      continue
    request_id = row["Requests"]["id"]
    tasks = api(
      "GET",
      CLUSTER
      + "/requests/"
      + str(request_id)
      + "/tasks?fields=Tasks/stderr,Tasks/stdout",
    )["items"]
    for task in tasks:
      text = task["Tasks"].get("stderr", "") + task["Tasks"].get("stdout", "")
      if "bootstrapStandby" in text:
        results.append({"request": request_id, "failure_tail": clean(text[-2600:])})
  print(json.dumps(results), flush=True)


def state_summary():
  props = current_properties("hdfs-site")
  assert props["dfs.nameservices"] == "legacy,warehouse"
  assert props["dfs.internal.nameservices"] == "warehouse"
  assert props["dfs.ha.namenodes.warehouse"] == "primary,backup,third"
  assert props["dfs.ha.namenodes.legacy"] == "east,west"
  metadata = api(
    "GET",
    "/stacks/BIGTOP/versions/3.3.0/services/HDFS/components/NAMENODE?fields=StackServiceComponents/cardinality",
  )
  if metadata["StackServiceComponents"]["cardinality"] != "1-3":
    raise RuntimeError("effective inherited NN cardinality changed")
  for host in HOSTS:
    assert exists(CLUSTER + "/hosts/" + host + "/host_components/NAMENODE")
  print(
    json.dumps(
      {
        "effective_namenode_cardinality": "1-3",
        "namenode_components": 3,
        "nn_ids": props["dfs.ha.namenodes.warehouse"],
        "legacy_namespace_retained": True,
      }
    ),
    flush=True,
  )


if __name__ == "__main__":
  try:
    login()
    if sys.argv[1] == "accept":
      acceptance(sys.argv[2])
    else:
      {
        "probe": probe,
        "bootstrap": bootstrap,
        "repair-fixture": repair_fixture,
        "progress": progress,
        "add-third": add_third,
        "repair-http": repair_http_auth,
        "audit-bootstrap": audit_bootstrap_failure,
        "state-summary": state_summary,
      }[sys.argv[1]]()
  except Exception as error:
    print(clean(str(error)), file=sys.stderr)
    sys.exit(1)
