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

# Third NameNode: unified review branch

This branch makes our Ambari 3.0.0 adaptation available for discussion with the
original author and maintainers of [AMBARI-26300 / PR #4038](https://github.com/apache/ambari/pull/4038).
It is a source review branch, not a new competing pull request or a claim of
completed production qualification.

## Attribution and exact base

- The original multiple-Standby Web wizard is by **AnanyaSingh2121** (`ananysin`),
  from PR #4038, commit `c53c5861a8369d6a77e87f382bd99d19f6a7cf32`.
- Our contribution adapts that wizard to Ambari 3.0.0, combines it with the
  three-NameNode backend, and adds the topology, configuration-drift and
  execution-order changes described below. Original ASF headers are retained.
- The exact base is Apache Ambari tag `release-3.0.0`, commit
  `15feafb7dfa2f4964a01edac47c0956ab29ad99e`.
- This is the source candidate prepared on 2026-10-30, exported as actual source
  files rather than an opaque binary overlay. Its original source-patch SHA-256
  is `3b74e1b5c687f2f7a267eec2353da00d0a6dd92e8acabd4b6d8b6719c62099e4`. Publication adds attribution and review documentation and
  expands license headers; runtime behavior is unchanged from that candidate.
- The manual **Switch Active NameNode** menu from
  [PR #4257](https://github.com/apache/ambari/pull/4257) is a separate contribution
  and is not included in this branch.

## Changes relative to the original proposal

| Area | Original PR #4038 | Unified candidate |
| --- | --- | --- |
| BIGTOP metadata | Does not change the inherited NN cardinality | Allows 1–3 NameNodes; this wizard only adds the third to an existing HA pair |
| Review page | `onLoadConfigs` submits a desired `hdfs-site` configuration while loading the review page | Review is read-only; writes are an execution task |
| Nameservice / identity | Uses the complete `dfs.nameservices` value and constructs sequential `nn1..N` IDs | Resolves effective scope consistently with the backend, preserves existing IDs/endpoints and chooses a non-colliding new ID |
| Addresses | Assumes generic endpoint properties and colon-separated ports | Handles per-NN ports, IPv6 and auxiliary service/lifeline endpoints |
| Configuration changes | No comparison with the reviewed `hdfs-site` tag | Rechecks the tag before component installation and before saving; rejects drift |
| Execution order | Includes safemode/saveNamespace, a separate ZKFC format, and starts ZKFC before NN/refresh completion | Configures the new NN before bootstrap; removes those safemode/saveNamespace and separate ZKFC format steps; starts ZKFC after DataNode/client configuration delivery |
| DataNode / clients | Singular refresh command and initial refresh workflow | Registered plural refresh command, singular compatibility alias, explicit client host list |
| Rolling backend | Wizard proposal does not provide the nameservice-scoped three-NN rolling handoff implementation | Selects an available Standby in configured order, refuses unconfirmed handoff and does not kill ZKFC as a fallback |
| Tests | Manual cluster test described in PR | Focused backend/UI regressions plus a synthetic integration lab and recorded validation boundaries |

Relevant implementation entry points:

- `ambari-web/app/controllers/main/admin/highAvailability/nameNode/multipleNN/step3_controller.js`
- `ambari-web/app/controllers/main/admin/highAvailability/nameNode/multipleNN/step4_controller.js`
- `ambari-web/app/utils/multiple_nn_topology.js`
- `ambari-server/src/main/resources/stacks/BIGTOP/3.2.0/services/HDFS/package/scripts/ha_topology.py`
- `ambari-server/src/main/resources/stacks/BIGTOP/3.2.0/services/HDFS/package/scripts/utils.py`

## Validation and limits

Fresh checks on this public source checkout:

```sh
python3 -m unittest discover \
  -s ambari-server/src/test/python/stacks/BIGTOP/3.2.0/HDFS \
  -p test_multiple_namenodes.py -v
node ambari-web/test/standalone/multiple_nn_unified_test.js
python3 -m unittest discover \
  -s dev-support/hdfs-three-nn-lab -p test_pinned_fencer.py -v
cd ambari-web && ./node_modules/.bin/brunch build
```

Backend and UI tests passed: **26 backend tests and 15 UI tests**. Python 3.9
used the isolated dependencies listed in the [operator document](hdfs-multiple-namenodes.md#validation).
The three fence-guard tests also passed, and the Ember Web build compiled
1025 files successfully. See [publication validation](hdfs-three-nn-community-validation.json). No infrastructure operations are performed by
these focused tests or the Web build.

The original candidate was installed on a test cluster on 2026-10-30. Stack API,
served Web bundle, agent delivery, a real DataNode refresh and authenticated
HDFS listing were checked. The third NN already existed before installation;
that smoke test does not prove an end-to-end 2→3 wizard run.

Earlier **backend v2** integration exercised native Ambari/API addition,
Kerberos, bootstrap, registration, selected rolling handoff and synthetic
fencing scenarios. Those are historical results of that backend candidate,
not a fresh integration run of the unified Web wizard.

The full interactive unified wizard, loaded-configuration convergence on old
NN/ZKFC daemons, unified rolling handoff, transition during continuous writes,
full Maven suite and production qualification are not established by this
publication. Configuration-tag checks are preflight checks, not atomic CAS.
Ordinary STOP behavior outside the handled rolling path is unchanged.

See [operator workflow and recovery](hdfs-multiple-namenodes.md) and the
[isolated integration lab](../dev-support/hdfs-three-nn-lab/README.md).
