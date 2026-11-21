(function () {
    "use strict";

    var FILTERS = ["upcoming", "live", "completed", "all"];

    function isPlainPrimaryClick(event) {
        return !(
            event.defaultPrevented
            || event.button !== 0
            || event.metaKey
            || event.ctrlKey
            || event.shiftKey
            || event.altKey
        );
    }

    function installFixtureFilters(shell) {
        if (shell.dataset.poolMatchFiltersReady === "true") {
            return;
        }
        shell.dataset.poolMatchFiltersReady = "true";

        var controls = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-match-filter]")
        );
        var rows = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-match-row]")
        );
        var emptyState = shell.querySelector("[data-pool-match-empty]");
        var statusMessage = shell.querySelector(
            "[data-pool-match-filter-status]"
        );
        var defaultFilter = shell.dataset.poolMatchFilterDefault || "upcoming";

        function validFilter(value) {
            return FILTERS.indexOf(value) !== -1 ? value : defaultFilter;
        }

        function selectedFromLocation() {
            return validFilter(
                new URL(window.location.href).searchParams.get("status")
            );
        }

        function render(filterName) {
            var visibleCount = 0;
            rows.forEach(function (row) {
                var visible = filterName === "all"
                    || row.dataset.matchStatus === filterName;
                row.hidden = !visible;
                if (visible) {
                    visibleCount += 1;
                }
            });

            controls.forEach(function (control) {
                var selected = control.dataset.poolMatchFilter === filterName;
                control.classList.toggle("is-active", selected);
                if (selected) {
                    control.setAttribute("aria-current", "page");
                } else {
                    control.removeAttribute("aria-current");
                }
            });

            if (emptyState) {
                emptyState.hidden = visibleCount !== 0;
            }
            if (statusMessage) {
                statusMessage.textContent = visibleCount
                    ? "Showing " + visibleCount + " fixture"
                        + (visibleCount === 1 ? "." : "s.")
                    : "No fixtures match this filter.";
            }
        }

        function updateLocation(filterName) {
            var url = new URL(window.location.href);
            url.searchParams.set("status", filterName);
            if (url.href !== window.location.href) {
                window.history.pushState(
                    {poolMatchFilter: filterName},
                    "",
                    url
                );
            }
        }

        controls.forEach(function (control) {
            control.addEventListener("click", function (event) {
                if (!isPlainPrimaryClick(event)) {
                    return;
                }
                event.preventDefault();
                var filterName = control.dataset.poolMatchFilter;
                render(filterName);
                updateLocation(filterName);
            });
        });

        window.addEventListener("popstate", function () {
            render(selectedFromLocation());
        });

        // The direct-link server response is already filtered for users
        // without JavaScript. Reapply it defensively once enhancement loads.
        render(selectedFromLocation());
    }

    function installAllFixtureFilters() {
        document.querySelectorAll("[data-pool-match-filters]").forEach(
            installFixtureFilters
        );
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installAllFixtureFilters);
    } else {
        installAllFixtureFilters();
    }
}());
