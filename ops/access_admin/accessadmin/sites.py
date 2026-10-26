from django.contrib.admin import AdminSite


class AccessAdminSite(AdminSite):
    """管理サイト。見た目は Django 標準の枠のまま（独自のデザインは作らない。承認済み）。"""

    site_header = "ドキュナビ 資料グループの管理"
    site_title = "ドキュナビ 管理"
    index_title = "管理"
