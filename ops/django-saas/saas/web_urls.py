from django.urls import path

from . import web_views

urlpatterns = [
    path("", web_views.index, name="index"),
    path("login/", web_views.login_view, name="login"),
    path("logout/", web_views.logout_view, name="logout"),
    path("dashboard/", web_views.dashboard, name="dashboard"),
    path("orgs/", web_views.org_list, name="org_list"),
    path("orgs/<int:org_id>/", web_views.org_detail, name="org_detail"),
    path("orgs/<int:org_id>/members/manage/", web_views.members_manage, name="members_manage"),
]
