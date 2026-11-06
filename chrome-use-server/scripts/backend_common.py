# Shared transport/state utilities adapted from computer-use-server; no sibling runtime dependency.
"""Errors shared by the browser adapter and its worker transport."""


class BackendError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
