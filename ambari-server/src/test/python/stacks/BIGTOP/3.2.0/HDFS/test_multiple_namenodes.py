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

import importlib
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch, call

ROOT = next(
  parent
  for parent in Path(__file__).resolve().parents
  if (parent / "ambari-common").is_dir()
)
SERVICE = ROOT / "ambari-server/src/main/resources/stacks/BIGTOP/3.2.0/services/HDFS"
for relative in (
  "ambari-common/src/main/python",
  "ambari-common/src/main/python/ambari_jinja2",
  "ambari-agent/src/main/python",
):
  sys.path.insert(0, str(ROOT / relative))
sys.path.insert(0, str(SERVICE / "package/scripts"))

from ha_topology import get_local_namenode
from resource_management.libraries.functions.namenode_ha_utils import (
  get_properties_for_all_nameservices,
)
from resource_management.core.environment import Environment
from resource_management.core.exceptions import Fail
import utils


def configuration():
  return {
    "dfs.nameservices": "legacy,warehouse",
    "dfs.internal.nameservices": "warehouse",
    "dfs.ha.namenodes.legacy": "east,west",
    "dfs.namenode.rpc-address.legacy.east": "old-a.example:8020",
    "dfs.namenode.rpc-address.legacy.west": "old-b.example:8020",
    "dfs.ha.namenodes.warehouse": "primary,backup,remote",
    "dfs.namenode.rpc-address.warehouse.primary": "nn1.example:18020",
    "dfs.namenode.rpc-address.warehouse.backup": "nn2.example:18020",
    "dfs.namenode.rpc-address.warehouse.remote": "nn3.example:18020",
  }


class TestLocalIdentity(unittest.TestCase):
  def test_third_namenode_keeps_existing_ids_and_endpoints(self):
    config = configuration()
    before = config.copy()
    self.assertEqual(
      ("warehouse", "remote", "nn3.example:18020"),
      get_local_namenode(config, "nn3.example"),
    )
    self.assertEqual(before, config)

  def test_exact_hostname_does_not_match_a_prefix(self):
    self.assertEqual((None, None, None), get_local_namenode(configuration(), "nn"))

  def test_public_hostname_case_and_trailing_dot(self):
    self.assertEqual(
      "remote", get_local_namenode(configuration(), "private-nn3", "NN3.EXAMPLE.")[1]
    )

  def test_two_namenodes_still_resolve(self):
    config = configuration()
    config["dfs.internal.nameservices"] = "legacy"
    self.assertEqual("west", get_local_namenode(config, "old-b.example")[1])

  def test_internal_nameservices_exclude_unmanaged_aliases(self):
    config = configuration()
    config["dfs.internal.nameservices"] = "warehouse"
    self.assertEqual((None, None, None), get_local_namenode(config, "old-a.example"))

  def test_ambiguous_local_identity_fails(self):
    config = configuration()
    config["dfs.internal.nameservices"] = "legacy,warehouse"
    config["dfs.namenode.rpc-address.legacy.east"] = "nn3.example:8020"
    with self.assertRaises(ValueError):
      get_local_namenode(config, "nn3.example")

  def test_ipv6_and_custom_ids(self):
    config = {
      "dfs.nameservices": "warehouse",
      "dfs.ha.namenodes.warehouse": "a,b,c",
      "dfs.namenode.rpc-address.warehouse.c": "[2001:db8::3]:18020",
    }
    self.assertEqual(
      ("warehouse", "c", "[2001:db8::3]:18020"),
      get_local_namenode(config, "2001:db8::3"),
    )

  def test_shared_edits_scope_matches_params_initialization_map(self):
    config = configuration()
    del config["dfs.internal.nameservices"]
    config["dfs.namenode.shared.edits.dir"] = (
      "qjournal://jn1:8485;jn2:8485;jn3:8485/warehouse"
    )
    namespace, nn_id, _ = get_local_namenode(config, "nn3.example")
    params_map = get_properties_for_all_nameservices(config, "dfs.ha.namenodes")
    self.assertEqual("warehouse", namespace)
    self.assertIn(nn_id, params_map[namespace].split(","))

  def test_default_scope_matches_params_initialization_map(self):
    config = configuration()
    del config["dfs.internal.nameservices"]
    namespace, nn_id, _ = get_local_namenode(config, "old-b.example")
    params_map = get_properties_for_all_nameservices(config, "dfs.ha.namenodes")
    self.assertEqual("legacy", namespace)
    self.assertIn(nn_id, params_map[namespace].split(","))
    self.assertEqual((None, None, None), get_local_namenode(config, "nn3.example"))


class TestRollingFailover(unittest.TestCase):
  def setUp(self):
    self.params = types.ModuleType("params")
    self.params.security_enabled = False
    self.params.hdfs_user = "hdfs"
    self.params.hdfs_site = configuration()
    self.params.namenode_nameservice = "warehouse"
    self.params.namenode_id = "primary"
    self.params.dfs_ha_namemodes_ids_list = ["primary", "backup", "remote"]
    self.params.hostname = "nn1.example"
    self.params.kinit_path_local = "/synthetic/kinit"
    self.params.hdfs_user_keytab = "/synthetic/service.keytab"
    self.params.hdfs_principal_name = "service@EXAMPLE.COM"
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)

  def invoke(self, states, security=False, failover_code=0, confirmation_error=None):
    self.params.security_enabled = security
    with patch.dict(sys.modules, {"params": self.params}), Environment(
      tmp_dir=self.temporary.name, test_mode=True
    ) as env:
      env.set_params(self.params)
      with patch.object(
        utils, "get_namenode_states", return_value=states
      ) as observe, patch.object(
        utils.shell, "call", return_value=(failover_code, "")
      ) as execute, patch.object(
        utils, "Execute", side_effect=confirmation_error
      ) as resource, patch.object(utils, "kill_zkfc") as kill:
        try:
          utils.initiate_safe_zkfc_failover()
        finally:
          kill.assert_not_called()
        observe.assert_called_once_with(
          self.params.hdfs_site, security, "hdfs", name_service="warehouse"
        )
        return execute.call_args_list, resource.call_args_list, kill.call_args_list

  def test_uses_third_namenode_when_second_is_unavailable(self):
    commands, resources, kills = self.invoke(
      ([("primary", "rpc")], [("remote", "rpc")], [("backup", "rpc")])
    )
    self.assertEqual(
      "hdfs haadmin -ns warehouse -failover primary remote", commands[0].args[0]
    )
    self.assertEqual(
      "hdfs haadmin -ns warehouse -getServiceState primary | grep -x standby",
      resources[0].args[0],
    )
    self.assertEqual([], kills)

  def test_two_namenode_handoff_is_preserved(self):
    self.params.dfs_ha_namemodes_ids_list = ["primary", "backup"]
    commands, _, _ = self.invoke(([("primary", "rpc")], [("backup", "rpc")], []))
    self.assertEqual(
      "hdfs haadmin -ns warehouse -failover primary backup", commands[0].args[0]
    )

  def test_selects_configured_order_not_observation_order(self):
    commands, _, _ = self.invoke(
      ([("primary", "rpc")], [("remote", "rpc"), ("backup", "rpc")], [])
    )
    self.assertTrue(commands[0].args[0].endswith("primary backup"))

  def test_standby_restart_does_not_initiate_failover(self):
    self.params.namenode_id = "remote"
    commands, _, _ = self.invoke(
      ([("primary", "rpc")], [("backup", "rpc"), ("remote", "rpc")], [])
    )
    self.assertEqual([], commands)

  def test_active_without_standby_refuses_restart(self):
    with self.assertRaises(Fail):
      self.invoke(([("primary", "rpc")], [], [("backup", "rpc"), ("remote", "rpc")]))

  def test_multiple_active_nodes_are_rejected(self):
    with self.assertRaises(Fail):
      self.invoke(([("primary", "rpc"), ("remote", "rpc")], [("backup", "rpc")], []))

  def test_kerberos_identity_is_acquired_before_handoff(self):
    commands, resources, _ = self.invoke(
      ([("primary", "rpc")], [("remote", "rpc")], []), security=True
    )
    self.assertEqual(
      "/synthetic/kinit -kt /synthetic/service.keytab service@EXAMPLE.COM",
      resources[0].args[0],
    )
    self.assertTrue(commands[0].args[0].endswith("primary remote"))

  def test_no_active_observed_refuses_rolling_stop(self):
    with self.assertRaises(Fail):
      self.invoke(([], [], [("primary", "rpc"), ("backup", "rpc")]))

  def test_local_unknown_with_remote_active_refuses_rolling_stop(self):
    with self.assertRaises(Fail):
      self.invoke(([("remote", "rpc")], [("backup", "rpc")], [("primary", "rpc")]))

  def test_failed_handoff_refuses_stop_without_killing_zkfc(self):
    with self.assertRaises(Fail):
      self.invoke(([("primary", "rpc")], [("remote", "rpc")], []), failover_code=1)

  def test_unconfirmed_standby_refuses_rolling_stop(self):
    with self.assertRaises(Fail):
      self.invoke(
        ([("primary", "rpc")], [("remote", "rpc")], []),
        confirmation_error=Fail("still active"),
      )

  def test_stop_propagates_handoff_failure_before_stopping_namenode(self):
    namenode = importlib.import_module("namenode")
    self.params.dfs_ha_enabled = True
    self.params.dfs_ha_automatic_failover_enabled = True
    with patch.dict(sys.modules, {"params": self.params}), patch.object(
      namenode.NameNode, "get_hdfs_binary", return_value="hdfs"
    ), patch.object(
      namenode, "initiate_safe_zkfc_failover", side_effect=Fail("no available standby")
    ), patch.object(namenode, "namenode") as daemon:
      with self.assertRaises(Fail):
        namenode.NameNode().stop(
          Mock(), upgrade_type=namenode.constants.UPGRADE_TYPE_ROLLING
        )
      daemon.assert_not_called()

  def test_stop_runs_handoff_before_stopping_namenode(self):
    namenode = importlib.import_module("namenode")
    self.params.dfs_ha_enabled = True
    self.params.dfs_ha_automatic_failover_enabled = True
    order = []
    with patch.dict(sys.modules, {"params": self.params}), patch.object(
      namenode.NameNode, "get_hdfs_binary", return_value="hdfs"
    ), patch.object(
      namenode,
      "initiate_safe_zkfc_failover",
      side_effect=lambda: order.append("handoff"),
    ), patch.object(
      namenode, "namenode", side_effect=lambda **kwargs: order.append(kwargs["action"])
    ):
      namenode.NameNode().stop(
        Mock(), upgrade_type=namenode.constants.UPGRADE_TYPE_ROLLING
      )
    self.assertEqual(["handoff", "stop"], order)


class TestComponentCommands(unittest.TestCase):
  def test_refresh_is_registered_in_inherited_stack(self):
    info = ET.parse(SERVICE / "metainfo.xml").getroot()
    components = {c.findtext("name"): c for c in info.findall(".//component")}
    self.assertEqual("1-3", components["NAMENODE"].findtext("cardinality"))
    commands = components["DATANODE"].findall("./customCommands/customCommand")
    self.assertEqual(
      ["scripts/datanode.py"],
      [
        c.findtext("commandScript/script")
        for c in commands
        if c.findtext("name") == "REFRESH_NAMENODES"
      ],
    )
    stack = ET.parse(SERVICE.parents[2] / "3.3.0/metainfo.xml").getroot()
    self.assertEqual("3.2.0", stack.findtext("extends"))

  def test_datanode_refresh_uses_service_identity_without_formatting(self):
    datanode = importlib.import_module("datanode")
    params = types.ModuleType("params")
    params.security_enabled = True
    params.hdfs_user = "hdfs"
    params.dn_kinit_cmd = "/synthetic/kinit-dn"
    params.dfs_dn_ipc_address = "dn.example:9867"
    with patch.dict(sys.modules, {"params": params}), patch.object(
      datanode, "Execute"
    ) as execute, patch.object(
      datanode.DataNode, "get_hdfs_binary", return_value="hdfs"
    ), patch.object(
      datanode,
      "get_dfsadmin_base_command",
      return_value="hdfs dfsadmin -fs hdfs://warehouse",
    ):
      datanode.DataNode().refresh_namenodes(Mock())
    self.assertEqual(
      [
        call("/synthetic/kinit-dn", user="hdfs"),
        call(
          "hdfs dfsadmin -fs hdfs://warehouse -refreshNamenodes dn.example:9867",
          user="hdfs",
          logoutput=True,
        ),
      ],
      execute.call_args_list,
    )

  def test_legacy_refresh_command_is_registered_and_delegates(self):
    info = ET.parse(SERVICE / "metainfo.xml").getroot()
    dn = next(c for c in info.findall(".//component") if c.findtext("name") == "DATANODE")
    aliases = [c for c in dn.findall("./customCommands/customCommand") if c.findtext("name") == "REFRESH_NAMENODE"]
    self.assertEqual(1, len(aliases))
    self.assertEqual("true", aliases[0].findtext("hidden"))
    datanode = importlib.import_module("datanode")
    env = Mock()
    with patch.object(datanode.DataNode, "refresh_namenodes") as canonical:
      datanode.DataNode().refresh_namenode(env)
    canonical.assert_called_once_with(env)

  def test_bootstrap_never_formats_or_forces_existing_state(self):
    namenode = importlib.import_module("namenode")
    params = types.ModuleType("params")
    params.security_enabled = True
    params.nn_kinit_cmd = "/synthetic/kinit-nn"
    params.hdfs_user = "hdfs"
    with patch.dict(sys.modules, {"params": params}), patch.object(
      namenode, "Execute"
    ) as execute:
      namenode.NameNode().bootstrap_standby(Mock())
    self.assertEqual(
      [
        call("/synthetic/kinit-nn", user="hdfs"),
        call(
          "hdfs namenode -bootstrapStandby -nonInteractive", user="hdfs", logoutput=True
        ),
      ],
      execute.call_args_list,
    )


if __name__ == "__main__":
  unittest.main()
