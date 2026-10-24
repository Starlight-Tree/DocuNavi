from django.urls import path

from . import views

urlpatterns = [
    path("me/orgs/", views.my_orgs),
    path("orgs/<int:org_id>/", views.org_detail),
    path("orgs/<int:org_id>/members/", views.members),
]
