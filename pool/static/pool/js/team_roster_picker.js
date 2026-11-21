(() => {
    const picker = document.getElementById("pool-roster-player-picker");
    if (!picker) {
        return;
    }

    const form = picker.closest("form");
    const searchInput = document.getElementById("pool-roster-player-search");
    const regionInput = document.getElementById("pool-roster-player-region");
    const results = document.getElementById("pool-roster-player-results");
    const selectedList = document.getElementById("pool-roster-selected");
    const selectedCount = document.getElementById("pool-roster-selected-count");
    const submitLabel = document.getElementById("pool-roster-submit-label");
    const baseInput = document.getElementById(picker.dataset.baseInputId);
    const selectedPlayers = new Map();
    let debounceTimer;
    let activeRequest;
    let currentResults = [];

    try {
        const initial = JSON.parse(
            document.getElementById("pool-roster-selected-players-data").textContent
        );
        initial.forEach((player) => selectedPlayers.set(String(player.id), player));
    } catch (_error) {
        // The server remains authoritative if an optional restore payload is malformed.
    }

    function labelFor(player) {
        let label = player.full_name;
        if (player.nickname) {
            label += ` ("${player.nickname}")`;
        }
        if (player.region) {
            label += ` — ${player.region}`;
        }
        return label;
    }

    function syncBasket() {
        form.querySelectorAll("input[data-pool-roster-player-id]").forEach((input) => input.remove());
        if (baseInput) {
            baseInput.value = "";
        }
        selectedList.replaceChildren();
        const players = Array.from(selectedPlayers.values());
        players.forEach((player) => {
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = picker.dataset.playerField;
            hidden.value = String(player.id);
            hidden.dataset.poolRosterPlayerId = String(player.id);
            form.appendChild(hidden);

            const item = document.createElement("div");
            item.className = "list-group-item d-flex flex-wrap align-items-center justify-content-between gap-2";
            const name = document.createElement("strong");
            name.textContent = labelFor(player);
            item.appendChild(name);
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn-sm btn-outline-secondary";
            remove.textContent = "Remove";
            remove.addEventListener("click", () => {
                selectedPlayers.delete(String(player.id));
                syncBasket();
                renderResults(currentResults);
            });
            item.appendChild(remove);
            selectedList.appendChild(item);
        });
        selectedCount.textContent = players.length
            ? `${players.length} ${players.length === 1 ? "player" : "players"} selected.`
            : "No players selected.";
        submitLabel.textContent = players.length
            ? `Add ${players.length} ${players.length === 1 ? "player" : "players"}`
            : "Add selected players";
    }

    function showNotice(message) {
        results.replaceChildren();
        if (!message) {
            return;
        }
        const notice = document.createElement("p");
        notice.className = "pool-manager-help mb-0";
        notice.textContent = message;
        results.appendChild(notice);
    }

    function renderResults(players) {
        currentResults = players;
        results.replaceChildren();
        if (!players.length) {
            showNotice("No active players matched that search.");
            return;
        }
        players.forEach((player) => {
            const selected = selectedPlayers.has(String(player.id));
            const button = document.createElement("button");
            button.type = "button";
            button.className = "list-group-item list-group-item-action";
            button.setAttribute("role", "option");
            button.disabled = selected || Boolean(player.unavailable);
            if (player.unavailable) {
                button.textContent = `${labelFor(player)} — ${player.unavailable_reason}`;
            } else {
                button.textContent = selected ? `Added: ${labelFor(player)}` : `+ ${labelFor(player)}`;
            }
            if (!button.disabled) {
                button.addEventListener("click", () => {
                    selectedPlayers.set(String(player.id), player);
                    syncBasket();
                    renderResults(players);
                });
            }
            results.appendChild(button);
        });
    }

    function searchPlayers() {
        const query = searchInput.value.trim();
        if (!query) {
            currentResults = [];
            showNotice("");
            return;
        }
        if (activeRequest) {
            activeRequest.abort();
        }
        activeRequest = new AbortController();
        const url = new URL(picker.dataset.searchUrl, window.location.origin);
        url.searchParams.set("q", query);
        if (regionInput.value.trim()) {
            url.searchParams.set("region", regionInput.value.trim());
        }
        fetch(url, { signal: activeRequest.signal, headers: { Accept: "application/json" } })
            .then((response) => {
                if (!response.ok) {
                    throw new Error("Roster search failed.");
                }
                return response.json();
            })
            .then((payload) => renderResults(payload.results || []))
            .catch((error) => {
                if (error.name !== "AbortError") {
                    showNotice("Player search is temporarily unavailable.");
                }
            });
    }

    function queueSearch() {
        window.clearTimeout(debounceTimer);
        debounceTimer = window.setTimeout(searchPlayers, 180);
    }

    searchInput.addEventListener("input", queueSearch);
    regionInput.addEventListener("input", queueSearch);
    syncBasket();
})();
