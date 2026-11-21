from django.conf import settings
from django.db import models


class LeagueMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "OWNER", "League Owner"
        ADMIN = "ADMIN", "League Admin"
        AGENT = "AGENT", "Match Agent"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="league_memberships",
    )

    competition = models.ForeignKey(
        "leagues.Competition",
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [
            "competition__name",
            "role",
            "user__username",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=["user", "competition"],
                name="unique_user_competition_membership",
            )
        ]

    def __str__(self):
        return (
            f"{self.user} - "
            f"{self.competition.name} - "
            f"{self.get_role_display()}"
        )


class RegionalAdministrator(models.Model):
    """A superuser-appointed administrator for one football Region."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="regional_administratorships",
    )
    region = models.ForeignKey(
        "leagues.Region",
        on_delete=models.CASCADE,
        related_name="regional_administrators",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["region__name", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region"],
                name="unique_regional_administrator_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.user} - Regional Administrator for {self.region}"


class OrganisationAdministrator(models.Model):
    """A superuser-appointed administrator for one non-regional football section.

    Each appointment is explicit.  A person can hold more than one appointment
    without that becoming platform-wide access, which keeps this compatible
    with future football organisations.
    """

    class Organisation(models.TextChoices):
        EWFA = "EWFA", "EWFA"
        PLE = "PLE", "Premier League of Eswatini (PLE)"
        ZONAL = "ZONAL", "Zonal Leagues"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organisation_administratorships",
    )
    organisation = models.CharField(
        max_length=10,
        choices=Organisation.choices,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organisation", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organisation"],
                name="unique_organisation_administrator_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.get_organisation_display()} Administrator"


class RegionalReportPreference(models.Model):
    """Per-administrator saved newspaper report selections.

    Selections reference the permanent league series, not a season, so a
    preference remains meaningful after a season rollover.
    """

    class ReportKind(models.TextChoices):
        FIXTURES = "FIXTURES", "Fixtures"
        RESULTS = "RESULTS", "Results"
        STANDINGS = "STANDINGS", "Standings"

    class SelectionMode(models.TextChoices):
        ALL = "ALL", "All leagues"
        CUSTOM = "CUSTOM", "Selected leagues"

    class OutputFormat(models.TextChoices):
        DOCX = "DOCX", "Word (.docx)"
        PDF = "PDF", "PDF (.pdf)"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="regional_report_preferences",
    )
    region = models.ForeignKey(
        "leagues.Region",
        on_delete=models.CASCADE,
        related_name="report_preferences",
    )
    report_kind = models.CharField(max_length=10, choices=ReportKind.choices)
    selection_mode = models.CharField(
        max_length=10,
        choices=SelectionMode.choices,
        default=SelectionMode.ALL,
    )
    selected_series = models.ManyToManyField(
        "leagues.CompetitionSeries",
        blank=True,
        related_name="regional_report_preferences",
    )
    output_format = models.CharField(
        max_length=4,
        choices=OutputFormat.choices,
        default=OutputFormat.DOCX,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region", "report_kind"],
                name="unique_regional_report_preference",
            ),
        ]

    def __str__(self):
        return f"{self.region} {self.get_report_kind_display()} preference for {self.user}"


class OrganisationReportPreference(models.Model):
    """Saved report selections for non-regional section administrators."""

    class ReportKind(models.TextChoices):
        FIXTURES = "FIXTURES", "Fixtures"
        RESULTS = "RESULTS", "Results"
        STANDINGS = "STANDINGS", "Standings"

    class SelectionMode(models.TextChoices):
        ALL = "ALL", "All leagues"
        CUSTOM = "CUSTOM", "Selected leagues"

    class OutputFormat(models.TextChoices):
        DOCX = "DOCX", "Word (.docx)"
        PDF = "PDF", "PDF (.pdf)"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organisation_report_preferences",
    )
    organisation = models.CharField(
        max_length=10,
        choices=OrganisationAdministrator.Organisation.choices,
    )
    report_kind = models.CharField(max_length=10, choices=ReportKind.choices)
    selection_mode = models.CharField(
        max_length=10,
        choices=SelectionMode.choices,
        default=SelectionMode.ALL,
    )
    selected_series = models.ManyToManyField(
        "leagues.CompetitionSeries",
        blank=True,
        related_name="organisation_report_preferences",
    )
    output_format = models.CharField(
        max_length=4,
        choices=OutputFormat.choices,
        default=OutputFormat.DOCX,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organisation", "report_kind"],
                name="unique_organisation_report_preference",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_organisation_display()} "
            f"{self.get_report_kind_display()} preference for {self.user}"
        )


class ActivityLog(models.Model):
    class Action(models.TextChoices):
        AGENT_REGISTERED = (
            "AGENT_REGISTERED",
            "Agent Registered",
        )

        AGENT_REACTIVATED = (
            "AGENT_REACTIVATED",
            "Agent Reactivated",
        )

        AGENT_DEACTIVATED = (
            "AGENT_DEACTIVATED",
            "Agent Deactivated",
        )

        AGENT_ASSIGNED = (
            "AGENT_ASSIGNED",
            "Agent Assigned",
        )
        KNOCKOUT_ROUNDS_CONFIGURED = (
            "KNOCKOUT_ROUNDS_CONFIGURED",
            "Knockout Rounds Configured",
        )
        GROUP_CREATED = (
            "GROUP_CREATED",
            "Group Created",
        )

        GROUP_UPDATED = (
            "GROUP_UPDATED",
            "Group Updated",
        )

        AGENT_UNASSIGNED = (
            "AGENT_UNASSIGNED",
            "Agent Unassigned",
        )
        FIRST_HALF_STARTED = (
            "FIRST_HALF_STARTED",
            "First Half Started",
        )
        FIRST_HALF_ENDED = (
            "FIRST_HALF_ENDED",
            "First Half Ended",
        )
        SECOND_HALF_STARTED = (
            "SECOND_HALF_STARTED",
            "Second Half Started",
        )
        REGULATION_ENDED = "REGULATION_ENDED", "Regulation Ended"
        EXTRA_TIME_STARTED = "EXTRA_TIME_STARTED", "Extra Time Started"
        EXTRA_TIME_FIRST_HALF_ENDED = (
            "EXTRA_TIME_FIRST_HALF_ENDED",
            "Extra Time First Half Ended",
        )
        EXTRA_TIME_SECOND_HALF_STARTED = (
            "EXTRA_TIME_SECOND_HALF_STARTED",
            "Extra Time Second Half Started",
        )
        EXTRA_TIME_ENDED = "EXTRA_TIME_ENDED", "Extra Time Ended"
        PENALTIES_STARTED = "PENALTIES_STARTED", "Penalties Started"
        PENALTIES_RECORDED = "PENALTIES_RECORDED", "Penalties Recorded"

        GOAL_ADDED = "GOAL_ADDED", "Goal Added"
        GOAL_EDITED = "GOAL_EDITED", "Goal Edited"
        GOAL_CANCELLED = "GOAL_CANCELLED", "Goal Cancelled"

        YELLOW_CARD_ADDED = (
            "YELLOW_CARD_ADDED",
            "Yellow Card Added",
        )
        CARD_ADDED = "CARD_ADDED", "Card Added"
        CARD_EDITED = "CARD_EDITED", "Card Edited"
        CARD_CANCELLED = "CARD_CANCELLED", "Card Cancelled"

        MATCH_FINISHED = "MATCH_FINISHED", "Match Finished"
        MATCH_ABANDONED = "MATCH_ABANDONED", "Match Abandoned"
        ABANDONMENT_RESOLVED = (
            "ABANDONMENT_RESOLVED",
            "Abandoned Match Resolved",
        )
        ABANDONMENT_RESOLUTION_REOPENED = (
            "ABANDONMENT_RESOLUTION_REOPENED",
            "Abandoned Match Resolution Reopened",
        )
        RESULT_CORRECTED = "RESULT_CORRECTED", "Result Corrected"

        BRACKET_CREATED = "BRACKET_CREATED", "Bracket Created"
        BRACKET_SEEDED = "BRACKET_SEEDED", "Bracket Seeded"
        BRACKET_LINKED = "BRACKET_LINKED", "Bracket Links Created"
        BYE_ADVANCED = "BYE_ADVANCED", "Bye Advanced"
        KNOCKOUT_WINNER_ADVANCED = (
            "KNOCKOUT_WINNER_ADVANCED",
            "Knockout Winner Advanced",
        )
        KNOCKOUT_CHAMPION_DECLARED = (
            "KNOCKOUT_CHAMPION_DECLARED",
            "Knockout Champion Declared",
        )
        BRACKET_CORRECTION_BLOCKED = (
            "BRACKET_CORRECTION_BLOCKED",
            "Bracket Correction Blocked",
        )
        BRACKET_CORRECTION_RESOLVED = (
            "BRACKET_CORRECTION_RESOLVED",
            "Bracket Correction Resolved",
        )

        TEAM_CREATED = "TEAM_CREATED", "Team Created"
        TEAM_UPDATED = "TEAM_UPDATED", "Team Updated"
        PLAYER_CREATED = "PLAYER_CREATED", "Player Created"
        PLAYER_UPDATED = "PLAYER_UPDATED", "Player Updated"

        FIXTURE_CREATED = "FIXTURE_CREATED", "Fixture Created"
        FIXTURE_UPDATED = "FIXTURE_UPDATED", "Fixture Updated"
        FIXTURE_DELETED = "FIXTURE_DELETED", "Fixture Deleted"
        SCHEDULE_LOCKED = "SCHEDULE_LOCKED", "Schedule Locked"
        SCHEDULE_UNLOCKED = "SCHEDULE_UNLOCKED", "Schedule Unlocked"

        STAFF_ADDED = "STAFF_ADDED", "Staff Added"
        STAFF_REMOVED = "STAFF_REMOVED", "Staff Removed"
        COACH_ASSIGNED = "COACH_ASSIGNED", "Team Coach Assigned"
        COACH_REMOVED = "COACH_REMOVED", "Team Coach Removed"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="league_activity_actions",
    )

    competition = models.ForeignKey(
        "leagues.Competition",
        on_delete=models.CASCADE,
        related_name="activity_logs",
    )

    match = models.ForeignKey(
        "matches.Match",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_logs",
    )

    action = models.CharField(
        max_length=40,
        choices=Action.choices,
    )

    description = models.TextField(blank=True)

    old_values = models.JSONField(
        default=dict,
        blank=True,
    )

    new_values = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["competition", "-created_at"],
                name="activity_league_date_idx",
            ),
            models.Index(
                fields=["match", "-created_at"],
                name="activity_match_date_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_action_display()} - "
            f"{self.competition.name}"
        )
