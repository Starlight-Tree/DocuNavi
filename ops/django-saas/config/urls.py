from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("saas.urls")),
    path("", include("saas.web_urls")),
]
