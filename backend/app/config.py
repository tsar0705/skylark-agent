import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Monday.com
    MONDAY_API_KEY: str = os.getenv(
        "MONDAY_API_KEY",
        ""
    )

    MONDAY_WORK_ORDERS_BOARD_ID: str = os.getenv(
        "MONDAY_WORK_ORDERS_BOARD_ID",
        ""
    )

    MONDAY_DEALS_BOARD_ID: str = os.getenv(
        "MONDAY_DEALS_BOARD_ID",
        ""
    )

    # Groq
    GROQ_API_KEY: str = os.getenv(
        "GROQ_API_KEY",
        ""
    )

    GROQ_MODEL: str = os.getenv(
        "GROQ_MODEL",
        "openai/gpt-oss-120b"
    )

    # Cache
    DATA_CACHE_TTL_SECONDS: int = int(
        os.getenv(
            "DATA_CACHE_TTL_SECONDS",
            "120"
        )
    )

    # CORS
    ALLOWED_ORIGINS: list[str] = os.getenv(
        "ALLOWED_ORIGINS",
        "*"
    ).split(",")

    def validate(self) -> list[str]:
        """Returns human-readable configuration problems."""

        problems = []

        if not self.MONDAY_API_KEY:
            problems.append(
                "MONDAY_API_KEY is not set"
            )

        if not self.MONDAY_WORK_ORDERS_BOARD_ID:
            problems.append(
                "MONDAY_WORK_ORDERS_BOARD_ID is not set"
            )

        if not self.MONDAY_DEALS_BOARD_ID:
            problems.append(
                "MONDAY_DEALS_BOARD_ID is not set"
            )

        if not self.GROQ_API_KEY:
            problems.append(
                "GROQ_API_KEY is not set"
            )

        return problems


settings = Settings()