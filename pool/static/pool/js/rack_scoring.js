(function () {
    "use strict";

    function setSaveStatus(form, message, state) {
        var element = form.querySelector("[data-pool-rack-save-status]");
        if (!element) {
            return;
        }
        element.hidden = false;
        element.textContent = message;
        element.classList.toggle("is-error", state === "error");
        element.classList.toggle("is-success", state === "success");
    }

    function disableControls(form, disabled) {
        Array.prototype.slice.call(
            form.querySelectorAll('button, input[type="submit"]')
        ).forEach(function (control) {
            control.disabled = disabled;
        });
    }

    function updateAuthoritativeScore(payload) {
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-home-score]")
        ).forEach(function (element) {
            element.textContent = payload.home_score;
        });
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-away-score]")
        ).forEach(function (element) {
            element.textContent = payload.away_score;
        });
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-race-target]")
        ).forEach(function (element) {
            element.textContent = payload.race_target;
        });
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-score-status]")
        ).forEach(function (element) {
            element.textContent = payload.status_label;
            element.className = "pool-status pool-status--" + String(payload.status).toLowerCase();
        });
    }

    function updateTeamSchedule(payload) {
        if (!payload.scheduled_rack) {
            return;
        }
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-team-live-rack]")
        ).forEach(function (container) {
            if (String(container.dataset.poolTeamLiveRack) !== String(payload.scheduled_rack.id)) {
                return;
            }
            var status = container.querySelector(".pool-status");
            if (status) {
                status.textContent = payload.scheduled_rack.status_label;
                status.className = "pool-status pool-status--" + String(payload.scheduled_rack.status).toLowerCase();
            }
            container.classList.add("is-completed");
        });
        var next = document.querySelector("[data-pool-next-scheduled-rack]");
        if (next && payload.next_scheduled_rack) {
            next.hidden = false;
            next.textContent = (
                "Next scheduled rack: #" + payload.next_scheduled_rack.sequence_number + "."
            );
        }
    }

    function updateLatestRack(payload) {
        if (!payload.latest_rack) {
            return;
        }
        Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-latest-rack]")
        ).forEach(function (element) {
            element.hidden = false;
            element.textContent = (
                "Latest rack: #" + payload.latest_rack.number +
                " — " + (payload.latest_rack.winning_player || payload.latest_rack.winner) +
                "."
            );
        });
    }

    function submitRack(form, isTeam) {
        if (!window.fetch || !window.FormData) {
            return;
        }
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            if (form.dataset.poolRackSaving === "true") {
                return;
            }
            form.dataset.poolRackSaving = "true";
            disableControls(form, true);
            setSaveStatus(form, "Saving…", "saving");

            window.fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Pool-Scoring": "1"
                }
            })
                .then(function (response) {
                    return response.json().catch(function () {
                        return {ok: false, error: "The scorer returned an unreadable response."};
                    }).then(function (payload) {
                        if (!response.ok || !payload.ok) {
                            throw new Error(payload.error || "The rack could not be saved.");
                        }
                        return payload;
                    });
                })
                .then(function (payload) {
                    updateAuthoritativeScore(payload);
                    updateTeamSchedule(payload);
                    updateLatestRack(payload);
                    if (!isTeam && payload.next_request_token) {
                        var tokenField = form.querySelector('input[name$="request_token"]');
                        form.reset();
                        if (tokenField) {
                            tokenField.value = payload.next_request_token;
                        }
                    }
                    var latest = payload.latest_rack
                        ? "Rack " + payload.latest_rack.number + " saved."
                        : "Rack saved.";
                    if (payload.finished) {
                        latest += " The match is complete.";
                    }
                    setSaveStatus(form, latest, "success");
                    if (!payload.finished && !isTeam) {
                        disableControls(form, false);
                    }
                })
                .catch(function (error) {
                    // The original action token stays in the form. Retrying
                    // after a lost response is safe: the server returns the
                    // already-recorded rack instead of creating another one.
                    setSaveStatus(
                        form,
                        error.message || "Could not confirm the rack. Retry safely once connected.",
                        "error"
                    );
                    disableControls(form, false);
                })
                .finally(function () {
                    delete form.dataset.poolRackSaving;
                });
        });
    }

    document.querySelectorAll("[data-pool-rack-score-form]").forEach(function (form) {
        submitRack(form, false);
    });
    document.querySelectorAll("[data-pool-team-rack-score-form]").forEach(function (form) {
        submitRack(form, true);
    });
}());
