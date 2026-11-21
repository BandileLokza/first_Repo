(function () {
    "use strict";

    function hasSafeSameOriginReferrer() {
        if (!document.referrer) {
            return false;
        }
        try {
            return new URL(document.referrer).origin === window.location.origin;
        } catch (error) {
            return false;
        }
    }

    document.addEventListener("click", function (event) {
        var backButton = event.target.closest("[data-pool-history-back]");
        if (!backButton) {
            return;
        }

        event.preventDefault();
        if (window.history.length > 1 && hasSafeSameOriginReferrer()) {
            window.history.back();
            return;
        }

        window.location.assign(backButton.dataset.poolFallback);
    });
}());
