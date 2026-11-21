(function () {
    "use strict";

    function installCompetitionTabs() {
        var shell = document.querySelector("[data-pool-competition-tab-shell]");
        if (!shell) {
            return;
        }

        var tabList = shell.querySelector("[data-pool-competition-tabs]");
        var panel = shell.querySelector("[data-pool-competition-tab-panel]");
        var tabs = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-tab]")
        );
        if (!tabList || !panel || !tabs.length) {
            return;
        }

        var tabByName = {};
        var tabCache = {};
        var pendingLoads = {};
        var activeCacheKey = null;

        tabs.forEach(function (tab) {
            tabByName[tab.dataset.poolTab] = tab;
        });

        function validTab(tab) {
            return Boolean(tab && tabByName[tab]);
        }

        function currentLocationTab() {
            var requested = new URL(window.location.href).searchParams.get("tab");
            return validTab(requested) ? requested : null;
        }

        function detailsForTab(tab) {
            var directUrl = new URL(tabByName[tab].href, window.location.href);
            var locationUrl = new URL(window.location.href);

            // Search and region filters belong to the Entrants tab. Retain
            // any query values from a bookmarked/history URL for that tab,
            // while ordinary tab clicks keep the link's clean fallback URL.
            if (locationUrl.searchParams.get("tab") === tab) {
                locationUrl.searchParams.forEach(function (value, name) {
                    if (name !== "tab") {
                        directUrl.searchParams.set(name, value);
                    }
                });
            }
            directUrl.searchParams.set("tab", tab);

            var partialUrl = new URL(
                shell.dataset.poolCompetitionTabUrlTemplate.replace(
                    "__tab__",
                    encodeURIComponent(tab)
                ),
                window.location.href
            );
            directUrl.searchParams.forEach(function (value, name) {
                if (name !== "tab") {
                    partialUrl.searchParams.append(name, value);
                }
            });

            return {
                directUrl: directUrl,
                partialUrl: partialUrl,
                cacheKey: tab + "|" + partialUrl.search
            };
        }

        function updateTabs(tab) {
            tabs.forEach(function (candidate) {
                var selected = candidate.dataset.poolTab === tab;
                candidate.classList.toggle("is-active", selected);
                candidate.setAttribute("aria-selected", selected ? "true" : "false");
                candidate.tabIndex = selected ? 0 : -1;
                candidate.toggleAttribute("aria-current", selected);
            });
            panel.id = "pool-panel-" + tab;
            panel.setAttribute("aria-labelledby", "pool-tab-" + tab);
            panel.hidden = false;
            panel.setAttribute("aria-hidden", "false");
        }

        function updateUrl(details, tab, mode) {
            if (mode === "none" || details.directUrl.href === window.location.href) {
                return;
            }
            if (mode === "replace") {
                window.history.replaceState(
                    {poolCompetitionTab: tab},
                    "",
                    details.directUrl
                );
            } else {
                window.history.pushState(
                    {poolCompetitionTab: tab},
                    "",
                    details.directUrl
                );
            }
        }

        function showCachedMarkup(cacheKey) {
            panel.innerHTML = tabCache[cacheKey];
            panel.dataset.loaded = "true";
            panel.removeAttribute("aria-busy");
        }

        function loadPanel(tab, details) {
            var cacheKey = details.cacheKey;
            if (Object.prototype.hasOwnProperty.call(tabCache, cacheKey)) {
                showCachedMarkup(cacheKey);
                return Promise.resolve();
            }
            if (pendingLoads[cacheKey]) {
                return pendingLoads[cacheKey];
            }

            panel.dataset.loaded = "false";
            panel.setAttribute("aria-busy", "true");
            panel.innerHTML = (
                '<p class="pool-section-note" role="status">Loading ' +
                tabByName[tab].textContent.trim().toLowerCase() +
                "…</p>"
            );

            pendingLoads[cacheKey] = window.fetch(details.partialUrl, {
                credentials: "same-origin",
                headers: {
                    "Accept": "text/html",
                    "X-Pool-Competition-Tab": "1"
                }
            })
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to load this competition section.");
                    }
                    return response.text();
                })
                .then(function (markup) {
                    tabCache[cacheKey] = markup;
                    if (activeCacheKey === cacheKey) {
                        showCachedMarkup(cacheKey);
                    }
                })
                .catch(function () {
                    if (activeCacheKey === cacheKey) {
                        panel.removeAttribute("aria-busy");
                        panel.innerHTML = (
                            '<div class="alert alert-danger" role="alert">' +
                            "This section could not be loaded. " +
                            '<a href="' + tabByName[tab].href + '">Open it in its own page</a>.' +
                            "</div>"
                        );
                    }
                })
                .finally(function () {
                    delete pendingLoads[cacheKey];
                });
            return pendingLoads[cacheKey];
        }

        function activate(tab, options) {
            options = options || {};
            if (!validTab(tab)) {
                tab = "overview";
            }
            var details = detailsForTab(tab);
            updateTabs(tab);
            updateUrl(details, tab, options.historyMode || "push");
            // Compute once more after a history update, so a direct entrant
            // link and browser Back/Forward both retain their filters.
            details = detailsForTab(tab);
            activeCacheKey = details.cacheKey;
            loadPanel(tab, details);
            if (options.focusTab) {
                tabByName[tab].focus();
            }
        }

        tabs.forEach(function (tab) {
            tab.addEventListener("click", function (event) {
                if (
                    event.defaultPrevented ||
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                ) {
                    return;
                }
                event.preventDefault();
                activate(tab.dataset.poolTab, {historyMode: "push"});
            });
        });

        tabList.addEventListener("keydown", function (event) {
            var currentIndex = tabs.indexOf(document.activeElement);
            if (currentIndex === -1) {
                return;
            }
            var nextIndex = null;
            if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                nextIndex = (currentIndex + 1) % tabs.length;
            } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
            } else if (event.key === "Home") {
                nextIndex = 0;
            } else if (event.key === "End") {
                nextIndex = tabs.length - 1;
            }
            if (nextIndex === null) {
                return;
            }
            event.preventDefault();
            activate(tabs[nextIndex].dataset.poolTab, {
                historyMode: "push",
                focusTab: true
            });
        });

        window.addEventListener("popstate", function () {
            activate(currentLocationTab() || "overview", {historyMode: "none"});
        });

        var initialTab = currentLocationTab() || shell.dataset.poolCompetitionActiveTab || "overview";
        var initialDetails = detailsForTab(initialTab);
        tabCache[initialDetails.cacheKey] = panel.innerHTML;
        activate(initialTab, {
            historyMode: "none"
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installCompetitionTabs);
    } else {
        installCompetitionTabs();
    }
}());
