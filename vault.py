"""Optional user-scoped Windows DPAPI storage. No plaintext credential files."""
import ctypes
from ctypes import wintypes
import os


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def transform(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Сохранение токена доступно только через Windows DPAPI.')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'Yandex Backup OAuth', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise RuntimeError('Windows DPAPI: операция не выполнена.')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save(path, token):
    encrypted = transform(token.encode('utf-8'))
    temporary = path.with_suffix('.new')
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load(path):
    return transform(path.read_bytes(), True).decode('utf-8') if path.exists() else ''
