from enum import Enum


class BatchStatus(str, Enum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    REJECTED = "rejected"

    def __str__(self) -> str:
        return self.value
