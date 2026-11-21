# Generated manually for the isolated 8-ball pool domain.

from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models
import uuid

from teams.storage_backends import SupabaseStorage


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PoolCompetition",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("slug", models.SlugField(unique=True)),
                ("description", models.TextField(blank=True)),
                (
                    "logo",
                    models.ImageField(
                        blank=True,
                        null=True,
                        storage=SupabaseStorage(folder="pool/competitions/logos"),
                        upload_to="pool/competitions/logos/",
                    ),
                ),
                (
                    "cover_image",
                    models.ImageField(
                        blank=True,
                        null=True,
                        storage=SupabaseStorage(folder="pool/competitions/covers"),
                        upload_to="pool/competitions/covers/",
                    ),
                ),
                (
                    "participant_mode",
                    models.CharField(
                        choices=[("SINGLES", "Singles"), ("TEAMS", "Teams")],
                        default="SINGLES",
                        max_length=10,
                    ),
                ),
                (
                    "format_type",
                    models.CharField(
                        choices=[
                            ("LEAGUE", "League"),
                            ("LEAGUE_KNOCKOUT", "League/Group + Knockout"),
                            ("KNOCKOUT", "Knockout"),
                        ],
                        default="LEAGUE",
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("PUBLISHED", "Published"),
                            ("COMPLETED", "Completed"),
                        ],
                        default="DRAFT",
                        max_length=10,
                    ),
                ),
                ("default_race_target", models.PositiveSmallIntegerField(default=7)),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                ("venue", models.CharField(blank=True, max_length=255)),
                ("rules", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owned_pool_competitions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-start_date", "name", "id"],
                "indexes": [
                    models.Index(
                        fields=["status", "start_date"],
                        name="pool_comp_status_date_idx",
                    ),
                    models.Index(
                        fields=["participant_mode", "format_type"],
                        name="pool_comp_mode_format_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("default_race_target__gte", 1)),
                        name="pool_competition_race_target_positive",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("start_date__isnull", True))
                            | models.Q(("end_date__isnull", True))
                            | models.Q(("end_date__gte", models.F("start_date")))
                        ),
                        name="pool_competition_dates_in_order",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolPlayer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "public_id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        unique=True,
                    ),
                ),
                ("full_name", models.CharField(max_length=200)),
                ("nickname", models.CharField(blank=True, max_length=100)),
                (
                    "profile_picture",
                    models.ImageField(
                        blank=True,
                        null=True,
                        storage=SupabaseStorage(folder="pool/players"),
                        upload_to="pool/players/",
                    ),
                ),
                ("region", models.CharField(blank=True, max_length=100)),
                ("country", models.CharField(blank=True, max_length=100)),
                (
                    "gender",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("FEMALE", "Female"),
                            ("MALE", "Male"),
                            ("NON_BINARY", "Non-binary"),
                            ("PREFER_NOT_TO_SAY", "Prefer not to say"),
                        ],
                        max_length=20,
                    ),
                ),
                ("contact_email", models.EmailField(blank=True, max_length=254)),
                ("contact_phone", models.CharField(blank=True, max_length=50)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pool_player_profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["full_name", "id"]},
        ),
        migrations.CreateModel(
            name="PoolTeam",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                (
                    "logo",
                    models.ImageField(
                        blank=True,
                        null=True,
                        storage=SupabaseStorage(folder="pool/teams"),
                        upload_to="pool/teams/",
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.CreateModel(
            name="PoolCompetitionMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("OWNER", "Competition Owner"),
                            ("ADMIN", "Competition Admin"),
                        ],
                        max_length=10,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="pool.poolcompetition",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pool_competition_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["competition__name", "role", "user__username", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "competition"),
                        name="pool_unique_user_competition_member",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolCompetitionEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("seed", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="pool.poolcompetition",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="competition_entries",
                        to="pool.poolplayer",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="competition_entries",
                        to="pool.poolteam",
                    ),
                ),
            ],
            options={
                "ordering": ["seed", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("player__isnull", False),
                                ("team__isnull", True),
                            )
                            | models.Q(
                                ("player__isnull", True),
                                ("team__isnull", False),
                            )
                        ),
                        name="pool_entry_exactly_one_participant",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("player__isnull", False)),
                        fields=("competition", "player"),
                        name="pool_unique_player_entry_per_comp",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("team__isnull", False)),
                        fields=("competition", "team"),
                        name="pool_unique_team_entry_per_comp",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolCompetitionStage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "stage_type",
                    models.CharField(
                        choices=[
                            ("LEAGUE", "League"),
                            ("GROUP", "Group"),
                            ("KNOCKOUT", "Knockout"),
                        ],
                        max_length=10,
                    ),
                ),
                ("order", models.PositiveSmallIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stages",
                        to="pool.poolcompetition",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("competition", "order"),
                        name="pool_unique_stage_order_per_comp",
                    ),
                    models.UniqueConstraint(
                        fields=("competition", "name"),
                        name="pool_unique_stage_name_per_comp",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolMatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("scheduled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("UPCOMING", "Upcoming"),
                            ("LIVE", "Live"),
                            ("FINISHED", "Finished"),
                            ("POSTPONED", "Postponed"),
                            ("CANCELLED", "Cancelled"),
                            ("ABANDONED", "Abandoned"),
                        ],
                        default="UPCOMING",
                        max_length=12,
                    ),
                ),
                ("race_target", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("home_score", models.PositiveSmallIntegerField(default=0)),
                ("away_score", models.PositiveSmallIntegerField(default=0)),
                ("round_label", models.CharField(blank=True, max_length=100)),
                ("venue", models.CharField(blank=True, max_length=255)),
                ("stream_url", models.URLField(blank=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "result_method",
                    models.CharField(
                        choices=[
                            ("PLAYED", "Played"),
                            ("WALKOVER", "Walkover"),
                            ("WITHDRAWAL", "Withdrawal"),
                            ("DISQUALIFICATION", "Disqualification"),
                            ("BYE", "Bye"),
                        ],
                        default="PLAYED",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "away_entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="away_pool_matches",
                        to="pool.poolcompetitionentry",
                    ),
                ),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="matches",
                        to="pool.poolcompetition",
                    ),
                ),
                (
                    "home_entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="home_pool_matches",
                        to="pool.poolcompetitionentry",
                    ),
                ),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="matches",
                        to="pool.poolcompetitionstage",
                    ),
                ),
                (
                    "winner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="won_pool_matches",
                        to="pool.poolcompetitionentry",
                    ),
                ),
            ],
            options={
                "ordering": ["scheduled_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["competition", "status", "scheduled_at"],
                        name="pool_match_comp_status_idx",
                    ),
                    models.Index(
                        fields=["stage", "scheduled_at"],
                        name="pool_match_stage_date_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(("home_entry", models.F("away_entry"))),
                        name="pool_match_distinct_competitors",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("home_score__gte", 0))
                        & models.Q(("away_score__gte", 0)),
                        name="pool_match_scores_nonnegative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("race_target__isnull", True))
                        | models.Q(("race_target__gte", 1)),
                        name="pool_match_race_target_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolRack",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("rack_number", models.PositiveSmallIntegerField()),
                (
                    "outcome_type",
                    models.CharField(
                        choices=[
                            ("NORMAL", "Normal"),
                            ("GREEN_APPLE", "Green apple"),
                            ("RED_APPLE", "Red apple"),
                            ("ONE_VISIT", "One visit"),
                        ],
                        default="NORMAL",
                        max_length=20,
                    ),
                ),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "breaker",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="break_off_pool_racks",
                        to="pool.poolcompetitionentry",
                    ),
                ),
                (
                    "match",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="racks",
                        to="pool.poolmatch",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recorded_pool_racks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "winner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="won_pool_racks",
                        to="pool.poolcompetitionentry",
                    ),
                ),
            ],
            options={
                "ordering": ["rack_number", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("match", "rack_number"),
                        name="pool_unique_rack_number_per_match",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("rack_number__gte", 1)),
                        name="pool_rack_number_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolMatchResultRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("reason", models.TextField(blank=True)),
                ("previous_result", models.JSONField(blank=True, default=dict)),
                ("revised_result", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "match",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="result_revisions",
                        to="pool.poolmatch",
                    ),
                ),
                (
                    "revised_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pool_match_result_revisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["match", "-created_at"],
                        name="pool_revision_match_date_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PoolTeamRoster",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "competition_entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="roster_members",
                        to="pool.poolcompetitionentry",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="team_roster_memberships",
                        to="pool.poolplayer",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "player__full_name", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("competition_entry", "player"),
                        name="pool_unique_player_per_team_entry",
                    ),
                ],
            },
        ),
    ]
