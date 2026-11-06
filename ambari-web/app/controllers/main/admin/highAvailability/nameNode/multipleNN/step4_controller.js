/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

var App = require('app');

App.MultipleNameNodeWizardStep4Controller = App.HighAvailabilityProgressPageController.extend(App.WizardEnableDone, {

    name: "multipleNameNodeWizardStep4Controller",

    commands: ['validateConfiguration', 'installNameNode', 'installZKFC', 'reconfigureServices', 'configureNameNode', 'bootstrapNameNode', 'startNameNode', 'refreshConfigs', 'refreshNamenodes', 'refreshClients', 'startZKFC'],

    tasksMessagesPrefix: 'admin.multipleNameNode.wizard.step',

    initializeTasks: function initializeTasks() {
        this._super();
    },

    newNameNodeHosts: function () {
        return this.get('content.masterComponentHosts').filterProperty('component', 'NAMENODE').filterProperty('isInstalled', false).mapProperty('hostName');
    }.property('content.masterComponentHosts.@each.hostName'),

    oldNameNodeHosts: function () {
        return this.get('content.masterComponentHosts').filterProperty('component', 'NAMENODE').filterProperty('isInstalled', true).mapProperty('hostName');
    }.property('content.masterComponentHosts.@each.hostName'),

    configurationMatchesReview: function (data) {
        var reviewed = this.get('content.serviceConfigProperties').items.findProperty('type', 'hdfs-site');
        return !!reviewed.tag && data.Clusters.desired_configs['hdfs-site'].tag === reviewed.tag;
    },

    validateConfiguration: function () {
        return App.ajax.send({name: 'config.tags', sender: this,
            success: 'onValidateConfiguration', error: 'onTaskError'});
    },

    onValidateConfiguration: function (data) {
        if (!this.configurationMatchesReview(data)) {
            this.set('configError', 'HDFS configuration changed since review. Stop and review the current configuration before retrying.');
            return this.onTaskError();
        }
        this.onTaskCompleted();
    },

    reconfigureServices: function () {
        return App.ajax.send({name: 'config.tags', sender: this,
            success: 'saveReviewedConfiguration', error: 'onTaskError'});
    },

    saveReviewedConfiguration: function (current) {
        if (!this.configurationMatchesReview(current)) {
            this.set('configError', 'HDFS configuration changed during installation. Review it before saving.');
            return this.onTaskError();
        }
        var configs = [];
        var data = this.get('content.serviceConfigProperties');
        var note = Em.I18n.t('admin.multipleNameNode.wizard.step4.save.configuration.note');
        configs.push({
            Clusters: {
                desired_config: this.reconfigureSites(['hdfs-site'], data, note)
            }
        });
        return App.ajax.send({
            name: 'common.service.multiConfigurations',
            sender: this,
            data: {
                configs: configs
            },
            success: 'onSaveConfigs',
            error: 'onTaskError'
        });
    },

    onSaveConfigs: function () {
        this.onTaskCompleted();
    },

    installHDFSClients: function installHDFSClients() {
        var nnHostNames = this.get('content.masterComponentHosts').filterProperty('component', 'NAMENODE').mapProperty('hostName');
        this.createInstallComponentTask('HDFS_CLIENT', nnHostNames, 'HDFS');
    },

    installNameNode: function installNameNode() {
        this.createInstallComponentTask('NAMENODE', this.get('newNameNodeHosts'), "HDFS");
    },

    installZKFC: function installZKFC() {
        this.createInstallComponentTask('ZKFC', this.get('newNameNodeHosts'), "HDFS");
    },

    startZKFC: function () {
        this.updateComponent('ZKFC', this.get('newNameNodeHosts')[0], "HDFS", "Start");
    },

    startRangerAdmin: function () {
        var hostNames = App.HostComponent.find().filterProperty('componentName', 'RANGER_ADMIN').mapProperty('hostName');
        this.updateComponent('RANGER_ADMIN', hostNames, "RANGER", "Start");
    },

    startRangerUsersync: function () {
        var hostNames = App.HostComponent.find().filterProperty('componentName', 'RANGER_USERSYNC').mapProperty('hostName');
        this.updateComponent('RANGER_USERSYNC', hostNames, "RANGER", "Start");
    },

    startNameNode: function () {
        this.updateComponent('NAMENODE', this.get('newNameNodeHosts')[0], "HDFS", "Start");
    },

    configureNameNode: function () {
        return App.ajax.send({
            name: 'host.host_component.refresh_configs',
            sender: this,
            data: {
                resource_filters: [{
                    service_name: 'HDFS',
                    component_name: 'NAMENODE',
                    hosts: this.get('newNameNodeHosts').join(',')
                }],
                context: 'Write additional NameNode configuration before bootstrap'
            },
            success: 'startPolling',
            error: 'onTaskError'
        });
    },

    bootstrapNameNode: function bootstrapNameNode() {
        App.ajax.send({
            name: 'nameNode.federation.bootstrapNameNode',
            sender: this,
            data: {
                host: this.get('newNameNodeHosts')[0]
            },
            success: 'startPolling',
            error: 'onTaskError'
        });
    },

    refreshConfigs: function () {
        var allDatanodeHosts = App.SlaveComponent.find('DATANODE').get('hostNames');
        var resource_filters = [
            {
                "service_name" : "HDFS",
                "component_name" : "DATANODE",
                "hosts": allDatanodeHosts.join(",")
            }
        ];
        App.ajax.send({
            name: 'host.host_component.refresh_configs',
            sender: this,
            data: {
                resource_filters: resource_filters,
                context: "refresh configs"
            },
            success: 'startPolling',
            error: 'onTaskError'
        });
    },

    refreshClients: function () {
        var hosts = App.HostComponent.find().filterProperty('componentName', 'HDFS_CLIENT').mapProperty('hostName');
        if (!hosts.length) { return this.onTaskError(); }
        return App.ajax.send({
            name: 'host.host_component.refresh_configs', sender: this,
            data: {resource_filters: [{service_name: 'HDFS', component_name: 'HDFS_CLIENT', hosts: hosts.join(',')}],
                context: 'Distribute three-NameNode configuration to HDFS clients'},
            success: 'startPolling', error: 'onTaskError'
        });
    },

    refreshNamenodes: function () {
        var allDatanodeHosts = App.SlaveComponent.find('DATANODE').get('hostNames');
        App.ajax.send({
            name: 'multipleNameNode.refreshNamenodes',
            sender: this,
            data: {
                host: allDatanodeHosts
            },
            success: 'startPolling',
            error: 'onTaskError'
        });
    }
});
