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

# Three-NameNode backend integration lab

This lab starts source-built Ambari server/agents, PostgreSQL, a real MIT KDC and Hadoop 3.3.6. Three synthetic hosts each run a DataNode, JournalNode and ZooKeeper. It first establishes two NameNodes, writes a control dataset and then adds a third through native Ambari API operations.

The generated Docker network is internal. No ports are published, no Docker socket is mounted, and no container is privileged. The driver verifies its project and ownership labels before selecting containers. All identities, credentials, certificates and data belong to this lab. Generated state remains in the ignored build tree; do not publish it.

## Scope

The harness tests the inherited BIGTOP 3.3.0 metadata and the Linux backend scripts. `packages.pre.installed=true` skips package installation. Hadoop and ZooKeeper archives must match their Apache SHA-512 checksums. The Bigtop selectors are pinned in [selector-sources.json](selector-sources.json) and checked before execution.

The raw Hadoop tarball needs package-layout adapters: BIGTOP paths, command wrappers, canonical synthetic hostnames and the secure DataNode PID naming contract. These adapters leave the reviewed HDFS stack modules unchanged. This profile does not qualify production RPM installation or existing package modifications.

HDFS RPC, QJM and HTTP/SPNEGO use real Kerberos identities. ZooKeeper ACL migration and production identity custody are outside this profile. The lab SSH fencer checks a strict synthetic host/port allowlist, kills the process listening on the NN RPC port and checks that the port is closed. Its private-network SSH host-key settings are unsuitable for production.

## Build inputs

Run commands from the repository root. Requirements are Docker Compose, Python 3.9 or newer, JDK 17 and Maven 3.9.16. Allow approximately 8 GiB for Docker and sufficient space for source artifacts and Hadoop archives. The container images use Linux amd64, including on an arm64 host.

Place the verified Apache Maven distribution under `target/integration-tools/apache-maven-3.9.16`. The Maven wrapper uses an explicit public Central mirror, selects Java 17 on macOS and generates the required Azure DDL into build output without changing source resources.

```sh
dev-support/hdfs-three-nn-lab/maven.sh -B -ntp \
  -pl ambari-server,ambari-agent -am \
  -Dmaven.repo.local=target/integration-m2 \
  -Dskip=true -DskipTests -DskipPythonTests -DskipSurefireTests \
  -DskipAdminWebTests -Dskip.installnodenpm=true -Dskip.npm=true \
  -DassemblyPhase=package -Dcheckstyle.skip -Dspotbugs.skip -Drat.skip=true \
  package
```

These flags build API-only lab artifacts; they do not replace the [focused runtime tests](../../docs/hdfs-multiple-namenodes.md#validation) or qualify the Web UI.

Populate `target/integration-downloads` with:

- Apache `hadoop-3.3.6.tar.gz` and its `.sha512` file.
- Apache `apache-zookeeper-3.7.2-bin.tar.gz` and its `.sha512` file.
- `distro-select` and `conf-select` from the URLs in the selector manifest.

Build the base image and preload the PostgreSQL 16 image before starting the internal network:

```sh
docker build --platform linux/amd64 \
  -f dev-support/hdfs-three-nn-lab/Dockerfile.base \
  -t hdfs-three-nn-base:20261029 dev-support/hdfs-three-nn-lab
docker pull postgres:16
python3 dev-support/hdfs-three-nn-lab/lab.py prepare
python3 dev-support/hdfs-three-nn-lab/lab.py build-control
python3 dev-support/hdfs-three-nn-lab/lab.py build-nodes
```

Compile `RetainedClient.java` for Java 8 using the `hadoop-common-3.3.6.jar` and `hadoop-hdfs-client-3.3.6.jar` from the verified Hadoop distribution as the compiler classpath. Put the resulting `RetainedClient.class` in `target/integration-tools/client-compile`. The runtime uses the full installed Hadoop classpath.

## Acceptance sequence

Use a new lab state directory. `prepare` refuses to overwrite an existing checkpoint. Keep writers stopped during the NN-list transition; start the retained client after the third NN has been accepted. This is a phase-driven development harness. Its final clean-start sequence has not yet been replayed as an unattended CI gate.

```sh
python3 dev-support/hdfs-three-nn-lab/lab.py up-control
python3 dev-support/hdfs-three-nn-lab/lab.py up-nodes
python3 dev-support/hdfs-three-nn-lab/lab.py bootstrap
python3 dev-support/hdfs-three-nn-lab/lab.py baseline-check
python3 dev-support/hdfs-three-nn-lab/lab.py add-third
python3 dev-support/hdfs-three-nn-lab/lab.py data-check
python3 dev-support/hdfs-three-nn-lab/lab.py client-start
python3 dev-support/hdfs-three-nn-lab/lab.py accept
python3 dev-support/hdfs-three-nn-lab/lab.py fence-test
python3 dev-support/hdfs-three-nn-lab/lab.py restart-proof
python3 dev-support/hdfs-three-nn-lab/lab.py final-checks
python3 dev-support/hdfs-three-nn-lab/lab.py data-check
python3 dev-support/hdfs-three-nn-lab/lab.py candidate-audit
python3 dev-support/hdfs-three-nn-lab/lab.py snapshot
python3 dev-support/hdfs-three-nn-lab/lab.py client-stop
python3 dev-support/hdfs-three-nn-lab/lab.py stop
```

The acceptance checks bootstrap failure/retry without format/force, one Active and two Standby, all DN registrations, unchanged namespace identity and control-data hashes, and new block writes. It tests rolling handoff to the third NN with another NN unavailable, refusal to stop the sole Active, actual failed/successful fencing, and control-plane restarts. A single Kerberos-authenticated `FileSystem` instance remains alive across the fault tests. Temporary unavailability while fencing cannot complete is expected; it must not become a successful unsafe write.

`final-checks` requires a healthy `fsck` and rejection of a client without a Kerberos ticket. `candidate-audit` compares actual agent cache modules with this checkout. `snapshot` verifies the running isolation boundary. Aggregate receipts remain private. `stop` preserves containers and volumes for inspection; it does not prune or delete resources.

Use `progress`, `state-summary`, `fence-progress`, `server-errors` and `ha-debug` for bounded diagnostics. The `repair-*` commands are development aids for fixture setup, not production recovery commands. If a phase fails, inspect that phase before retrying; do not force format, replace the KDC or recreate populated node containers.

## Separate HA security candidate

`pinned_fencer.py` and `SaslAclProbe.java` support separate qualification of SSH fencing and SASL ACL migration. The baseline acceptance sequence above does not install or qualify this profile.

The candidate fencer requires root-managed target mappings and host pins, a dedicated SSH identity, and a forced remote command. It validates nameservice, NN ID, host, RPC port, namespace VERSION hash, process UID and JVM class, and ownership of the RPC listener. Linux pidfd support pins the process during termination. SSH/authentication errors and timeouts return failure. This remains a candidate helper, without production packaging, credential provisioning or a rollout entrypoint. SSH fencing requires reachability to the old host; independent site isolation needs a separate mechanism.

The ACL probe checks real SASL identity, targeted version-guarded migration with ZKFC stopped, denial for unauthorized clients, and ACLs on persistent and ephemeral HA nodes. Principal normalization must be qualified against the deployment's realm trust and monitoring identities. Do not apply the synthetic ACL identity or modify a shared parent without verifying its ownership.

Run the focused local guard checks with `python3 -m unittest discover -s dev-support/hdfs-three-nn-lab -p test_pinned_fencer.py`. These checks mock SSH and do not replace real failed/successful fencing and client read/write tests in an isolated Kerberos-enabled cluster.
