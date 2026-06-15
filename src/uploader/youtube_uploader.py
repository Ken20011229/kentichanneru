import logging
import os

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

    def upload(self, video_path: str, thumbnail_path: str, metadata: dict) -> str:
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata.get("tags", []),
                "categoryId": metadata.get("category_id") or self.config.get("category_id", "28"),
                "defaultLanguage": self.config.get("default_language", "ja"),
            },
            "status": {
                "privacyStatus": self.config.get("privacy_status", "public"),
                "selfDeclaredMadeForKids": self.config.get("made_for_kids", False),
            },
        }

        media = MediaFileUpload(video_path, chunksize=10 * 1024 * 1024, resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

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
