class YtPdfError(Exception):
    """An expected, user-facing failure. The CLI prints the message without a traceback."""


class NoCaptionsError(YtPdfError):
    """The video has no captions to read, so the only way to a transcript is to transcribe the audio."""
