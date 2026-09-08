class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status = status
        self.response = {"error": code, "message": message, **details}
        super().__init__(message)
