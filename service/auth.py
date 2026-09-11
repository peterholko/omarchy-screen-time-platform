"""Parent/root PAM password by default; an optional independent parent PIN."""
import ctypes
import hashlib
import hmac
import secrets


def pin_record(pin):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1)
    return {"algorithm": "scrypt", "salt": salt.hex(), "key": key.hex()}


def verify_pin(pin, record):
    try:
        if record.get("algorithm") != "scrypt" or len(record["salt"]) != 32 or len(record["key"]) != 128:
            return False
        key = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(record["salt"]), n=16384, r=8, p=1)
        return hmac.compare_digest(key.hex(), record["key"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def parent_password_ok(password):
    """Authenticate root through the installed PAM stack. No sudo timestamp,
    root-to-root sudo shortcut, shell interpolation or password arguments.
    PAM owns the allocated conversation replies after a successful callback.
    """
    if not password or "\x00" in password:
        return False
    pam = ctypes.CDLL("libpam.so.0")
    libc = ctypes.CDLL(None)

    class Message(ctypes.Structure):
        _fields_ = [("style", ctypes.c_int), ("text", ctypes.c_char_p)]

    class Response(ctypes.Structure):
        _fields_ = [("text", ctypes.c_void_p), ("code", ctypes.c_int)]

    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.POINTER(Message)), ctypes.POINTER(ctypes.POINTER(Response)), ctypes.c_void_p)

    class Conversation(ctypes.Structure):
        _fields_ = [("callback", callback_type), ("data", ctypes.c_void_p)]

    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.calloc.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    libc.strdup.restype = ctypes.c_void_p
    libc.free.argtypes = [ctypes.c_void_p]

    @callback_type
    def conversation(count, messages, output, _data):
        if not 1 <= count <= 32:
            return 19  # PAM_CONV_ERR
        address = libc.calloc(count, ctypes.sizeof(Response))
        if not address:
            return 5  # PAM_BUF_ERR
        responses = ctypes.cast(address, ctypes.POINTER(Response))
        for i in range(count):
            style = messages[i].contents.style
            if style in (1, 2):  # ECHO_OFF password, ECHO_ON username
                responses[i].text = libc.strdup(password.encode() if style == 1 else b"root")
                if responses[i].text:
                    continue
            elif style in (3, 4):  # Informational messages, no response.
                continue
            for j in range(i + 1):
                libc.free(responses[j].text)
            libc.free(address)
            return 19
        output[0] = responses
        return 0

    pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conversation), ctypes.POINTER(ctypes.c_void_p)]
    pam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    handle = ctypes.c_void_p()
    conv = Conversation(conversation, None)
    result = pam.pam_start(b"peterholko-screen-time", b"root", ctypes.byref(conv), ctypes.byref(handle))
    if result != 0:
        return False
    try:
        result = pam.pam_authenticate(handle, 0)
        if result == 0:
            result = pam.pam_acct_mgmt(handle, 0)
        return result == 0
    finally:
        pam.pam_end(handle, result)
