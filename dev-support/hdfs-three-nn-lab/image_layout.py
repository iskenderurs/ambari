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

"""Install paths inside the disposable container image, never on the host."""
import os
import pathlib
import shutil
import subprocess
import sys


def link(source, target):
  target = pathlib.Path(target)
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.is_symlink():
    target.unlink()
  elif target.exists():
    return
  target.symlink_to(source)


def control():
  for path in (
    "/var/run/ambari-server",
    "/var/run/ambari-agent",
    "/var/log/ambari-server",
    "/var/log/ambari-agent",
    "/var/lib/ambari-server/keys",
    "/var/lib/ambari-agent/keys",
    "/etc/ambari-server/conf",
    "/lab",
  ):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
  properties = pathlib.Path("/etc/ambari-server/conf/ambari.properties")
  properties.write_text(properties.read_text().replace("$ROOT", ""))
  link("/usr/bin/python3", "/usr/bin/ambari-python-wrap")
  link("/etc/init.d/ambari-server", "/usr/sbin/ambari-server")
  link("/var/lib/ambari-agent/bin/ambari-agent", "/usr/sbin/ambari-agent")
  for user in ("hdfs", "zookeeper", "ambari-qa"):
    subprocess.run(
      ["useradd", "--create-home", "--shell", "/bin/bash", user], check=True
    )
  subprocess.run(["groupadd", "hadoop"], check=True)
  for user in ("hdfs", "zookeeper", "ambari-qa"):
    subprocess.run(["usermod", "--append", "--groups", "hadoop", user], check=True)


def node():
  base = pathlib.Path("/usr/bigtop/3.3.0/usr/lib")
  base.mkdir(parents=True, exist_ok=True)
  shutil.move("/opt/hadoop-3.3.6", base / "hadoop")
  shutil.move("/opt/apache-zookeeper-3.7.2-bin", base / "zookeeper")
  for package in ("hadoop-hdfs", "hadoop-yarn", "hadoop-mapreduce"):
    directory = base / package
    directory.mkdir()
    for child in (base / "hadoop").iterdir():
      if child.name != "conf":
        link(str(child), directory / child.name)
  for package in ("hadoop", "zookeeper"):
    directory = pathlib.Path("/etc") / package / "conf"
    directory.mkdir(parents=True, exist_ok=True)
    previous = base / package / "conf"
    if previous.is_dir() and not previous.is_symlink():
      shutil.copytree(previous, directory, dirs_exist_ok=True)
      shutil.move(previous, previous.with_name("conf.factory"))
    link(str(directory), previous)
  for directory in ("bin", "sbin"):
    destination = base.parent / directory
    destination.mkdir(exist_ok=True)
    for command in (base / "hadoop" / directory).iterdir():
      link(str(command), destination / command.name)
  for package in (
    "hadoop-client",
    "hadoop-hdfs-namenode",
    "hadoop-hdfs-datanode",
    "hadoop-hdfs-journalnode",
    "hadoop-hdfs-zkfc",
    "zookeeper-client",
    "zookeeper-server",
  ):
    subprocess.run(
      ["python3", "/usr/lib/bigtop-select/distro-select", "set", package, "3.3.0"],
      check=True,
    )
  link("/usr/lib/bigtop-select/distro-select", "/usr/bin/distro-select")
  link("/usr/lib/bigtop-select/conf-select", "/usr/bin/conf-select")
  link("/usr/lib/bigtop-select/distro-select", "/usr/bin/bigtop-select")


if __name__ == "__main__":
  {"control": control, "node": node}[sys.argv[1]]()
