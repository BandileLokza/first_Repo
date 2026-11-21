(function () {
    "use strict";

    var FILTER_VALUES = {
        category: ["leagues", "tournaments", "money_matches"],
        mode: ["singles", "teams"],
        status: ["active", "upcoming", "finished", "all"]
    };

    function includes(values, value) {
        return values.indexOf(value) !== -1;
    }

    function installHomeFilters() {
        var shell = document.querySelector("[data-pool-home-filters]");
        if (!shell) {
            return;
        }

        var cards = Array.prototype.slice.call(
            document.querySelectorAll("[data-pool-home-card]")
        );
        var controls = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-home-filter]")
        );
        var clearControl = shell.querySelector("[data-pool-home-clear]");
        var emptyState = document.querySelector("[data-pool-home-empty]");
        var statusMessage = document.querySelector(
            "[data-pool-home-filter-status]"
        );

        function valueFor(url, key) {
            var value = url.searchParams.get(key);
            return includes(FILTER_VALUES[key], value) ? value : "";
        }

        function matchesCategoryAndMode(card, state) {
            return (
                (!state.category || card.dataset.category === state.category)
                && (!state.mode || card.dataset.mode === state.mode)
            );
        }

        function defaultStatus(state) {
            return cards.some(function (card) {
                return (
                    matchesCategoryAndMode(card, state)
                    && card.dataset.status === "active"
                );
            }) ? "active" : "upcoming";
        }

        function stateFromUrl(url) {
            var state = {
                category: valueFor(url, "category"),
                mode: valueFor(url, "mode"),
                status: valueFor(url, "status"),
                hasRequestedFilters: ["category", "mode", "status"].some(
                    function (key) {
                        return url.searchParams.has(key);
                    }
                )
            };

            if (!state.status) {
                // This mirrors the server's useful default after category and
                // participant-mode filters have been applied.
                state.status = defaultStatus(state);
            }
            return state;
        }

        function cardMatches(card, state) {
            return (
                matchesCategoryAndMode(card, state)
                && (
                    state.status === "all"
                    || card.dataset.status === state.status
                )
            );
        }

        function filterUrlForState(state) {
            var url = new URL(window.location.href);

            ["category", "mode"].forEach(function (key) {
                if (state[key]) {
                    url.searchParams.set(key, state[key]);
                } else {
                    url.searchParams.delete(key);
                }
            });
            // A click makes the selected/default status explicit, matching
            // the existing progressive-enhancement hrefs.
            url.searchParams.set("status", state.status);
            return url;
        }

        function updateLocation(url, state) {
            if (url.href === window.location.href) {
                return;
            }
            window.history.pushState(
                {poolHomeFilters: state},
                "",
                url.href
            );
        }

        function render(state) {
            var visibleCount = 0;

            cards.forEach(function (card) {
                var visible = cardMatches(card, state);
                card.hidden = !visible;
                if (visible) {
                    visibleCount += 1;
                }
            });

            controls.forEach(function (control) {
                var selected = (
                    control.dataset.poolHomeFilterValue
                    === state[control.dataset.poolHomeFilterKey]
                );
                control.classList.toggle("is-active", selected);
                if (selected) {
                    control.setAttribute("aria-current", "page");
                } else {
                    control.removeAttribute("aria-current");
                }
            });

            if (clearControl) {
                clearControl.hidden = !state.hasRequestedFilters;
            }
            if (emptyState) {
                emptyState.hidden = visibleCount !== 0;
            }
            if (statusMessage) {
                statusMessage.textContent = visibleCount
                    ? "Showing " + visibleCount + " competition"
                        + (visibleCount === 1 ? "." : "s.")
                    : "No competitions match these filters.";
            }
        }

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

        controls.forEach(function (control) {
            control.addEventListener("click", function (event) {
                if (!isPlainPrimaryClick(event)) {
                    return;
                }

                event.preventDefault();
                var state = stateFromUrl(new URL(window.location.href));
                state[control.dataset.poolHomeFilterKey] = (
                    control.dataset.poolHomeFilterValue
                );
                state.hasRequestedFilters = true;
                var url = filterUrlForState(state);
                render(state);
                updateLocation(url, state);
            });
        });

        if (clearControl) {
            clearControl.addEventListener("click", function (event) {
                if (!isPlainPrimaryClick(event)) {
                    return;
                }

                event.preventDefault();
                var url = new URL(window.location.href);
                ["category", "mode", "status"].forEach(function (key) {
                    url.searchParams.delete(key);
                });
                var state = stateFromUrl(url);
                render(state);
                updateLocation(url, state);
            });
        }

        window.addEventListener("popstate", function () {
            render(stateFromUrl(new URL(window.location.href)));
        });

        // The initial server response is already correct for direct URLs and
        // no-JavaScript users.  Reapply the same state to protect against a
        // cached DOM or a malformed query parameter drifting from it.
        render(stateFromUrl(new URL(window.location.href)));
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installHomeFilters);
    } else {
        installHomeFilters();
    }
}());
