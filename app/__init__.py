from app.log_redaction import install as _install_log_redaction

_install_log_redaction()  # ログと未処理の例外に混ざる秘密情報（接続文字列のパスワードなど）を伏せる。詳細は app/log_redaction.py
