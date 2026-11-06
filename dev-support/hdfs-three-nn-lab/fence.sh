#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Apache-2.0. This fencer is restricted to synthetic lab endpoints.
set -euo pipefail
umask 077
case "${target_host:-}" in
  nn1.geo.test|nn2.geo.test|nn3.geo.test) ;;
  *) exit 1 ;;
esac
[[ "${target_port:-}" == 8020 ]] || exit 1
if ssh -i /home/hdfs/.ssh/id_rsa -o BatchMode=yes \
  -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null "hdfs@$target_host" \
  'fuser -k -n tcp 8020 >/dev/null 2>&1 || true; ! fuser -n tcp 8020 >/dev/null 2>&1'; then
  printf '%s success\n' "$target_host" >> /var/tmp/geo-fence.events
else
  printf '%s failed\n' "$target_host" >> /var/tmp/geo-fence.events
  exit 1
fi
