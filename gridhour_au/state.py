"""Application state and the config file (~/.config/gridhour-au/config.json)."""
import json
import os
import time
from dataclasses import replace

from .grid import REGIONS, Forecast, normalize_postcode, number
from .plan import DEFAULT_JOBS, MODES, Job, parse_clock
from .safe import atomic_write, label
from .themes import C, apply_theme

MAX_JOBS = 20


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "gridhour-au", "config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.loads(f.read(256 * 1024))
    except (OSError, ValueError, RecursionError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


class State:
    def __init__(self):
        self.postcode = None        # e.g. "2000"; the region follows it
        self.region_id = None       # NEM region ("VIC1") when there is no postcode; None = NSW
        self.prices = True
        self.jobs = [replace(j) for j in DEFAULT_JOBS]
        self.sel = 0                # selected job
        self.mode = "both"          # green | cheap | both
        self.h12 = False
        self.mix = True             # show the generation mix rows
        self.frozen = None          # scrubbed time, None = live
        self.fc = Forecast()
        self.loading = False
        self.gen = 0                # fetch generation, the newest one wins
        self.prompt = None          # None | "job" | "postcode" | "help"
        self.buf = ""
        self.err = ""
        self.msg = ("", None, 0.0)
        self.compact = "auto"       # auto | on | off
        self.persist = True

    @property
    def job(self):
        return self.jobs[self.sel] if self.jobs else None

    def save(self):
        if not self.persist:
            return
        try:
            atomic_write(config_path(), json.dumps(
                {"postcode": self.postcode, "region": self.region_id, "prices": self.prices,
                 "jobs": [j.to_json() for j in self.jobs], "mode": self.mode, "theme": C.name, "h12": self.h12,
                 "mix": self.mix, "compact": self.compact}, indent=1))
        except OSError:
            pass

    def say(self, text, color=None, secs=3.5):
        self.msg = (text, color or C.TEXT, time.time() + secs)


def _choice(value, allowed):
    return isinstance(value, str) and value in allowed


def state_from_config():
    st = State()
    cfg = load_config()
    apply_theme(cfg.get("theme", "carbon"))
    if isinstance(cfg.get("postcode"), str):
        try:
            st.postcode = normalize_postcode(cfg["postcode"])
        except ValueError:
            st.postcode = None
    if _choice(cfg.get("region"), REGIONS):
        st.region_id = cfg["region"]
    st.prices = cfg.get("prices", True) is not False
    jobs = []
    raw = cfg.get("jobs")
    for j in raw[:MAX_JOBS] if isinstance(raw, list) else []:
        try:
            name = label(j["name"], 18)
            minutes = int(number(j["minutes"], 30, 24 * 60)) // 30 * 30
            deadline = parse_clock(j["deadline"]) if isinstance(j.get("deadline"), str) else None
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if name and name.strip():
            jobs.append(Job(name.strip(), minutes, deadline, j.get("split") is True))
    if jobs:
        st.jobs = jobs
    st.mode = cfg["mode"] if _choice(cfg.get("mode"), MODES) else "both"
    st.h12 = cfg.get("h12") is True
    st.mix = cfg.get("mix", True) is not False
    st.compact = cfg["compact"] if _choice(cfg.get("compact"), ("auto", "on", "off")) else "auto"
    return st
