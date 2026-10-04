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
        self.inflightAvg = ko.observable(null);
        self.resendsDetected = ko.observable(0);
        self.limitedBy = ko.observable(null);
        self.limitedByText = ko.pureComputed(function () {
            return {
                rx_buffer: '(held by the RX buffer)',
                command_buffer: '(held by the command buffer)',
                target: '(at the target, ' + self.inflightTarget() + ')'
            }[self.limitedBy()] || '';
        });
        self.lastStatsAt = 0;

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
            self.inflightAvg(stats.inflight_avg === undefined ? null : stats.inflight_avg); // not in stats kept from 0.2.0
            self.resendsDetected(stats.resends_detected);
            self.limitedBy(stats.limited_by);
            self.lastStatsAt = Date.now();
        };

        // Stats only arrive with oks, so while the printer sits on a long command (heating) nothing updates them
        setInterval(function () {
            if (self.jobActive() && Date.now() - self.lastStatsAt > 3000) {
                self.linesPerSecond(0);
            }
        }, 1000);

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

        // The API needs a logged in user. This also fires on page load when one already is, and saved settings
        // arrive as a state message.
        self.onUserLoggedIn = self.requestData;

        self.openSettings = function () {
            self.settingsViewModel.show("settings_plugin_buffer_buddy");
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BufferBuddyViewModel,
        dependencies: [ "settingsViewModel" ],
        elements: [ "#sidebar_plugin_buffer_buddy" ]
    });
});
