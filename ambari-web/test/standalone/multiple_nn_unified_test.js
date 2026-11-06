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
'use strict';
var assert = require('assert'), fs = require('fs'), vm = require('vm'), path = require('path');
var root = path.resolve(__dirname, '../..');
var topology = require(root + '/app/utils/multiple_nn_topology');
var count = 0;
function test(name, fn) { fn(); count++; console.log('PASS ' + name); }
function config() {
  return {'dfs.ha.automatic-failover.enabled': 'true', 'dfs.nameservices': 'legacy,amba', 'dfs.internal.nameservices': 'amba',
    'dfs.ha.namenodes.amba': 'primary,backup',
    'dfs.namenode.rpc-address.amba.primary': 'a.example:18020',
    'dfs.namenode.rpc-address.amba.backup': 'b.example:18020',
    'dfs.namenode.http-address.amba.primary': 'a.example:9870',
    'dfs.namenode.https-address.amba.primary': 'a.example:9871'};
}
function plan(p) { return topology(p, ['a.example', 'b.example'], 'c.example'); }
test('preserve custom IDs, ports and original properties', function () {
  var p = config(), before = JSON.stringify(p), result = plan(p);
  assert.equal(result.listNameNodes, 'primary,backup,nn1');
  assert.equal(result.nnHttpPort, '9870'); assert.equal(result.nnRpcPort, '18020');
  assert.equal(JSON.stringify(p), before);
});
test('avoid ID collision with nonsequential existing IDs and orphan address', function () {
  var p = config(); p['dfs.ha.namenodes.amba'] = 'nn1,nn3';
  p['dfs.namenode.rpc-address.amba.nn1'] = 'a.example:8020';
  p['dfs.namenode.rpc-address.amba.nn3'] = 'b.example:8020';
  p['dfs.namenode.http-address.amba.nn2'] = 'old.example:9870';
  assert.equal(plan(p).newNamenodeIndex, 'nn4');
});
test('select matching namespace without rewriting unrelated legacy namespace', function () {
  var p = config(); delete p['dfs.internal.nameservices'];
  p['dfs.namenode.shared.edits.dir'] = 'qjournal://jn1:8485;jn2:8485;jn3:8485/amba';
  assert.equal(plan(p).namespaceId, 'amba');
});
test('default nameservice scope agrees with backend', function () {
  var p = config(); delete p['dfs.internal.nameservices'];
  assert.throws(function () { plan(p); }, /uniquely/);
});
test('manual HA does not silently acquire ZKFC', function () {
  var p = config(); p['dfs.ha.automatic-failover.enabled'] = 'false';
  assert.throws(function () { plan(p); }, /automatic failover/);
});
test('reject ambiguous namespace', function () {
  var p = config(); p['dfs.internal.nameservices'] = 'amba,other';
  p['dfs.ha.namenodes.other'] = 'x,y';
  p['dfs.namenode.rpc-address.other.x'] = 'a.example:8020';
  p['dfs.namenode.rpc-address.other.y'] = 'b.example:8020';
  assert.throws(function () { plan(p); }, /uniquely/);
});
test('reject hostname substring match', function () {
  var p = config(); p['dfs.namenode.rpc-address.amba.primary'] = 'prefix-a.example:8020';
  assert.throws(function () { plan(p); });
});
test('normalize hostname case and trailing dot', function () {
  assert.equal(topology(config(), ['A.EXAMPLE.', 'b.example'], 'c.example').namespaceId, 'amba');
});
test('reject fourth node and existing host reuse', function () {
  assert.throws(function () { topology(config(), ['a.example', 'b.example', 'c.example'], 'd.example'); });
  assert.throws(function () { topology(config(), ['a.example', 'b.example'], 'A.EXAMPLE.'); });
});
test('preserve auxiliary RPC ports', function () {
  var p = config(); p['dfs.namenode.servicerpc-address.amba.primary'] = 'a.example:8022';
  assert.deepEqual(plan(p).extraAddresses, [{name: 'dfs.namenode.servicerpc-address.amba.nn1', value: 'c.example:8022'}]);
});
test('parse IPv6 endpoints', function () {
  var p = config(); p['dfs.namenode.rpc-address.amba.primary'] = '[2001:db8::1]:8020';
  assert.equal(topology(p, ['2001:db8::1', 'b.example'], 'c.example').namespaceId, 'amba');
});
test('reject invalid RPC port', function () {
  var p = config(); p['dfs.namenode.rpc-address.amba.primary'] = 'a.example:99999';
  assert.throws(function () { plan(p); }, /endpoint/);
});
var controller, calls = [];
var app = {HighAvailabilityProgressPageController: {extend: function (_, value) { controller = value; }},
  WizardEnableDone: {}, ajax: {send: function (request) { calls.push(request); }}};
var sandbox = {require: function () { return app; }, Em: {I18n: {t: function (v) { return v; }}}};
vm.createContext(sandbox); vm.runInContext('Function.prototype.property = function () { return this; };', sandbox);
vm.runInContext(fs.readFileSync(root + '/app/controllers/main/admin/highAvailability/nameNode/multipleNN/step4_controller.js', 'utf8'), sandbox);
var reviewed = {items: {findProperty: function () { return {tag: 'reviewed'}; }}};
var state = {get: function () { return reviewed; }, set: function () {},
  configurationMatchesReview: controller.configurationMatchesReview,
  onTaskError: function () { this.failed = true; }, onTaskCompleted: function () { this.complete = true; }};
test('changed configuration prevents all config writes', function () {
  calls = []; state.failed = false;
  controller.saveReviewedConfiguration.call(state, {Clusters: {desired_configs: {'hdfs-site': {tag: 'newer'}}}});
  assert.equal(state.failed, true); assert.equal(calls.length, 0);
});
test('unchanged configuration passes validation', function () {
  controller.onValidateConfiguration.call(state, {Clusters: {desired_configs: {'hdfs-site': {tag: 'reviewed'}}}});
  assert.equal(state.complete, true);
});
test('configure precedes bootstrap, election follows DN refresh and client delivery', function () {
  var sequence = controller.commands;
  assert.equal(sequence[0], 'validateConfiguration');
  assert(sequence.indexOf('configureNameNode') < sequence.indexOf('bootstrapNameNode'));
  assert(sequence.indexOf('startZKFC') > sequence.indexOf('refreshNamenodes'));
  assert(sequence.indexOf('startZKFC') > sequence.indexOf('refreshClients'));
  assert.equal(sequence.indexOf('formatZKFC'), -1);
});
console.log(count + ' tests passed');
