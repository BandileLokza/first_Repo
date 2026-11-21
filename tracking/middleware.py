from .models import PageVisit


class PageVisitTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method != "GET":
            return response

        path = request.path

        # ===== SKIP NOISY PATHS =====
        if (
            path.startswith("/admin/")
            or path.startswith("/static/")
            or path.startswith("/media/")
        ):
            return response

        # ===== ONLY TRACK HTML PAGES =====
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        if not request.session.session_key:
            request.session.create()

        PageVisit.objects.create(
            session_key=request.session.session_key,
            user=request.user if request.user.is_authenticated else None,
            path=path,
            referrer=request.META.get("HTTP_REFERER", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        return response