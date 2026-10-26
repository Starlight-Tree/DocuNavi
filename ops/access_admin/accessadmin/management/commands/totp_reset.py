"""運用者の TOTP の機器を消す（認証アプリを失くしたとき）。消したあと、次のログインで登録をやり直す。

    python manage.py totp_reset <ユーザー名>

シークレットは表示しない（消した台数だけ）。実行できるのは、DB に接続できる運用者だけ（Cloud Run のジョブまたはプロキシ経由。docs/access-admin.md）。
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "指定したユーザーの TOTP の機器をすべて消す（次のログインで登録をやり直す）"

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(**{user_model.USERNAME_FIELD: options["username"]})
        except user_model.DoesNotExist:
            raise CommandError("そのユーザーは見つかりません。") from None
        deleted, _ = TOTPDevice.objects.filter(user=user).delete()
        self.stdout.write(f"TOTP の機器を {deleted} 台消しました。次のログインで登録をやり直します。")
