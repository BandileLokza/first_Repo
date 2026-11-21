document.addEventListener('DOMContentLoaded', function () {
    const leagueSelect = document.getElementById('id_league');

    document.body.addEventListener('select2:opening', function (e) {
        const leagueId = leagueSelect.value;

        if (!leagueId) return;

        const select = e.target;

        if (select.id === 'id_home_team' || select.id === 'id_away_team') {
            const url = new URL(select.dataset.ajaxUrl, window.location.origin);
            url.searchParams.set('league_id', leagueId);
            select.dataset.ajaxUrl = url.toString();
        }
    });
});
