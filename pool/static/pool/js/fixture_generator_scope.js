(function () {
    "use strict";

    function submitScope(form) {
        if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
        } else {
            form.submit();
        }
    }

    function installFixtureScopeForms() {
        document.querySelectorAll("[data-pool-fixture-scope-form]").forEach(function (form) {
            var stage = form.elements.stage;
            var group = form.elements.group;
            if (!stage || !group) {
                return;
            }

            group.addEventListener("change", function () {
                submitScope(form);
            });
            stage.addEventListener("change", function () {
                // A group can never carry over into another stage. The server
                // validates the resulting empty/selected group before exposing
                // an action form for the new scope.
                group.value = "";
                submitScope(form);
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installFixtureScopeForms);
    } else {
        installFixtureScopeForms();
    }
}());
