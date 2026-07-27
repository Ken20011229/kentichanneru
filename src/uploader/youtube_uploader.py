import logging
import os
import re
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class YouTubeUploader:
    def __init__(self, config: dict):
        self.config = config["youtube"]
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        token_file = self.config["token_file"]
        creds_file = self.config["credentials_file"]

        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(creds_file):
                    raise FileNotFoundError(
                        f"YouTube OAuth credentials not found at '{creds_file}'. "
                        "Download client_secrets.json from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
                creds = flow.run_local_server(port=0)

            os.makedirs(os.path.dirname(token_file), exist_ok=True)
            with open(token_file, "w") as f:
                f.write(creds.to_json())

        return build("youtube", "v3", credentials=creds)

    @staticmethod
    def _sanitize_tags(tags: list[str]) -> list[str]:
        """YouTube の制限に合わせてタグを整える。

        1タグ30文字・全体で500文字という上限があり、超えると videos.insert が
        400 を返して投稿ごと失敗する。日本語タグを12〜15個生成させている以上、
        送信前に必ず詰める必要がある。
        """
        out, seen, total = [], set(), 0
        for t in tags or []:
            t = re.sub(r"[<>]", "", str(t)).strip()[:30]
            if not t or t in seen:
                continue
            if total + len(t) + 1 > 480:
                break
            seen.add(t)
            out.append(t)
            total += len(t) + 1
        return out

    def upload(self, video_path: str, thumbnail_path: str, metadata: dict) -> str:
        lang = self.config.get("default_language", "ja")
        body = {
            "snippet": {
                "title": re.sub(r"[<>]", "", metadata["title"])[:100],
                "description": re.sub(r"[<>]", "", metadata["description"])[:4900],
                "tags": self._sanitize_tags(metadata.get("tags", [])),
                "categoryId": metadata.get("category_id") or self.config.get("category_id", "28"),
                "defaultLanguage": lang,
                # 音声言語を明示しないと自動翻訳字幕の起点にならず、日本語圏外
                # への露出が発生しない
                "defaultAudioLanguage": lang,
            },
            "status": {
                "privacyStatus": self.config.get("privacy_status", "public"),
                "selfDeclaredMadeForKids": self.config.get("made_for_kids", False),
                # config に notify_subscribers があるのにコードから参照されて
                # おらず、設定が効いていなかった。
                # metadata 側で明示された場合はそちらを優先する（Shorts は
                # 本編と同じ内容なので、1日4通の通知を送ると登録解除を招く）。
                "notifySubscribers": metadata.get(
                    "notify_subscribers", self.config.get("notify_subscribers", True)
                ),
            },
            "recordingDetails": {
                "recordingDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }

        media = MediaFileUpload(video_path, chunksize=10 * 1024 * 1024, resumable=True)
        request = self.service.videos().insert(
            part="snippet,status,recordingDetails", body=body, media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"Upload progress: {int(status.progress() * 100)}%")

        video_id = response["id"]
        logger.info(f"Video uploaded: https://youtu.be/{video_id}")

        try:
            self.service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path),
            ).execute()
            logger.info(f"Thumbnail set for video {video_id}")
        except Exception as e:
            logger.warning(f"Thumbnail upload failed (video still uploaded): {e}")

        return video_id

    def append_description(self, video_id: str, extra: str) -> bool:
        """公開済み動画の説明欄の末尾に1行追記する。

        本編アップロード時点では Shorts の ID がまだ確定していないため、
        相互リンクは後追いで足すしかない。失敗しても投稿自体は成功して
        いるので、例外は握って警告に留める。
        """
        extra = (extra or "").strip()
        if not extra:
            return False
        try:
            resp  = self.service.videos().list(part="snippet", id=video_id).execute()
            items = resp.get("items", [])
            if not items:
                logger.warning(f"Video {video_id} not found — skipping description update")
                return False
            snippet = items[0]["snippet"]
            current = snippet.get("description") or ""
            if extra in current:
                return True
            snippet["description"] = f"{current}\n\n{extra}".strip()[:4900]
            self.service.videos().update(
                part="snippet", body={"id": video_id, "snippet": snippet},
            ).execute()
            logger.info(f"Description cross-link added to {video_id}")
            return True
        except Exception as e:
            logger.warning(f"Could not append to description of {video_id}: {e}")
            return False
