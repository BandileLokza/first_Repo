(() => {
    const link = document.querySelector("[data-history-back]");
    if (!link) return;

    const navigation = window.navigation?.currentEntry ? window.navigation : null;
    const fallbackKey = "esportsliveBackFallback";
    let pending = false;
    let retryTimer;

    if (!navigation) {
        try {
            const replacement = sessionStorage.getItem(fallbackKey);
            sessionStorage.removeItem(fallbackKey);
            if (replacement === window.location.href) {
                history.replaceState({ ...history.state, [fallbackKey]: true }, "");
            }
        } catch {
            // The fallback link still works when storage is unavailable.
        }
    }

    function reset() {
        clearTimeout(retryTimer);
        pending = false;
        link.removeAttribute("aria-disabled");
    }

    function isPreviousPage(url) {
        if (!url) return false;
        const previous = new URL(url);
        const current = window.location;
        return previous.origin === current.origin &&
            (previous.pathname !== current.pathname || previous.search !== current.search);
    }

    // A document restored from the browser cache can accept another Back tap.
    window.addEventListener("pageshow", reset);

    link.addEventListener("click", async (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
            return;
        }
        event.preventDefault();
        if (pending) return;
        pending = true;
        link.setAttribute("aria-disabled", "true");

        if (navigation) {
            try {
                const current = navigation.currentEntry;
                const previous = !current.getState()?.[fallbackKey] && navigation.canGoBack
                    ? navigation.entries().slice(0, current.index).findLast(entry => isPreviousPage(entry.url))
                    : null;
                const result = previous
                    ? navigation.traverseTo(previous.key)
                    : navigation.navigate(link.href, { history: "replace", state: { [fallbackKey]: true } });
                await Promise.all([result.committed, result.finished]);
            } catch {
                // A cancelled navigation leaves the user on this page.
            } finally {
                reset();
            }
            return;
        }

        // The older History API cannot tell whether a back traversal succeeded.
        retryTimer = setTimeout(reset, 2000);
        if (!history.state?.[fallbackKey] && history.length > 1 && isPreviousPage(document.referrer)) {
            history.back();
            return;
        }
        try {
            sessionStorage.setItem(fallbackKey, link.href);
        } catch {
            // Replacing the entry still avoids adding a new visit.
        }
        window.location.replace(link.href);
    });
})();
