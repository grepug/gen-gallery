from __future__ import annotations


class ImageGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        retryable: bool,
        immediate_retry_on_other_key: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.immediate_retry_on_other_key = immediate_retry_on_other_key
