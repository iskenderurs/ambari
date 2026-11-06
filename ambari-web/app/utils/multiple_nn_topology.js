/**
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements. See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 * http://www.apache.org/licenses/LICENSE-2.0
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

function list(value) {
  return (value || '').split(',').map(function (v) { return v.trim(); }).filter(Boolean);
}
function host(value) { return value.toLowerCase().replace(/\.$/, ''); }
function endpoint(value) {
  var match = /^(\[[^\]]+\]|[^:]+):(\d+)$/.exec(value || '');
  if (!match || +match[2] < 1 || +match[2] > 65535) {
    throw new Error('Invalid or missing NameNode endpoint: ' + value);
  }
  return {host: host(match[1].replace(/^\[|\]$/g, '')), port: match[2]};
}

module.exports = function (properties, installedHosts, newHost) {
  if (installedHosts.length !== 2 || !newHost || installedHosts.some(function (h) {
    return host(h) === host(newHost);
  })) {
    throw new Error('Select one new host for an existing pair of NameNodes.');
  }
  if (String(properties['dfs.ha.automatic-failover.enabled']) !== 'true') {
    throw new Error('This wizard requires an existing HA nameservice with automatic failover enabled.');
  }
  // Same effective scope as the backend get_nameservices helper.
  var candidates = list(properties['dfs.internal.nameservices']);
  if (!candidates.length) {
    var declared = list(properties['dfs.nameservices']);
    var shared = properties['dfs.namenode.shared.edits.dir'];
    var selected = declared.filter(function (ns) {
      return shared && new RegExp('.*' + ns + '$').test(shared);
    });
    candidates = selected.length ? selected.slice(0, 1) : declared.slice(0, 1);
  }
  var matches = candidates.filter(function (ns) {
    var ids = list(properties['dfs.ha.namenodes.' + ns]);
    if (ids.length !== 2 || ids[0] === ids[1]) { return false; }
    var hosts = ids.map(function (id) {
      return endpoint(properties['dfs.namenode.rpc-address.' + ns + '.' + id]).host;
    });
    return hosts[0] !== hosts[1] && installedHosts.every(function (h) { return hosts.indexOf(host(h)) >= 0; });
  });
  if (matches.length !== 1) {
    throw new Error('Cannot uniquely match the existing NameNode pair to a nameservice.');
  }
  var ns = matches[0], ids = list(properties['dfs.ha.namenodes.' + ns]);
  var index = 1, newId;
  do {
    newId = 'nn' + index++;
  } while (ids.indexOf(newId) >= 0 || Object.keys(properties).some(function (key) {
    return key.indexOf('dfs.namenode.') === 0 && key.slice(-(ns + '.' + newId).length) === ns + '.' + newId;
  }));
  var result = {namespaceId: ns, newNamenodeIndex: newId, newNameNode: newHost.indexOf(':') >= 0 && newHost.charAt(0) !== '[' ? '[' + newHost + ']' : newHost,
    listNameNodes: ids.concat([newId]).join(','), extraAddresses: []};
  [['rpc', 'nnRpcPort'], ['http', 'nnHttpPort'], ['https', 'nnHttpsPort']].forEach(function (entry) {
    var address = properties['dfs.namenode.' + entry[0] + '-address.' + ns + '.' + ids[0]] || properties['dfs.namenode.' + entry[0] + '-address'];
    if (!address && entry[0] !== 'rpc') {
      result[entry[1]] = entry[0] === 'http' ? '50070' : '50470';
    } else { result[entry[1]] = endpoint(address).port; }
  });
  ['servicerpc', 'lifeline.rpc'].forEach(function (type) {
    var prefix = 'dfs.namenode.' + type + '-address.' + ns + '.';
    var address = properties[prefix + ids[0]];
    if (address) {
      result.extraAddresses.push({name: prefix + newId, value: result.newNameNode + ':' + endpoint(address).port});
    }
  });
  return result;
};
