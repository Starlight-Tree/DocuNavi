from django.contrib.admin.apps import AdminConfig


class AccessAdminConfig(AdminConfig):
    """`django.contrib.admin` の代わりに入れる設定。標準の管理サイトを、このサービスの AccessAdminSite に差し替える。"""

    default_site = "accessadmin.sites.AccessAdminSite"
