/*
 * View model for BufferBuddy
 *
 * Author: chendo - updated by fflosi
 * License: AGPLv3
 */
$(function() {
    function BufferBuddyViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        self.status = ko.observable('Initialising...');
        self.state = ko.observable('initialising');
        self.enabled = ko.observable(false);
        self.advancedOkDetected = ko.observable(false);

        self.plannerBufferSize = ko.observable(0);
        self.commandBufferSize = ko.observable(0);
        self.inflightTarget = ko.observable(0);

        // Stats of the current or last job, cleared when the next one starts
        self.hasStats = ko.observable(false);
        self.linesPerSecond = ko.observable(0);
        self.inflight = ko.observable(0);
        self.plannerQueued = ko.observable(0);
        self.plannerQueuedAvg = ko.observable(null);
        self.resendsDetected = ko.observable(0);
        self.ctsTriggered = ko.observable(0);

        // Throughput, inflight and the current planner fill go stale once the job ends, so they only show during one.
        // The planner doesn't take part in an upload to SD.
        self.jobActive = ko.pureComputed(function () {
            return self.state() === 'printing' || self.state() === 'transferring';
        });
        self.printing = ko.pureComputed(function () {
            return self.state() === 'printing';
        });

        self.setStats = function (stats) {
            self.hasStats(!!stats);
            if (!stats) {
                return;
            }
            self.linesPerSecond(stats.lines_per_second);
            self.inflight(stats.inflight);
            self.plannerQueued(stats.planner_queued);
            self.plannerQueuedAvg(stats.planner_queued_avg);
            self.resendsDetected(stats.resends_detected);
            self.ctsTriggered(stats.cts_triggered);
        };

        self.setState = function (config) {
            self.status(config.status);
            self.state(config.state);
            self.enabled(config.enabled);
            self.advancedOkDetected(config.advanced_ok_detected);
            self.plannerBufferSize(config.planner_buffer_size);
            self.commandBufferSize(config.command_buffer_size);
            self.inflightTarget(config.inflight_target);
            self.setStats(config.stats);
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "buffer_buddy") {
                return;
            }

            if (data.type === 'update') {
                self.setStats(data.message);
            } else if (data.type === 'status') {
                self.status(data.message);
            } else if (data.type === 'state') {
                self.setState(data.message);
            }
        };

        self.requestData = function () {
            OctoPrint.plugins.base.get(OctoPrint.plugins.base.getSimpleApiUrl("buffer_buddy"))
                .done(function (response) {
                    self.setState(response.state);
                });
        };

        self.onStartup = self.onUserLoggedIn = self.onUserLoggedOut = self.onEventSettingsUpdated = function() {
            self.requestData();
        };

        self.openSettings = function () {
            $('a#navbar_show_settings').click();
            $('li#settings_plugin_buffer_buddy_link a').click();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BufferBuddyViewModel,
        dependencies: [ "settingsViewModel" ],
        elements: [ "#sidebar_plugin_buffer_buddy" ]
    });
});
