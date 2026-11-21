(function () {
    "use strict";

    function installManagerTabs() {
        var shell = document.querySelector("[data-pool-manager-tab-shell]");
        if (!shell) {
            return;
        }

        var tabList = shell.querySelector("[data-pool-manager-tablist]");
        var tabs = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-manager-tab]")
        );
        var panels = Array.prototype.slice.call(
            shell.querySelectorAll("[data-pool-manager-tab-panel]")
        );
        var tabByName = {};
        var panelByName = {};
        var pendingLoads = {};

        tabs.forEach(function (tab) {
            tabByName[tab.dataset.poolManagerTab] = tab;
        });
        panels.forEach(function (panel) {
            panelByName[panel.dataset.poolManagerTabPanel] = panel;
        });

        function validTab(tab) {
            return Boolean(tab && tabByName[tab] && panelByName[tab]);
        }

        function currentLocationTab() {
            var requested = new URL(window.location.href).searchParams.get("tab");
            return validTab(requested) ? requested : null;
        }

        function replaceEmbeddedScripts(container) {
            Array.prototype.slice.call(container.querySelectorAll("script")).forEach(
                function (oldScript) {
                    var replacement = document.createElement("script");
                    Array.prototype.slice.call(oldScript.attributes).forEach(function (attribute) {
                        replacement.setAttribute(attribute.name, attribute.value);
                    });
                    replacement.text = oldScript.text || oldScript.textContent || "";
                    oldScript.parentNode.replaceChild(replacement, oldScript);
                }
            );
        }

        function prepareLoadedPanel(panel, tab) {
            var sourceUrl = tabByName[tab].href;
            Array.prototype.slice.call(panel.querySelectorAll("form")).forEach(
                function (form) {
                    if (!form.getAttribute("action")) {
                        form.setAttribute("action", sourceUrl);
                    }
                }
            );
            replaceEmbeddedScripts(panel);
        }

        function loadPanel(tab) {
            var panel = panelByName[tab];
            if (panel.dataset.loaded === "true") {
                return Promise.resolve();
            }
            if (pendingLoads[tab]) {
                return pendingLoads[tab];
            }

            var partialUrl = shell.dataset.poolManagerPartialUrl.replace(
                "__tab__",
                encodeURIComponent(tab)
            );
            panel.setAttribute("aria-busy", "true");
            panel.innerHTML = '<p class="pool-manager-help" role="status">Loading section…</p>';
            pendingLoads[tab] = window.fetch(partialUrl, {
                credentials: "same-origin",
                headers: {
                    "Accept": "text/html",
                    "X-Pool-Manager-Tab": "1"
                }
            })
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to load this management section.");
                    }
                    return response.text();
                })
                .then(function (markup) {
                    panel.innerHTML = markup;
                    panel.dataset.loaded = "true";
                    prepareLoadedPanel(panel, tab);
                })
                .catch(function () {
                    panel.innerHTML = (
                        '<div class="alert alert-danger" role="alert">' +
                        "This section could not be loaded. " +
                        '<a href="' + tabByName[tab].href + '">Open it in its own page</a>.' +
                        "</div>"
                    );
                })
                .finally(function () {
                    panel.removeAttribute("aria-busy");
                    delete pendingLoads[tab];
                });
            return pendingLoads[tab];
        }

        function updateUrl(tab, mode) {
            if (mode === "none") {
                return;
            }
            var url = new URL(window.location.href);
            if (tab === "overview") {
                url.searchParams.delete("tab");
            } else {
                url.searchParams.set("tab", tab);
            }
            window.history[mode === "replace" ? "replaceState" : "pushState"](
                {poolManagerTab: tab},
                "",
                url
            );
        }

        function activate(tab, options) {
            options = options || {};
            if (!validTab(tab)) {
                tab = "overview";
            }
            tabs.forEach(function (candidate) {
                var selected = candidate.dataset.poolManagerTab === tab;
                candidate.classList.toggle("is-active", selected);
                candidate.setAttribute("aria-selected", selected ? "true" : "false");
                candidate.tabIndex = selected ? 0 : -1;
            });
            panels.forEach(function (candidate) {
                var selected = candidate.dataset.poolManagerTabPanel === tab;
                candidate.hidden = !selected;
                candidate.setAttribute("aria-hidden", selected ? "false" : "true");
            });
            updateUrl(tab, options.historyMode || "push");
            loadPanel(tab);
            if (options.focusTab) {
                tabByName[tab].focus();
            }
        }

        tabs.forEach(function (tab) {
            tab.addEventListener("click", function (event) {
                if (
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                ) {
                    return;
                }
                event.preventDefault();
                activate(tab.dataset.poolManagerTab, {focusTab: false});
            });
        });

        if (tabList) {
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
                activate(tabs[nextIndex].dataset.poolManagerTab, {focusTab: true});
            });
        }

        window.addEventListener("popstate", function () {
            activate(
                currentLocationTab() || shell.dataset.poolManagerActiveTab || "overview",
                {historyMode: "none", focusTab: false}
            );
        });

        // A bookmarked dashboard URL such as ``?tab=fixtures`` must win over
        // the server-rendered fallback panel.  The fallback remains useful
        // for a malformed/missing query parameter and for no-JavaScript use.
        var initialTab = currentLocationTab();
        if (!validTab(initialTab)) {
            initialTab = shell.dataset.poolManagerActiveTab;
        }
        activate(initialTab, {historyMode: "replace", focusTab: false});
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installManagerTabs);
    } else {
        installManagerTabs();
    }
}());
