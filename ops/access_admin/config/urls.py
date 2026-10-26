from accessadmin import health, otp_views
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

otp_patterns = (
    [
        path("setup/", otp_views.setup, name="setup"),
        path("verify/", otp_views.verify, name="verify"),
        path("sign-out/", otp_views.sign_out, name="sign_out"),
    ],
    "otp",
)

urlpatterns = [
    path("otp/", include(otp_patterns)),
    path("healthz", health.healthz, name="healthz"),
    path("readyz", health.readyz, name="readyz"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
]
