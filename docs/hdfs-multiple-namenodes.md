<!--
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
-->

# Adding a third NameNode to an existing BIGTOP HA nameservice

This change supports the backend path for adding a third NameNode to an existing Linux BIGTOP HA cluster. BIGTOP 3.3.0 inherits the HDFS metadata and scripts from 3.2.0. The NameNode component cardinality is 1-3. Existing NameNode IDs, addresses and nameservices remain operator-owned configuration.

The workflow uses the existing Ambari component install, `BOOTSTRAP_STANDBY`, start and stop operations. The DataNode custom command `REFRESH_NAMENODES` is registered in stack metadata and invokes `dfsadmin -refreshNamenodes` after configuration delivery. It does not deliver the configuration itself.

The unified candidate combines this backend with an adapted Web wizard from [AMBARI-26300 / PR 4038](https://github.com/apache/ambari/pull/4038). The UI stages addition of a standby; loaded configuration convergence and full HA acceptance remain operator steps. It does not add automatic three-NameNode greenfield Blueprint deployment or change the Web HA/disable-HA wizards. Those paths retain their existing two-node assumptions and must not be used to rewrite a three-node setup without separate validation. Windows and additional federated NameNode pairs are outside this change.

## Identity and restart behavior

The local NameNode is resolved by an exact RPC hostname match, including its public hostname alias. Effective nameservices follow the existing shared helper: internal selection first, then the shared-edits namespace, then the first declared nameservice. This is the same scope used for parameter initialization. Substrings cannot select another NameNode. The helper preserves custom IDs, ports and the internal nameservice selection; it rejects ambiguous local identities.

During a rolling stop, HA state is queried only for the local nameservice. If the local NameNode is Active, the script chooses an available Standby in configured ID order. It can select the third NameNode when the second is unavailable. Multiple observed Actives or no available Standby for an Active stop cause the operation to fail before the daemon is stopped. An ordinary stop outside the rolling-upgrade path retains its existing behavior.

Failed graceful handoff aborts the rolling stop. The script does not kill ZKFC as a fallback. The local NN must be observed as Standby before an Active handoff is considered complete; unknown local state or no single observed Active also aborts the rolling stop. This does not certify production fencing or prevent a concurrent topology change; the operation owner must keep maintenance serialized and verify one Active.

## Prerequisites

- An existing healthy HA nameservice and accepted new host, with an assigned NameNode directory that contains no unrelated namespace.
- An explicit third NN ID and reviewed RPC, HTTP/HTTPS and service endpoints. Preserve the existing two IDs and all unrelated nameservices.
- Kerberos provisioning for the new NN, its HTTP service and ZKFC through the normal credential owner. Do not copy an existing host's private identity.
- Working fencing, ZooKeeper ACLs, journal connectivity, metadata backups and a tested restore path.
- Capacity and heap sizing checked for all three NN hosts. Existing advisor recommendations that inspect only the first two hosts do not establish the third host's capacity.
- One owner for configuration, Ambari component operations and the complete maintenance window. Review pending desired configuration before any restart.

## Installation boundary

The runtime change consists of the HDFS `metainfo.xml`, `datanode.py`, `params_linux.py`, `utils.py` and new `ha_topology.py`. Distribute these through the owning server/stack package path and verify delivery to agents before component operations. A manifest must bind each replacement to the expected prior bytes, and the new module must not overwrite an unrelated file.

Preserve other local package modifications. Replacing an entire Ambari RPM can replace unrelated stack fixes; an overlay candidate is not authorization for installation. Back up the affected files, Ambari metadata/database and ownership before any separately approved deployment. Reload/restart Ambari as required for stack metadata, then read back the effective cardinality and custom command before adding the component. A file copied to the server does not prove that its cached stack metadata or an agent's command package was refreshed.

## Operator sequence

1. Freeze the exact target host and nameservice, all three IDs, endpoints, configuration tags and backups. Capture current Active/Standby state and a control dataset. Stop writers for the reviewed NN-list transition.
2. Deliver the reviewed NN list and endpoint configuration consistently to NN hosts, DataNodes and required clients. Use a planned NN stop/start window so no running NN retains the old two-ID configuration. This transition does not change QJM membership by itself.
3. Install the new NameNode, HDFS client and ZKFC components through Ambari. Installation/configuration must use the new host's assigned identity and the final reviewed NN list.
4. Start the existing NN pair with the consistent configuration and confirm one Active, one Standby, unchanged namespace identity and normal edits progression. Keep automatic election of an unbootstrapped third NN disabled by leaving its ZKFC stopped.
5. Request the existing `BOOTSTRAP_STANDBY` command on the new NN host only. Its command is `hdfs namenode -bootstrapStandby -nonInteractive`. Do not send NameNode `FORMAT`, `-initializeSharedEdits` or `-force` for this existing namespace. A bootstrap refusal on a nonempty directory needs inspection, not a forced retry.
6. Start the third NN and verify edits catch-up and block reports. Then start its ZKFC against the existing HA namespace. Do not request a separate ZooKeeper `FORMAT` operation. The stock non-forced initialization helper may encounter an already initialized namespace; its handling and ACLs need integration acceptance.
7. After delivering the NN-list configuration to each DataNode, request `REFRESH_NAMENODES` on those exact DataNode hosts. The command obtains the configured DN Kerberos identity when security is enabled. Verify actual registration with all three NN; command success alone is insufficient.
8. Deliver and activate reviewed client configuration through its owner. Verify the long-lived client's logical-nameservice read and new block write, rather than only `getconf` output.
9. Accept controlled failover to each NN and rolling stop/restart with one unavailable Standby. Confirm single Active, new writes, existing data and consistent identity after each case. Open writers only after the agreed acceptance boundary.

Example custom-command request shape, using placeholders:

```json
{
  "RequestInfo": {
    "command": "REFRESH_NAMENODES",
    "context": "Register DataNode with reviewed NameNode list"
  },
  "Requests/resource_filters": [{
    "service_name": "HDFS",
    "component_name": "DATANODE",
    "hosts": "<approved-datanode>"
  }]
}
```

## Rollback

Keep original files and configuration generations. Before the third NN has served as Active, rollback still requires a controlled NN-list transition: stop its ZKFC and NN, verify one surviving Active, restore the reviewed two-ID configuration consistently and refresh DataNodes/clients. Remove the third Ambari component before restoring a two-NN cardinality. Preserve its metadata for inspection.

After the third NN has accepted namespace writes, rollback must use the latest shared journal and synchronized metadata. Do not restore an old image or journal snapshot simply to return to two NN. Stop writers, fence the departing Active, catch up the retained NN pair and verify the current transaction boundary. Configuration rollback does not undo acknowledged edits.

## Validation

Use Python 3.9 or 3.10 and isolated test dependencies `distro==1.9.0`, `mock==5.2.0`, `six==1.17.0` and `requests==2.32.3`, then run from the checkout root:

```sh
python3 -m unittest discover \
  -s ambari-server/src/test/python/stacks/BIGTOP/3.2.0/HDFS \
  -p test_multiple_namenodes.py -v
```

The focused tests import actual stack modules and resource-management helpers. They replace process execution, HA observations and Kerberos acquisition with synthetic boundaries. They check exact identity resolution, custom IDs and IPv6, two-NN compatibility, nameservice-scoped state queries, available third-NN selection, fail-closed handoff, stop ordering, command registration and non-forced bootstrap. These tests do not start Ambari server/agent, a KDC or HDFS daemons.

The [isolated integration harness](../dev-support/hdfs-three-nn-lab/README.md) exercises the source-built Ambari server and agents with Hadoop 3.3.6, a real MIT KDC, SPNEGO, HTTPS and three synthetic hosts. The original backend v2 acceptance (not a replay of this unified candidate) passed for adding a third NN to a nonempty two-NN namespace, bootstrap refusal/retry with unavailable source NN, DataNode refresh and registration with all three NN. Namespace identity, existing contents, new block writes and the unrelated legacy nameservice were retained.

In that original backend v2 run, the native rolling restart handed off to the third NN when the second was unavailable. With no available Standby, the rolling stop failed and the Active JVM PID remained unchanged. One authenticated `FileSystem` instance continued reading and writing across these cases. A frozen Active and unavailable SSH fencer prevented a replacement Active and a new write; restoring SSH allowed the fencer to terminate the old JVM before recovery. Restarting Ambari and all agents retained the three-NN configuration and all NN PIDs. The synthetic dataset passed `fsck` with RF=3, no missing replicas and no corrupt blocks. Cached runtime-module hashes matched the reviewed source on all agents.

This acceptance uses `packages.pre.installed=true`, raw Apache distributions and explicit container layout/PID adapters. It does not qualify RPM installation, the Web wizard, production ZooKeeper ACLs, production fencing/identity custody, WAN behavior or DataNode loss during an active write pipeline. The SSH fencer is restricted to synthetic endpoints and must not be promoted as a production profile. Deployment still requires a separate review of the installed package modifications, backups, delivered/loaded configuration and rollback.
