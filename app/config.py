"""
Configuration module — loads environment variables and defines constants.
"""

import os
from datetime import date, timedelta
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- AI API keys ---
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API key")
    GROQ_API_KEY: str = Field(default="", description="Groq API key")

    # --- Azure AD / Microsoft Graph ---
    AZURE_TENANT_ID: str = Field(default="", description="Azure AD tenant ID")
    AZURE_CLIENT_ID: str = Field(default="", description="Azure AD app client ID")
    AZURE_CLIENT_SECRET: str = Field(default="", description="Azure AD app client secret")

    # --- SharePoint / OneDrive Excel file ---
    DRIVE_ID: str = Field(default="", description="OneDrive or SharePoint drive ID")
    DRIVE_ITEM_ID: str = Field(default="", description="Excel workbook drive item ID")
    SHEET_NAME: str = Field(default="Sheet1", description="Worksheet name in the workbook")
    TABLE_NAME: str = Field(default="Table1", description="Table name in the worksheet (if using structured table)")

    # --- Teams Chat (Graph API subscription) ---
    CHAT_ID: str = Field(default="", description="MS Teams group chat ID for EOD messages")
    AGILE_CHAT_ID: str = Field(default="", description="MS Teams group chat ID for agile summaries (morning, WIP, progress). Falls back to CHAT_ID if not set.")
    WEBHOOK_NOTIFICATION_URL: str = Field(
        default="",
        description="Public URL for Graph API subscription notifications (e.g. https://your-server.com/api/graph-webhook)",
    )
    REDIRECT_URI: str = Field(
        default="",
        description="OAuth redirect URI for delegated auth (e.g. https://your-server.com/api/auth-callback)",
    )

    # --- Server ---
    HOST: str = Field(default="0.0.0.0", description="Server host")
    PORT: int = Field(default=8000, description="Server port")
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

# Allowed enum values for validation
ALLOWED_PRIORITIES = {"High", "Medium", "Low"}
ALLOWED_STAGES = {"WIP", "Sent for Approval", "Closed"}
DEFAULT_PRIORITY = "Medium"
DEFAULT_STAGE = "WIP"
DEFAULT_EXPECTED_SP = 2
MIN_SP = 1
MAX_SP = 13
MAX_FIELD_LENGTH = 500

# Default column order (fallback — actual columns are detected per sheet)
COLUMN_ORDER = [
    "Brand",
    "Activity Type",
    "Backlog",
    "Sprint Backlog",
    "Dependency",
    "Deadline",
    "Priority",
    "WIP",
    "Sent for Approval",
    "Closed",
    "Comments",
    "Expected Story Points",
    "Actual Story Points",
]

# Known brands (exact dropdown values in the sheet)
KNOWN_BRANDS = [
    "Wogom", "Wofi", "Brandverse", "WDV", "Mediaverse",
    "WEMS", "Schneider", "Abaj", "Nar Narayan", "Internal",
]

# Activity types (exact dropdown values in the sheet)
ACTIVITY_TYPES = [
    "Social Media", "Collateral", "Website", "Branding",
    "Ops", "Content", "Digital Marketing",
]

# Sub-brands: tasks detected as key are filed under the value brand
BRAND_PARENT = {
    "Wobble": "Wogom",
    "Aiwa": "Abaj",
}

# Fuzzy-match threshold for deduplication
DEDUP_THRESHOLD = 0.85

# Backlog promotion: threshold for matching parsed tasks to backlog items
BACKLOG_PROMOTION_THRESHOLD = 0.65

# Microsoft Graph API base URL
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


def get_sprint_end_date(ref_date: date | None = None) -> str:
    """
    Return the current sprint end date as YYYY-MM-DD.

    Convention:
      - If today is in the 1st–15th → sprint ends on the 15th
      - If today is in the 16th–end  → sprint ends on the last day of the month
    """
    ref = ref_date or date.today()
    if ref.day <= 15:
        sprint_end = ref.replace(day=15)
    else:
        # last day of the month
        next_month = ref.replace(day=28) + timedelta(days=4)
        sprint_end = next_month - timedelta(days=next_month.day)
    return sprint_end.isoformat()
