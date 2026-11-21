(function () {
    "use strict";

    function configureRegionField() {
        const section = document.getElementById("id_league_type");
        const region = document.getElementById("id_region");
        if (!section || !region) return;

        const wrapper = region.closest(".form-row, .field-region");
        const update = function () {
            const isRegional = section.value === "REGIONAL";
            if (wrapper) wrapper.hidden = !isRegional;
            region.disabled = !isRegional;
            if (!isRegional) region.value = "";
        };

        section.addEventListener("change", update);
        update();
    }

    document.addEventListener("DOMContentLoaded", configureRegionField);
})();
