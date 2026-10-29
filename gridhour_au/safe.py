"""Small defences shared by the rest of the package.

* Text from outside (API responses, the cache, the config file, exception messages) goes through
  :func:`label` before it is stored, and everything drawn goes through :func:`clean`.
* Files are written with :func:`atomic_write`: a fresh temporary file (O_EXCL, so a planted
  symlink is never followed), mode 0600, then renamed over the target.
"""
import os
import re
import tempfile
import unicodedata

# C0, DEL, C1 and lone surrogates. Any of these reaching a terminal can start an escape sequence
# (ESC, or U+009B which some terminals take as CSI) or break the UTF-8 encoder.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
# format characters (bidi overrides, zero width), combining marks and line/paragraph separators
DROP_CATEGORIES = {"Cf", "Mn", "Me", "Zl", "Zp", "Cs", "Co", "Cn"}


def clean(text):
    """Replace control characters with '?'. Cheap enough to run on every string drawn."""
    return CONTROL.sub("?", str(text))


def label(value, limit=60):
    """External text made safe to show: one terminal column per character, nothing invisible,
    nothing that reorders the line. Non-strings become None."""
    if not isinstance(value, str):
        return None
    out = []
    for ch in value[:limit]:
        if CONTROL.match(ch) or unicodedata.category(ch) in DROP_CATEGORIES:
            out.append("?")
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append("?")         # two columns wide would push the rest of the row sideways
        else:
            out.append(ch)
    return "".join(out)


def private_dir(path):
    """Create ``path`` as 0700, and tighten it if it already exists and is ours."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        st = os.stat(path)
        if st.st_uid == os.getuid() and st.st_mode & 0o077:
            os.chmod(path, 0o700)
    except (OSError, AttributeError):
        pass


def atomic_write(path, text):
    """Write ``text`` to ``path`` as a 0600 file, replacing it in one step."""
    folder = os.path.dirname(path)
    private_dir(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
