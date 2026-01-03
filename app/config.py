from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    database_url: str = Field(default="postgresql+psycopg://app:app@db:5432/app")

    # Square
    square_access_token: str = Field(default="")
    square_location_id: str = Field(default="")
    square_version: str = Field(default="2025-01-22")

    # eBay
    ebay_client_id: str = Field(default="")
    ebay_client_secret: str = Field(default="")
    ebay_refresh_token: str = Field(default="")
    ebay_marketplace_id: str = Field(default="EBAY_GB")
    ebay_merchant_location_key: str = Field(default="")
    ebay_fulfillment_policy_id: str = Field(default="")
    ebay_payment_policy_id: str = Field(default="")
    ebay_return_policy_id: str = Field(default="")

    def validate_required(self) -> None:
        missing = []

        # Square required
        if not self.square_access_token.strip():
            missing.append("SQUARE_ACCESS_TOKEN")
        if not self.square_location_id.strip():
            missing.append("SQUARE_LOCATION_ID")

        # eBay required
        if not self.ebay_client_id.strip():
            missing.append("EBAY_CLIENT_ID")
        if not self.ebay_client_secret.strip():
            missing.append("EBAY_CLIENT_SECRET")
        if not self.ebay_refresh_token.strip():
            missing.append("EBAY_REFRESH_TOKEN")
        if not self.ebay_merchant_location_key.strip():
            missing.append("EBAY_MERCHANT_LOCATION_KEY")
        if not self.ebay_fulfillment_policy_id.strip():
            missing.append("EBAY_FULFILLMENT_POLICY_ID")
        if not self.ebay_payment_policy_id.strip():
            missing.append("EBAY_PAYMENT_POLICY_ID")
        if not self.ebay_return_policy_id.strip():
            missing.append("EBAY_RETURN_POLICY_ID")

        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Check your .env file is present and loaded by docker compose."
            )


settings = Settings()
