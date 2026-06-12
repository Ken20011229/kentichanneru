"""Run this once to complete YouTube OAuth and save token.json."""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from src.config_loader import load_config
from src.uploader.youtube_uploader import YouTubeUploader

if __name__ == "__main__":
    config = load_config()
    token_path = config["youtube"]["token_file"]
    creds_path = config["youtube"]["credentials_file"]

    if not os.path.exists(creds_path):
        print(f"ERROR: {creds_path} が見つかりません。")
        print("Google Cloud Console から client_secrets.json をダウンロードして配置してください。")
        sys.exit(1)

    if os.path.exists(token_path):
        print(f"token.json は既に存在します: {token_path}")
        ans = input("再認証しますか? [y/N]: ").strip().lower()
        if ans != "y":
            print("認証をスキップしました。")
            sys.exit(0)
        os.remove(token_path)

    print("ブラウザが開きます。Googleアカウントでアプリを認証してください。")
    uploader = YouTubeUploader(config)
    print(f"\n認証成功! トークンを保存しました: {token_path}")
    print("次のコマンドで実際のアップロードテストが行えます:")
    print("  python main.py --once")
