class AccessRejectedError(ValueError):
    """A fixed reason that contains no account fields or database error text."""


AccessRejected = AccessRejectedError
