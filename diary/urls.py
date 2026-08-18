from django.urls import include, path
from core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("invite/<str:token>/", views.accept_invite, name="accept_invite"),
    path("journal/", include("core.journal_urls")),
    path("review/", include("core.review_urls")),
    path("control/", include("core.control_urls")),
    path("api/v1/", include("core.api_urls")),
    path("health/live", views.liveness, name="liveness"),
    path("health/ready", views.readiness, name="readiness"),
]
