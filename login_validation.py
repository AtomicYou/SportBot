"""University email validation before collecting a password or opening a browser."""
import re

EMAIL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._%+\-]*@innopolis\.university', re.IGNORECASE)


def is_university_email(value):
    return isinstance(value, str) and len(value.split('@', 1)[0]) <= 64 and EMAIL.fullmatch(value) is not None
