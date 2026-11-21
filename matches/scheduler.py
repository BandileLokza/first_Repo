from apscheduler.schedulers.background import BackgroundScheduler

scheduler = None


def update_live_minutes():
    from matches.models import Match

    live_matches = Match.objects.filter(status="LIVE")

    for match in live_matches:
        new_minute = match.get_current_minute()
        if match.minute != new_minute:
            match.minute = new_minute
            match.save(update_fields=["minute"])


def start():
    global scheduler

    if scheduler and scheduler.running:
        return

    scheduler = BackgroundScheduler(timezone="Africa/Mbabane")
    scheduler.add_job(
        update_live_minutes,
        "interval",
        seconds=30,
        id="update_live_minutes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()