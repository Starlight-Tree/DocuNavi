from accessadmin import health
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

urlpatterns = [
    path("healthz", health.healthz, name="healthz"),
    path("readyz", health.readyz, name="readyz"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
]
