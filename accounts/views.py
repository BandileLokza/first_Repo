from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import SignUpForm, LoginForm


def _safe_next(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return next_url
    return "/"


def signup_view(request):
    if request.user.is_authenticated:
        return JsonResponse({"success": True, "redirect": "/"})

    next_url = request.POST.get("next") or "/"

    if request.method != "POST":
        return redirect(_safe_next(request, next_url))

    form = SignUpForm(request.POST)

    if form.is_valid():
        username = form.cleaned_data.get("username", "").strip()
        email = form.cleaned_data.get("email", "").strip()
        password = form.cleaned_data["password1"]

        if not username:
            base_username = email.split("@")[0] or "user"
            username = base_username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{counter}"
                counter += 1

        user = User.objects.create_user(
            username=username, email=email, password=password
        )
        # create_user() doesn't run authenticate(), so user.backend is
        # never set. With two backends configured, auth_login() can't
        # guess which one to use — must be explicit.
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return JsonResponse({"success": True, "redirect": _safe_next(request, next_url)})

    return JsonResponse({"success": False, "errors": form.errors}, status=400)


def login_view(request):
    if request.user.is_authenticated:
        return JsonResponse({"success": True, "redirect": "/"})

    next_url = request.POST.get("next") or "/"

    if request.method != "POST":
        return redirect(_safe_next(request, next_url))

    form = LoginForm(request.POST)

    if form.is_valid():
        identifier = form.cleaned_data["identifier"].strip()
        password = form.cleaned_data["password"]

        user = authenticate(request, username=identifier, password=password)
        # authenticate() already sets user.backend, so auth_login() here
        # needs no explicit backend argument.

        if user is not None:
            auth_login(request, user)
            return JsonResponse({"success": True, "redirect": _safe_next(request, next_url)})

        form.add_error(None, "Incorrect email/username or password.")

    return JsonResponse({"success": False, "errors": form.errors}, status=400)


def login_page_view(request):
    if request.user.is_authenticated:
        return redirect("/")
    return render(request, "accounts/login.html", {
        "next": request.GET.get("next", "/"),
    })

