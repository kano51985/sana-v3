import os


def get_user_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            raw, _ = winreg.QueryValueEx(handle, name)
        return raw if isinstance(raw, str) else ""
    except Exception:
        return ""


def set_user_env(name: str, value: str) -> None:
    os.environ[name] = value
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)
    except Exception:
        pass
