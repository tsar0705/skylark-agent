import httpx
from app.config import settings

API_URL = "https://api.monday.com/v2"

BOARD_IDS = [
    "5030968956",
    "5030969063",
]
query = """
mutation ($boardId: ID!) {
    delete_board(board_id: $boardId) {
        id
    }
}
"""

headers = {
    "Authorization": settings.MONDAY_API_KEY,
    "Content-Type": "application/json",
    "API-Version": "2026-07",
}

for board_id in BOARD_IDS:
    print(f"Deleting board {board_id}...")

    response = httpx.post(
        API_URL,
        json={
            "query": query,
            "variables": {"boardId": board_id},
        },
        headers=headers,
        timeout=30,
    )

    print(response.text)

    response.raise_for_status()

print("Done.")