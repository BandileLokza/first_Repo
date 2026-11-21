document.addEventListener("DOMContentLoaded", function () {

    const leagueSelect = document.getElementById("id_league");
    const homeSelect = document.getElementById("id_home_team");
    const awaySelect = document.getElementById("id_away_team");

    function resetSelect(select) {
        select.innerHTML = '<option value="">---------</option>';
    }

    // CLEAR TEAMS ON PAGE LOAD
    resetSelect(homeSelect);
    resetSelect(awaySelect);

    function loadTeams(leagueId) {
        fetch(`/matches/ajax/get-teams/?league_id=${leagueId}`)
            .then(response => response.json())
            .then(data => {

                resetSelect(homeSelect);
                resetSelect(awaySelect);

                data.forEach(team => {
                    const option1 = new Option(team.name, team.id);
                    const option2 = new Option(team.name, team.id);

                    homeSelect.add(option1);
                    awaySelect.add(option2);
                });
            });
    }

    leagueSelect.addEventListener("change", function () {
        const leagueId = this.value;

        if (!leagueId) {
            resetSelect(homeSelect);
            resetSelect(awaySelect);
            return;
        }

        loadTeams(leagueId);
    });

});