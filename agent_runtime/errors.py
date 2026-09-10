class RuntimeFault(Exception):
    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def as_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out
