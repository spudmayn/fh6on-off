
#! python3.8
"""FH6 Engine Toggle: turn the Forza Horizon 6 engine off and on with a hotkey (F10).

Off: a shutoff sound plays, the car's own engine audio stumbles and dies away,
accelerate is cut, the brakes stay on lightly until the car stops, and it cannot be
reversed. On: the engine audio comes back while the car's real engine revs to a
multiple of its idle and settles, and driving comes back.

The engine audio is shaped by writing the game's Cars volume in memory; no menu opens
and nothing in the game install changes. Calibration finds where the running game keeps
that volume, once per game session. The game ignores keys sent by programs, so revving
and braking go through a virtual Xbox controller (ViGEmBus driver), and cutting your
own controller needs HidHide. The game's Data Out telemetry supplies revs and speed.
Settings are in settings.json next to the program; it is created on first run.

Offline use only: this edits the game's memory while it runs.

Usage:
    FH6EngineToggle                      calibrate if needed, then the hotkey toggles
    FH6EngineToggle --recalibrate        ignore the saved calibration
    FH6EngineToggle --restore-controller give the controller back to games and exit
    FH6EngineToggle --debug-report       write FH6-debug-report.txt and exit
Close the window to stop; the engine and controller are put back first.
"""

import base64
import collections
import ctypes
import ctypes.wintypes as wt
import glob
import json
import math
import msvcrt
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
import winsound
import zipfile

import numpy as np

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
FINGERPRINTS_PATH = os.path.join(HERE, "volume_fingerprints.json")
SHIFTING_FINGERPRINTS_PATH = os.path.join(HERE, "shifting_fingerprints.json")
LAUNCH_PATH = os.path.join(HERE, "launch_control.json")
SESSION_PATH = os.path.join(HERE, "session.json")
STARTUP_WAV = os.path.join(HERE, "startup.wav")
STARTUP_INDEX_PATH = os.path.join(HERE, "startup_cars.json")
CUSTOM_SHUTOFF_WAV = os.path.join(HERE, "shutoff.wav")
SOUNDS_DIR = os.path.join(HERE, "sounds")
VGMSTREAM = os.path.join(HERE, "tools", "vgmstream", "vgmstream-cli.exe")
HIDHIDE_CLI = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    r"Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
)
VIGEMBUS_URL = "https://github.com/nefarius/ViGEmBus/releases"
HIDHIDE_URL = "https://github.com/nefarius/HidHide/releases"
WINDOW_TITLE = "forza horizon 6"
STEAM_APP_ID = "2483190"
VERSION = "1.2.1"
GAME_DIR = None

# Everything a player can change lives in settings.json; these are the defaults and
# what each one means. load_settings() copies them onto the module names below.
DEFAULT_SETTINGS = {
    "hotkey": "F10",
    "controller_toggle_button": "DpadDown",
    "start_game": True,
    "rev_on_startup": True,
    "rev_multiple_of_idle": 8.0,
    "rev_trigger_strength": 0.6,
    "switch_to_manual_for_rev": True,
    "minimum_brake_when_off": 0.05,
    "park_hold": True,
    "park_hold_below_kmh": 7.0,
    "cut_controller_when_off": True,
    "auto_reconnect_controller": False,
    "throttle_keys": ["Up", "W"],
    "brake_keys": ["Down", "S"],
    "play_starter_sound": False,
    "startup_volume": 1.6,
    "shutoff_volume": 0.15,
    "telemetry_port": 5300,
    "game_exe_names": ["forzahorizon6.exe"],
    "enabled": True,
}
SETTINGS_HELP = {
    "hotkey": "Key that turns the engine off and on: F1-F12.",
    "controller_toggle_button": "Controller button that turns the engine off and on while driving, kept from the game while driving: "
                                "DpadUp, DpadDown, DpadLeft, DpadRight, Start, Back, LeftStick, RightStick, LB, RB, A, B, X, Y, or None.",
    "start_game": "Start Forza Horizon 6 through Steam if it is not running yet.",
    "rev_on_startup": "Rev the real engine on startup (needs ViGEmBus).",
    "rev_multiple_of_idle": "Startup rev peak as a multiple of the car's idle revs.",
    "rev_trigger_strength": "How hard the startup rev presses the throttle, 0 to 1.",
    "switch_to_manual_for_rev": "If you drive Automatic, switch to Manual during the startup rev so the car can be held "
                                "with the brake instead of the handbrake (which sets off launch control). Needs a one-time setup.",
    "minimum_brake_when_off": "Brake applied while rolling with the engine off, 0 to 1. You can brake harder.",
    "park_hold": "Hold the handbrake while stopped with the engine off, so the car cannot drive or reverse.",
    "park_hold_below_kmh": "Speed below which the car counts as stopped.",
    "cut_controller_when_off": "Route your controller through the tool so its triggers are cut while the engine is off (needs HidHide).",
    "auto_reconnect_controller": "When this is opened after the game, reconnect the controller so the game lets go of it "
                                 "(shows a Windows admin prompt, and may not work). Opening this before the game needs neither.",
    "throttle_keys": "Keyboard keys blocked while the engine is off.",
    "brake_keys": "Keyboard keys allowed only while the car is still rolling with the engine off.",
    "play_starter_sound": "Play a starter sound before the startup rev.",
    "startup_volume": "How loud the engine is while it starts and revs, as a multiple of your Cars volume, 1 to 3. "
                      "It cannot go above the Cars slider's maximum.",
    "shutoff_volume": "Loudness of the shutoff sound, 0 to 1.",
    "telemetry_port": "Port set in the game's Data Out settings.",
    "game_exe_names": "Process names the game may run as.",
    "enabled": "While false, the tool stays closed and your controller is left alone. FH6 Turn Off.bat and FH6 Turn On.bat switch this.",
}

KEY_CODES = {"Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27, "Space": 0x20,
             "Shift": 0x10, "Ctrl": 0x11, "Enter": 0x0D}
KEY_CODES.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEY_CODES.update({str(d): 0x30 + d for d in range(10)})
KEY_CODES.update({"F%d" % n: 0x6F + n for n in range(1, 13)})

# The starter from the game's "Car Failing" horn: this much of its cranking, slowed to
# HORN_SPEED, faded out over HORN_CROSSFADE while the car's own engine fades in.
HORN_CRANK_SECONDS = 1.6
HORN_CROSSFADE = 0.3
HORN_SPEED = 0.75
DEFAULT_CYLINDERS = 6
SOUND_GAIN = 0.6
SAMPLE_RATE = 44100

# On startup the engine audio fades back in over CATCH_RISE seconds while the revs rise
# and settle. Your own startup recordings start the catch CUSTOM_SOUND_OVERLAP before
# they end; the generated starter runs CRANK_SECONDS.
CATCH_RISE = 0.1
CRANK_SECONDS = 1.6
# The engine comes back at the startup_volume setting times your Cars level (capped at
# the slider's maximum), held until STARTUP_BOOST_HOLD, then eased back to your level
# over STARTUP_BOOST_FADE.
STARTUP_BOOST_HOLD = 0.8
STARTUP_BOOST_FADE = 0.6
CUSTOM_SOUND_OVERLAP = 0.1
STARTUP_CRANK_GAIN = 0.5
STARTUP_CRANK_KEEP = 1.0
STARTUP_CRANK_OVERLAP = 0.25
STARTUP_REV_AFTER = 0.5
STARTUP_REV_RATE = 0.8

# The startup rev holds the virtual handbrake (A) so the car stays put. With Data Out the
# throttle goes straight in and lets go once the revs, a lookahead ahead at their current
# rate, would reach the target, so a quick engine is let go sooner. The lookahead starts at
# FLARE_LOOKAHEAD and is learned per car from each start's overshoot. The handbrake is held
# until the revs fall back within SETTLED_FRACTION of the way to idle.
# Without Data Out the throttle eases in over TRIGGER_RAMP for FLARE_SECONDS and the
# handbrake stays HANDBRAKE_TAIL longer. Neither phase runs past its *_MAX_SECONDS.
FLARE_LOOKAHEAD = 0.05
FLARE_LOOKAHEAD_MAX = 0.25
LOOKAHEAD_LEARNING = 0.5
RATE_WINDOW = 0.03
SETTLED_FRACTION = 0.15
TRIGGER_RAMP = 0.15
FLARE_SECONDS = 0.15
FLARE_MAX_SECONDS = 8.0
HANDBRAKE_TAIL = 0.3
HANDBRAKE_TAIL_MAX_SECONDS = 1.5
# When the car was switched from Automatic to Manual to be held with the brake, Shifting
# goes back this long after the brake lets go, so the game never sees the brake held
# at a standstill in Automatic (where it is the reverse throttle).
SHIFT_BACK_DELAY = 0.1
# Turning the engine off or on starts with the key-turn click, CLICK_LEAD before the car's
# audio starts to change, so the click is always heard first. Off then follows SHUTOFF_FADE.
CLICK_LEAD = 0.06
SHUTOFF_FADE = ((0.00, 1.0), (0.07, 0.45), (0.15, 0.85), (0.32, 0.30), (0.60, 0.08), (0.85, 0.0))
ENVELOPE_STEP = 1.0 / 120

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
SYNCHRONIZE = 0x00100000
WM_TIMER = 0x0113
MEM_COMMIT = 0x1000
WRITABLE_PROTECT = {0x04, 0x08, 0x40, 0x80}
TH32CS_SNAPPROCESS = 0x2

WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
CHUNK_BYTES = 32 * 1024 * 1024
POINT_READ_LIMIT = 200000

# A fingerprint is the FINGERPRINT_WORDS words either side of a setting's place. On a new
# launch, memory is searched for up to FINGERPRINT_ANCHORS distinctive words from it
# (values found more than ANCHOR_HIT_LIMIT times are ignored), and a place is taken as
# found when at least FINGERPRINT_MATCH of its comparable words are the same.
# Fingerprints with fewer than MIN_FINGERPRINT_WORDS are not kept.
FINGERPRINT_WORDS = 64
FINGERPRINT_MATCH = 0.8
MIN_FINGERPRINT_WORDS = 12
FINGERPRINT_ANCHORS = 4
ANCHOR_HIT_LIMIT = 5000
COMMON_WORDS = {0x3F800000, 0x3F000000, 0x40000000, 0xBF800000, 0x7F7FFFFF, 0xFFFFFFFF, 0x80000000}

# The game's Shifting setting, by its place in the menu.
SHIFTING_AUTOMATIC, SHIFTING_MANUAL, SHIFTING_CLUTCH = 0, 1, 2
SHIFTING_VALUES = (SHIFTING_AUTOMATIC, SHIFTING_MANUAL, SHIFTING_CLUTCH)
SHIFTING_NAMES = {SHIFTING_AUTOMATIC: "Automatic", SHIFTING_MANUAL: "Manual", SHIFTING_CLUTCH: "Manual with Clutch"}
# Calibration sets Shifting to each of these in turn until few places follow it, and at
# least MIN_SHIFTING_STEPS times, since small numbers that change on their own can follow
# a short run of changes by chance.
MIN_SHIFTING_STEPS = 5
# The game keeps several copies of the setting, and all of them are written.
SHIFTING_STEPS = (SHIFTING_MANUAL, SHIFTING_CLUTCH, SHIFTING_AUTOMATIC, SHIFTING_MANUAL,
                  SHIFTING_AUTOMATIC, SHIFTING_CLUTCH, SHIFTING_MANUAL, SHIFTING_AUTOMATIC,
                  SHIFTING_CLUTCH, SHIFTING_AUTOMATIC, SHIFTING_MANUAL, SHIFTING_AUTOMATIC)
MAX_SHIFTING_PLACES = 16
FINAL_CANDIDATES = 16
MAX_KEPT_CANDIDATES = 64

# The slider number can be stored as a 0-1 fraction of a 10- or 100-step slider,
# or as the number itself.
REPS = (
    ("fraction of 10", lambda v: v / 10.0, 1.0),
    ("fraction of 100", lambda v: v / 100.0, 1.0),
    ("as shown", lambda v: v, 100.0),
)


def _key_codes(names):
    codes = tuple(KEY_CODES[n.strip().capitalize() if len(n.strip()) > 1 else n.strip().upper()] for n in names)
    if not codes:
        raise ValueError("empty")
    return codes


def _number(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError("must be a number from %g to %g" % (lo, hi))
        return float(value)
    return check


def _flag(value):
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _hotkey(value):
    if not (isinstance(value, str) and value.upper() in ("F%d" % n for n in range(1, 13))):
        raise ValueError("must be F1 to F12")
    return value.upper()


BUTTON_MASKS = {"DpadUp": 0x0001, "DpadDown": 0x0002, "DpadLeft": 0x0004, "DpadRight": 0x0008,
                "Start": 0x0010, "Back": 0x0020, "LeftStick": 0x0040, "RightStick": 0x0080,
                "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000}


def _button(value):
    if value is None or (isinstance(value, str) and value.lower() in ("", "none")):
        return None
    for name in BUTTON_MASKS:
        if isinstance(value, str) and value.lower() == name.lower():
            return name
    raise ValueError("must be one of %s, or None" % ", ".join(BUTTON_MASKS))


def _exe_names(value):
    if not value or not all(isinstance(v, str) for v in value):
        raise ValueError("must be a list of names")
    return tuple(v.lower() for v in value)


def _port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("must be a port number")
    return value


SETTING_CHECKS = {
    "hotkey": _hotkey,
    "controller_toggle_button": _button,
    "start_game": _flag,
    "rev_on_startup": _flag,
    "rev_multiple_of_idle": _number(1.0, 12.0),
    "rev_trigger_strength": _number(0.05, 1.0),
    "switch_to_manual_for_rev": _flag,
    "minimum_brake_when_off": _number(0.0, 1.0),
    "park_hold": _flag,
    "park_hold_below_kmh": _number(0.0, 50.0),
    "cut_controller_when_off": _flag,
    "auto_reconnect_controller": _flag,
    "throttle_keys": _key_codes,
    "brake_keys": _key_codes,
    "play_starter_sound": _flag,
    "startup_volume": _number(1.0, 3.0),
    "shutoff_volume": _number(0.0, 1.0),
    "telemetry_port": _port,
    "game_exe_names": _exe_names,
    "enabled": _flag,
}


def _apply(values):
    global HOTKEY_NAME, HOTKEY_VK, REV_WITH_PAD, FLARE_IDLE_MULTIPLE, TRIGGER_MAX, OFF_BRAKE
    global PARK_HOLD, PARK_HOLD_SPEED, PASS_THROUGH, FORCE_RECONNECT, THROTTLE_KEYS, BRAKE_KEYS
    global PLAY_STARTUP_SOUND, SHUTOFF_GAIN, TELEMETRY_PORT, GAME_EXES
    global TOGGLE_BUTTON_NAME, TOGGLE_BUTTON, START_GAME, STARTUP_LOUDNESS, SWITCH_TO_MANUAL
    HOTKEY_NAME = values["hotkey"]
    HOTKEY_VK = KEY_CODES[HOTKEY_NAME]
    TOGGLE_BUTTON_NAME = values["controller_toggle_button"]
    TOGGLE_BUTTON = BUTTON_MASKS.get(TOGGLE_BUTTON_NAME, 0)
    START_GAME = values["start_game"]
    STARTUP_LOUDNESS = values["startup_volume"]
    SWITCH_TO_MANUAL = values["switch_to_manual_for_rev"]
    REV_WITH_PAD = values["rev_on_startup"]
    FLARE_IDLE_MULTIPLE = values["rev_multiple_of_idle"]
    TRIGGER_MAX = values["rev_trigger_strength"]
    OFF_BRAKE = values["minimum_brake_when_off"]
    PARK_HOLD = values["park_hold"]
    PARK_HOLD_SPEED = values["park_hold_below_kmh"] / 3.6
    PASS_THROUGH = values["cut_controller_when_off"]
    FORCE_RECONNECT = values["auto_reconnect_controller"]
    THROTTLE_KEYS = values["throttle_keys"]
    BRAKE_KEYS = values["brake_keys"]
    PLAY_STARTUP_SOUND = values["play_starter_sound"]
    SHUTOFF_GAIN = values["shutoff_volume"]
    TELEMETRY_PORT = values["telemetry_port"]
    GAME_EXES = values["game_exe_names"]


_apply({name: SETTING_CHECKS[name](value) for name, value in DEFAULT_SETTINGS.items()})


def load_settings():
    """Read settings.json, falling back to the default for anything missing or invalid,
    and write it back so new settings and their descriptions show up in the file."""
    stored = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError) as e:
            print("settings.json could not be read (%s); using the defaults." % e)
    merged, checked = {}, {}
    for name, default in DEFAULT_SETTINGS.items():
        value = stored.get(name, default)
        try:
            checked[name] = SETTING_CHECKS[name](value)
            merged[name] = value
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            print("settings.json: %s = %r is not valid (%s); using %r." % (name, value, e, default))
            checked[name] = SETTING_CHECKS[name](default)
            merged[name] = default
    _apply(checked)
    merged["_help"] = SETTINGS_HELP
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)


def save_setting(name, value):
    """Change one setting in settings.json and apply it now."""
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        stored = json.load(f)
    stored[name] = value
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)
    load_settings()


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenMutexW.argtypes = (wt.DWORD, ctypes.c_bool, ctypes.c_wchar_p)
kernel32.OpenMutexW.restype = wt.HANDLE
INSTANCE_MUTEX = "Local\\FH6EngineToggle"
_instance = None
user32 = ctypes.WinDLL("user32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = LRESULT
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wt.HMODULE

kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.VirtualQueryEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t)
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.ReadProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.WriteProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.GetProcessTimes.argtypes = (wt.HANDLE,) + (ctypes.POINTER(wt.FILETIME),) * 4
kernel32.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
CTRL_HANDLER = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = (CTRL_HANDLER, wt.BOOL)
kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
kernel32.SetConsoleTitleW.argtypes = (wt.LPCWSTR,)
kernel32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
kernel32.WaitForSingleObject.restype = wt.DWORD
user32.SetTimer.argtypes = (wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
user32.SetTimer.restype = ctypes.c_size_t

user32.RegisterHotKey.argtypes = (wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = (wt.HWND,)
user32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)


def find_game_pid():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in GAME_EXES:
                return entry.th32ProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return None


def game_folder(handle):
    """The folder the running game's exe is in, wherever it is installed."""
    size = wt.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return os.path.dirname(buf.value)
    return None


def game_focused():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return WINDOW_TITLE in buf.value.lower()


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wt.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", wt.WORD), ("wRightMotorSpeed", wt.WORD)]


xinput = ctypes.WinDLL("xinput1_4")
xinput.XInputGetState.argtypes = (wt.DWORD, ctypes.POINTER(XINPUT_STATE))
xinput.XInputSetState.argtypes = (wt.DWORD, ctypes.POINTER(XINPUT_VIBRATION))
XINPUT_START = 0x0010
XINPUT_BACK = 0x0020
XINPUT_A = 0x1000
REPORT_FIELDS = ("wButtons", "bLeftTrigger", "bRightTrigger", "sThumbLX", "sThumbLY", "sThumbRX", "sThumbRY")
NEUTRAL = (0, 0, 0, 0, 0, 0, 0)
VIGEM_ERROR_NONE = 0x20000000


class VirtualPad:
    """A virtual Xbox controller that stays plugged in for the session. When your own
    controller is routed here (passing = True), everything you do on it is copied here. While the engine is off (mode() is not "on") the throttle is cut; while
    the car is still rolling ("moving") the brake is held at OFF_BRAKE or more, and once
    it is all but stopped ("stopped") the brake is cut too so it cannot reverse. On top
    of that the startup rev can take the throttle, brake and handbrake, and the parking
    hold the handbrake."""

    def __init__(self):
        import vgamepad
        import vgamepad.win.vigem_client as vcli

        self.vcli = vcli
        self.pad = vgamepad.VX360Gamepad()
        self.throttle = None
        self.brake = None
        self.handbrake = False
        self.passing = False
        self.mode = lambda: "on"
        self.driving = lambda: False
        self.on_toggle = None
        self._toggle_held = False
        self.slot = None
        self.pad.register_notification(callback_function=self._rumble)
        threading.Thread(target=self._run, daemon=True).start()

    def _own_slot(self):
        index = ctypes.c_ulong()
        err = self.vcli.vigem_target_x360_get_user_index(self.pad._busp, self.pad._devicep, ctypes.addressof(index))
        return index.value if err == VIGEM_ERROR_NONE else None

    def _read_real(self, state):
        """Your controller's state, finding which XInput slot it is in when needed."""
        if self.slot is not None and xinput.XInputGetState(self.slot, ctypes.byref(state)) == 0:
            return tuple(getattr(state.Gamepad, f) for f in REPORT_FIELDS)
        own = self._own_slot()
        if own is None:
            self.slot = None
            return NEUTRAL
        for slot in range(4):
            if slot != own and xinput.XInputGetState(slot, ctypes.byref(state)) == 0:
                if self.passing:
                    print("  Reading your controller (slot %d)." % (slot + 1))
                self.slot = slot
                return tuple(getattr(state.Gamepad, f) for f in REPORT_FIELDS)
        if self.slot is not None and self.passing:
            print("  Your controller disconnected; waiting for it to come back.")
        self.slot = None
        return NEUTRAL

    def _rumble(self, client, target, large_motor, small_motor, led_number, user_data):
        if self.passing and self.slot is not None:
            vibration = XINPUT_VIBRATION(large_motor * 257, small_motor * 257)
            xinput.XInputSetState(self.slot, ctypes.byref(vibration))

    def _run(self):
        state = XINPUT_STATE()
        last = None
        while True:
            buttons, lt, rt, lx, ly, rx, ry = self._read_real(state) if self.passing else NEUTRAL
            if TOGGLE_BUTTON:
                held = bool(buttons & TOGGLE_BUTTON) and self.driving()
                if held and not self._toggle_held and self.on_toggle is not None:
                    self.on_toggle()
                self._toggle_held = held
                if held:
                    buttons &= ~TOGGLE_BUTTON
            mode = self.mode()
            if mode != "on":
                # The game can still get your real throttle another way, so while
                # rolling the brake is pressed at least as hard as the throttle.
                lt = max(lt, int(OFF_BRAKE * 255), rt) if mode == "moving" else 0
                rt = 0
            if self.handbrake:
                buttons |= XINPUT_A
            if self.throttle is not None:
                rt = int(self.throttle * 255)
            if self.brake is not None:
                lt = int(self.brake * 255)
            report = (buttons, lt, rt, lx, ly, rx, ry)
            if report != last:
                for field, value in zip(REPORT_FIELDS, report):
                    setattr(self.pad.report, field, value)
                self.pad.update()
                last = report
            time.sleep(0.002)


def open_pad():
    if not REV_WITH_PAD:
        return None
    try:
        return VirtualPad()
    except Exception as e:
        print("No virtual controller (%s); startup will be an audio fade-in only." % e)
        return None


def hidhide(*args):
    return subprocess.run([HIDHIDE_CLI] + list(args), capture_output=True, text=True).stdout


CONTROLLER_QUERY = r"""
$bus = Get-PnpDevice -PresentOnly | Where-Object FriendlyName -like '*Virtual Gamepad Emulation Bus*'
$virtual = @(); $queue = @($bus.InstanceId)
while ($queue.Count) {
  $id, $queue = $queue; $queue = @($queue | Where-Object { $_ })
  $children = (Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_Children -ErrorAction SilentlyContinue).Data
  if ($children) { $virtual += $children; $queue += $children }
}
Get-PnpDevice -PresentOnly -Class XnaComposite,XboxComposite,HIDClass -ErrorAction SilentlyContinue |
  Where-Object { ($_.Class -ne 'HIDClass' -or $_.FriendlyName -like '*game controller*') -and $virtual -notcontains $_.InstanceId } |
  ForEach-Object { $_.InstanceId }
"""


def controller_devices():
    """Device instance paths of the real game controllers plugged in (not the
    virtual one)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", CONTROLLER_QUERY], capture_output=True, text=True
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def route_controller(pad):
    """Hide your controllers from everything but this tool and copy them onto the
    virtual controller instead, so the tool can cut them while the engine is off.
    Returns the hidden devices, or an empty list."""
    if not PASS_THROUGH or pad is None:
        return []
    if not os.path.exists(HIDHIDE_CLI):
        print("HidHide is not installed, so your controller cannot be cut while the engine is off.")
        return []
    devices = controller_devices()
    if not devices:
        print("No controller found to route through the tool; plug it in and restart this window.")
        return []
    args = ["--app-reg", sys.executable]
    for device in devices:
        args += ["--dev-hide", device]
    hidhide(*(args + ["--cloak-on"]))
    if "--cloak-on" not in hidhide("--cloak-state"):
        print("HidHide did not switch on, so your controller cannot be cut.")
        return []
    pad.passing = True
    print("Your controller now goes through this tool.")
    return devices


RECONNECT_SCRIPT = r"""
$ids = @(%s)
$targets = foreach ($id in $ids) {
  $parent = (Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue).Data
  if ($parent -like 'USB\VID_*' -and $parent -notlike '*&MI_*' -and $parent -notlike '*&IG_*') { $parent } else { $id }
}
foreach ($t in ($targets | Sort-Object -Unique)) { pnputil /restart-device "$t" | Out-Null }
"""


def reconnect_controllers(devices):
    """Restart the controllers' USB devices, like unplugging and replugging them. Runs
    elevated, so Windows asks for permission."""
    script = RECONNECT_SCRIPT % ",".join("'%s'" % d for d in devices)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    launcher = (
        "try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
        "-ArgumentList '-NoProfile','-EncodedCommand','%s'; exit 0 } catch { exit 1 }" % encoded
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", launcher], capture_output=True)
    return result.returncode == 0


def unroute_controller():
    hidhide("--cloak-off")


class Telemetry(threading.Thread):
    """Latest revs and car details from the game's Data Out packets (the "Sled" fields
    every Forza format starts with)."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.history = collections.deque(maxlen=60)
        self.idle_rpm = None
        self.max_rpm = None
        self.car = None
        self.cylinders = None
        self.speed = None

    def run(self):
        while True:
            data, _ = self.sock.recvfrom(2048)
            if len(data) < 44:
                continue
            race_on, _, max_rpm, idle_rpm, rpm = struct.unpack_from("<iIfff", data, 0)
            if not race_on or not 1000 < max_rpm < 30000:
                continue
            self.max_rpm, self.idle_rpm = max_rpm, idle_rpm
            self.speed = math.sqrt(sum(v * v for v in struct.unpack_from("<fff", data, 32)))
            if len(data) >= 232:
                car, _, _, _, cylinders = struct.unpack_from("<iiiii", data, 212)
                self.car = car
                if 1 <= cylinders <= 16:
                    self.cylinders = cylinders
            self.history.append((time.perf_counter(), rpm))

    def fresh(self):
        return bool(self.history) and time.perf_counter() - self.history[-1][0] < 0.2

    def rpm(self):
        return self.history[-1][1]

    def rate(self, window=0.1):
        """Revs change per second over the last window seconds."""
        if len(self.history) < 2:
            return 0.0
        t1, r1 = self.history[-1]
        for t0, r0 in reversed(self.history):
            if t1 - t0 >= window:
                return (r1 - r0) / (t1 - t0)
        return 0.0


def start_telemetry():
    try:
        telemetry = Telemetry(TELEMETRY_PORT)
    except OSError:
        return None
    telemetry.start()
    return telemetry


def f32_bits(value):
    return int(np.float32(value).view(np.uint32))


def patterns_for(v):
    """Bit patterns a slider reading of v could be stored as, one float step either
    side so rounding in the game's own maths still matches."""
    bits, reps = [], []
    for code, (_, fn, _) in enumerate(REPS):
        centre = f32_bits(fn(v))
        for delta in (-1, 0, 1):
            bits.append(centre + delta)
            reps.append(code)
    order = np.argsort(bits)
    return np.array(bits, dtype=np.uint32)[order], np.array(reps, dtype=np.uint8)[order]


class Game:
    def __init__(self, pid):
        access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | SYNCHRONIZE
        self.pid = pid
        self.handle = kernel32.OpenProcess(access, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        times = [wt.FILETIME() for _ in range(4)]
        kernel32.GetProcessTimes(self.handle, *[ctypes.byref(t) for t in times])
        self.created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def regions(self):
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        while kernel32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base = mbi.BaseAddress or 0
            if mbi.State == MEM_COMMIT and mbi.Protect in WRITABLE_PROTECT:
                yield base, mbi.RegionSize
            addr = base + mbi.RegionSize

    def running(self):
        return kernel32.WaitForSingleObject(self.handle, 0) != 0

    def read_f32(self, addr):
        buf = ctypes.c_float()
        n = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), ctypes.byref(buf), 4, ctypes.byref(n)):
            return None
        return buf.value

    def read_bits(self, addr):
        buf = ctypes.c_uint32()
        n = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), ctypes.byref(buf), 4, ctypes.byref(n)):
            return None
        return buf.value

    def write_f32(self, addr, value):
        data = ctypes.create_string_buffer(struct.pack("<f", value), 4)
        n = ctypes.c_size_t()
        return bool(kernel32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), data, 4, ctypes.byref(n)))

    def read_words(self, addr, count):
        buf = (ctypes.c_uint32 * count)()
        n = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, 4 * count, ctypes.byref(n)):
            return None
        return np.frombuffer(buf, dtype=np.uint32).copy()

    def write_u32(self, addr, value):
        data = ctypes.create_string_buffer(struct.pack("<I", value), 4)
        n = ctypes.c_size_t()
        return bool(kernel32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), data, 4, ctypes.byref(n)))

    def read_many(self, addrs):
        """The words at sorted 4-byte-aligned addresses, read a chunk at a time.
        Returns (words, readable)."""
        words = np.zeros(addrs.size, dtype=np.uint32)
        readable = np.zeros(addrs.size, dtype=bool)
        buf = (ctypes.c_char * CHUNK_BYTES)()
        n = ctypes.c_size_t()
        spans = [(0, addrs.size)] if addrs.size else []
        while spans:
            lo, hi = spans.pop()
            start = int(addrs[lo])
            end = int(np.searchsorted(addrs, np.uint64(start + CHUNK_BYTES - 4), side="right"))
            hi_here = min(hi, end)
            length = int(addrs[hi_here - 1]) + 4 - start
            if kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(start), buf, length, ctypes.byref(n)) \
                    and n.value == length:
                chunk = np.frombuffer(buf, dtype=np.uint32, count=length // 4)
                words[lo:hi_here] = chunk[((addrs[lo:hi_here] - np.uint64(start)) // np.uint64(4)).astype(np.int64)]
                readable[lo:hi_here] = True
            elif hi_here - lo > 1:
                mid = (lo + hi_here) // 2
                spans += [(lo, mid), (mid, hi_here)]
            if hi_here < hi:
                spans.append((hi_here, hi))
        return words, readable

    def scan(self, v):
        """Every 4-byte-aligned float in writable memory that could be slider reading v.
        Returns (addresses, rep codes)."""
        return self.scan_bits(*patterns_for(v))

    def scan_bits(self, bits, reps):
        """Every 4-byte-aligned word in writable memory equal to one of the sorted bits.
        Returns (addresses, the reps paired with the bits found)."""
        buf = (ctypes.c_char * CHUNK_BYTES)()
        n = ctypes.c_size_t()
        found_addrs, found_reps = [], []
        scanned = 0
        started = last_report = time.time()
        for base, size in self.regions():
            offset = 0
            while offset < size:
                length = min(CHUNK_BYTES, size - offset)
                if kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(base + offset), buf, length, ctypes.byref(n)):
                    words = np.frombuffer(buf, dtype=np.uint32, count=n.value // 4)
                    hits = np.flatnonzero(np.isin(words, bits))
                    if hits.size:
                        found_addrs.append(np.uint64(base + offset) + hits.astype(np.uint64) * np.uint64(4))
                        found_reps.append(reps[np.searchsorted(bits, words[hits])])
                offset += length
                scanned += length
            if time.time() - last_report > 2:
                last_report = time.time()
                print("\r  scanned %.1f GB" % (scanned / 2 ** 30), end="", flush=True)
        print("\r  scanned %.1f GB in %.0f s" % (scanned / 2 ** 30, time.time() - started))
        if not found_addrs:
            return np.zeros(0, np.uint64), np.zeros(0, np.uint8)
        return np.concatenate(found_addrs), np.concatenate(found_reps)

    def narrow(self, addrs, reps, v):
        """Keep the candidates that now read as slider value v in the same form."""
        if addrs.size > POINT_READ_LIMIT:
            new_addrs, new_reps = self.scan(v)
            keys = addrs * np.uint64(4) + reps
            new_keys = new_addrs * np.uint64(4) + new_reps
            kept = np.intersect1d(keys, new_keys)
            return kept // np.uint64(4), (kept % np.uint64(4)).astype(np.uint8)
        bits, rep_of = patterns_for(v)
        allowed = set(zip(bits.tolist(), rep_of.tolist()))
        keep = []
        for i, (addr, rep) in enumerate(zip(addrs.tolist(), reps.tolist())):
            if (self.read_bits(addr), rep) in allowed:
                keep.append(i)
        keep = np.array(keep, dtype=np.int64)
        return addrs[keep], reps[keep]


def ask_number(prompt, avoid=None):
    while True:
        text = input(prompt).strip()
        try:
            v = float(text)
        except ValueError:
            print("  Type just the number the slider shows, for example 7.")
            continue
        if v <= 0:
            print("  Pick a level above 0; a zero is too common in memory to track.")
            continue
        if avoid is not None and v == avoid:
            print("  That is the same level as last time; move the slider to a different one.")
            continue
        return v


def ask_yes(prompt):
    return input(prompt).strip().lower().startswith("y")


def calibrate(game):
    print("\nCalibration finds where the game keeps the Cars volume this session.")
    print("Step 1: in the game, open Settings > Audio and set Cars to any level above 0.")
    v = ask_number("Type the number the Cars slider shows: ")
    print("Searching the game's memory (this first pass takes a while)...")
    addrs, reps = game.scan(v)
    print("  %d possible matches." % addrs.size)

    stalled = 0
    while addrs.size > FINAL_CANDIDATES:
        print("\nMove the Cars slider to a different level (the menu can stay open).")
        v = ask_number("Type the new number: ", avoid=v)
        before = addrs.size
        addrs, reps = game.narrow(addrs, reps, v)
        print("  %d possible matches left." % addrs.size)
        stalled = stalled + 1 if addrs.size == before else 0
        if stalled >= 2 and addrs.size <= MAX_KEPT_CANDIDATES:
            break

    if addrs.size == 0:
        print("\nNo memory followed the slider. Either the number typed did not match the")
        print("slider, or the game stores this volume in a form this tool does not look for.")
        return None

    print("\nFound %d place(s) that follow the slider." % addrs.size)
    print("Step 2: set Cars back to your normal level, close the menu, and sit in the car")
    print("with the engine audible.")
    input("Press Enter here when ready. Car audio will be switched off right away.")
    candidates = list(zip(addrs.tolist(), reps.tolist()))
    restore = mute(game, candidates)
    print("Switch to the game and listen, then come back here.")
    quiet = ask_yes("Did the car go quiet while the radio and other sound stayed? (y/n): ")
    unmute(game, candidates, restore)
    if not quiet:
        print("\nThe values changed but the sound did not. The game probably only applies the")
        print("Cars volume when the slider itself moves, so writing the stored value has no effect.")
        print("Run with --recalibrate and try once more, moving the slider by larger steps.")
        return None
    print("Calibration saved.")
    return candidates


def calibrate_shifting(game):
    """Find where the game keeps its Shifting setting, so the startup rev can switch to
    Manual for a moment. Returns the places found, or an empty list."""
    print("\nOne-time setup: find the game's Shifting setting. The startup rev then switches")
    print("to Manual for a moment and holds the brake, instead of holding the handbrake, which")
    print("sets off launch control. You will change Shifting a few times; the menu can stay open.")
    if not ask_yes("Do this now? (y/n): "):
        save_setting("switch_to_manual_for_rev", False)
        print("Skipped; the startup rev keeps using the handbrake. To be asked again, set")
        print("\"switch_to_manual_for_rev\" to true in settings.json.")
        return []
    print("In the game, open Settings > Difficulty and find Shifting.")
    input("Set Shifting to %s, then press Enter here." % SHIFTING_NAMES[SHIFTING_CLUTCH])
    print("Searching the game's memory...")
    addrs, _ = game.scan_bits(np.array([SHIFTING_CLUTCH], dtype=np.uint32), np.zeros(1, dtype=np.uint8))
    print("  %d possible matches." % addrs.size)
    for step, value in enumerate(SHIFTING_STEPS):
        if step >= MIN_SHIFTING_STEPS and addrs.size <= MAX_SHIFTING_PLACES:
            break
        input("Set Shifting to %s, then press Enter here." % SHIFTING_NAMES[value])
        words, readable = game.read_many(addrs)
        addrs = addrs[readable & (words == value)]
        print("  %d possible matches left." % addrs.size)
        if addrs.size == 0:
            break
    if not 0 < addrs.size <= MAX_SHIFTING_PLACES:
        print("Could not pin down the Shifting setting, so the startup rev keeps using the")
        print("handbrake. If the game only saves the change when you leave the menu, try again")
        print("with --recalibrate and back out of the menu after each change.")
        return []
    input("Found it. Set Shifting back to what you normally drive with, then press Enter here.")
    return [(int(a), 0) for a in addrs]


LAUNCH_RADIUS = 64


def _near_shifting(game, shifting, expected):
    """(shifting place, byte offset) pairs near it that currently read expected."""
    found = []
    for base in shifting:
        words = game.read_words(base - 4 * LAUNCH_RADIUS, LAUNCH_RADIUS * 2 + 1)
        if words is None:
            continue
        for index, word in enumerate(words):
            offset = (index - LAUNCH_RADIUS) * 4
            if offset != 0 and int(word) == expected:
                found.append((base, offset))
    return set(found)


def calibrate_launch(game, shifting):
    """Find Launch Control beside the Shifting setting. Returns the byte offsets, or []."""
    print("\nOne-time setup: find Launch Control. It is switched off with the engine,")
    print("and switched back on after the startup rev. The Difficulty menu can stay open.")
    if not ask_yes("Do this now? (y/n): "):
        print("Skipped. To be asked again, delete launch_control.json.")
        return []
    print("In the game, open Settings > Difficulty and find Launch Control.")
    input("Set Launch Control to Off, then press Enter here.")
    off = _near_shifting(game, shifting, 0)
    input("Set Launch Control to On, then press Enter here.")
    on = _near_shifting(game, shifting, 1)
    input("Set Launch Control to Off again, then press Enter here.")
    off_again = _near_shifting(game, shifting, 0)
    counts = collections.Counter(offset for _, offset in off & on & off_again)
    offsets = sorted(offset for offset, count in counts.items() if count >= 3)
    if not offsets:
        print("Could not pin down Launch Control. The engine toggle will leave it alone.")
        return []
    input("Found it. Set Launch Control back to what you want, then press Enter here.")
    print("Launch control saved.")
    return offsets


def load_launch_offsets():
    try:
        with open(LAUNCH_PATH, encoding="utf-8") as handle:
            offsets = json.load(handle).get("offsets", [])
    except (OSError, ValueError):
        return []
    return [int(offset) for offset in offsets]


def save_launch_offsets(offsets):
    with open(LAUNCH_PATH, "w", encoding="utf-8") as handle:
        json.dump({"offsets": offsets}, handle)


def plausible(value, rep):
    return value is not None and 0.0 <= value <= REPS[rep][2]


def mute(game, candidates):
    restore = []
    for addr, rep in candidates:
        value = game.read_f32(addr)
        if plausible(value, rep) and game.write_f32(addr, 0.0):
            restore.append(value)
        else:
            restore.append(None)
    return restore


def unmute(game, candidates, restore):
    for (addr, rep), value in zip(candidates, restore):
        if value is None:
            continue
        if game.read_f32(addr) == 0.0:
            game.write_f32(addr, value)


def load_session(game):
    if not os.path.exists(SESSION_PATH):
        return None
    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        session = json.load(f)
    if session.get("pid") != game.pid or session.get("created") != game.created:
        return None
    if not any(plausible(game.read_f32(a), r) for a, r in session["candidates"]):
        return None
    return session


def save_session(game, session):
    session.update(pid=game.pid, created=game.created)
    with open(SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)


def _comparable(words, start):
    """Which words of a memory window can be compared between game launches: not the
    volume itself, not zero, and not half of a pointer (those move every launch)."""
    keep = words != 0
    keep[FINGERPRINT_WORDS] = False
    for i in range(words.size - 1):
        if (start + 4 * i) % 8 == 0:
            q = int(words[i]) | (int(words[i + 1]) << 32)
            if 0x10000 <= q < 0x800000000000 and words[i + 1]:
                keep[i] = keep[i + 1] = False
    return keep


def volume_valid(word, rep):
    return 0.0 <= struct.unpack("<f", struct.pack("<I", int(word)))[0] <= REPS[rep][2]


def shifting_valid(word, _rep):
    return int(word) in SHIFTING_VALUES


def capture_fingerprints(game, candidates, path=None, valid=volume_valid):
    """The memory around each place, so the next launch can find them again without
    calibrating."""
    path = path or FINGERPRINTS_PATH
    prints = []
    for addr, rep in candidates:
        start = addr - 4 * FINGERPRINT_WORDS
        words = game.read_words(start, 2 * FINGERPRINT_WORDS + 1)
        if words is None or not valid(words[FINGERPRINT_WORDS], rep):
            continue
        keep = _comparable(words, start)
        if keep.sum() >= MIN_FINGERPRINT_WORDS and _anchors(words, keep):
            prints.append({"rep": rep, "align": start % 8, "words": words.tolist(), "keep": keep.astype(int).tolist()})
    if prints:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prints, f)
    return len(prints)


def _anchors(words, keep):
    """Window positions whose values are distinctive enough to search memory for:
    large, not round, not a common constant. Nearest the place first."""
    picks = [i for i in np.flatnonzero(keep)
             if words[i] > 0xFFFF and words[i] & 0xFFFF and int(words[i]) not in COMMON_WORDS]
    picks.sort(key=lambda i: abs(i - FINGERPRINT_WORDS))
    return picks[:FINGERPRINT_ANCHORS]


def find_from_fingerprints(game, path=None, label="the Cars volume", valid=volume_valid):
    """Look for last launch's places by the memory around them: search for a few
    distinctive values near each, then compare the whole window. Returns the
    candidates found, or None. Narrows each fingerprint to the words that matched,
    so it gets sharper every launch."""
    path = path or FINGERPRINTS_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            prints = json.load(f)
    except (OSError, ValueError):
        return None
    uses = collections.defaultdict(list)
    for n, p in enumerate(prints):
        words = np.array(p["words"], dtype=np.uint32)
        for i in _anchors(words, np.array(p["keep"], dtype=bool)):
            uses[int(words[i])].append((n, int(i)))
    if not uses:
        return None
    print("Looking for %s from last time..." % label)
    values = np.array(sorted(uses), dtype=np.uint32)
    hits, found_values = game.scan_bits(values, values)
    targets = collections.defaultdict(set)
    counts = collections.Counter(found_values.tolist())
    for addr, value in zip(hits.tolist(), found_values.tolist()):
        if counts[value] > ANCHOR_HIT_LIMIT:
            continue
        for n, i in uses[value]:
            targets[n].add(addr + 4 * (FINGERPRINT_WORDS - i))
    found, taken = [], set()
    for n, p in enumerate(prints):
        words = np.array(p["words"], dtype=np.uint32)
        keep = np.array(p["keep"], dtype=bool)
        best, best_score, best_window = None, FINGERPRINT_MATCH, None
        for addr in targets.get(n, ()):
            start = addr - 4 * FINGERPRINT_WORDS
            if addr in taken or start % 8 != p["align"]:
                continue
            window = game.read_words(start, 2 * FINGERPRINT_WORDS + 1)
            if window is None or not valid(window[FINGERPRINT_WORDS], p["rep"]):
                continue
            score = (window[keep] == words[keep]).mean()
            if score >= best_score:
                best, best_score, best_window = addr, score, window
        if best is not None:
            taken.add(best)
            found.append((best, p["rep"]))
            narrowed = keep & (best_window == words)
            if narrowed.sum() >= MIN_FINGERPRINT_WORDS and _anchors(words, narrowed):
                p["keep"] = narrowed.astype(int).tolist()
    if not found:
        print("  Not found.")
        return None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prints, f)
    print("  Found %d of %d place(s)." % (len(found), len(prints)))
    return found


def _fade(signal, fade_in, fade_out):
    n_in = int(SAMPLE_RATE * fade_in)
    n_out = int(SAMPLE_RATE * fade_out)
    signal[:n_in] *= np.linspace(0.0, 1.0, n_in)
    signal[-n_out:] *= np.linspace(1.0, 0.0, n_out)
    return signal


def _biquad(signal, kind, freq, q=0.707):
    """One RBJ-cookbook filter section: "low" pass or "band" pass."""
    w = 2 * np.pi * freq / SAMPLE_RATE
    alpha = np.sin(w) / (2 * q)
    cos_w = np.cos(w)
    if kind == "low":
        b = ((1 - cos_w) / 2, 1 - cos_w, (1 - cos_w) / 2)
    else:
        b = (alpha, 0.0, -alpha)
    a0 = 1 + alpha
    b0, b1, b2 = (c / a0 for c in b)
    a1, a2 = -2 * cos_w / a0, (1 - alpha) / a0
    out = np.empty_like(signal)
    x1 = x2 = y1 = y2 = 0.0
    for i, x in enumerate(signal.tolist()):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y
        x2, x1, y2, y1 = x1, x, y1, y
    return out


def make_crank(cylinders=DEFAULT_CYLINDERS, seconds=1.3):
    """A starter motor turning the engine over: the solenoid clunks in, then the motor
    whirs and labours on each compression stroke, with a puff of air through the
    intake and exhaust as each stroke lets go. A four-stroke engine has a compression
    stroke for every cylinder each two crank turns, so more cylinders chuff faster and
    smoother."""
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    rng = np.random.default_rng(6)
    engaged = np.clip((t - 0.04) / 0.08, 0.0, 1.0)

    crank_rpm = 170.0 + 70.0 * (1 - np.exp(-t * 6))
    stroke_hz = crank_rpm / 60.0 * cylinders / 2.0
    stroke_phase = 2 * np.pi * np.cumsum(stroke_hz) / SAMPLE_RATE
    sharpness = 4 if cylinders <= 4 else 3 if cylinders <= 6 else 2
    compression = (0.5 - 0.5 * np.cos(stroke_phase)) ** sharpness
    motor_speed = 1.0 - 0.28 * min(1.0, 4.0 / cylinders) * compression

    motor_phase = 2 * np.pi * np.cumsum(170 * motor_speed) / SAMPLE_RATE
    motor = sum(np.sin(k * motor_phase) * 0.8 ** k for k in range(1, 9))
    gear = 0.15 * np.sin(2 * np.pi * np.cumsum(1250 * motor_speed) / SAMPLE_RATE)
    release = np.roll(compression, int(SAMPLE_RATE * 0.02))
    chuff = _biquad(rng.standard_normal(n), "band", 450, q=1.2) * release ** 2
    thump = np.sin(2 * np.pi * 40 * t) * compression ** 2
    cranking = (0.35 * motor * (0.6 + 0.4 * compression) + gear + 0.9 * chuff + 0.5 * thump) * engaged

    clunk = np.sin(2 * np.pi * 95 * t) * np.exp(-t * 60)
    clunk += 0.5 * _biquad(rng.standard_normal(n), "band", 1800, q=2.0) * np.exp(-t * 120)

    signal = _biquad(cranking + 1.2 * clunk, "low", 3500)
    return _fade(signal, 0.002, 0.08)


def make_click(size):
    """A key-turn click: two small ticks as the key passes its detent."""
    t = np.arange(size) / SAMPLE_RATE
    tick = _biquad(np.random.default_rng(9).standard_normal(size), "band", 3200, q=1.5)
    second = np.clip(t - 0.035, 0.0, None)
    return tick * (np.exp(-t * 700) + 0.5 * np.exp(-second * 700) * (t >= 0.035))


def make_shutoff(seconds=0.6):
    """The key-turn click and the block easing into a shudder and stopping."""
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    click = make_click(t.size)
    shudder_phase = 2 * np.pi * np.cumsum(55 - 25 * t / seconds) / SAMPLE_RATE
    attack = 1 - np.exp(-np.clip(t - 0.04, 0.0, None) * 20)
    shudder = np.sin(shudder_phase) * (0.6 + 0.4 * np.sin(2 * np.pi * 7 * t)) * np.exp(-t * 5) * attack
    return _fade(0.3 * click + 0.6 * shudder, 0.001, 0.08)


def write_wav(path, signal, rate=SAMPLE_RATE, gain=SOUND_GAIN, normalize=True):
    if normalize:
        peak = np.max(np.abs(signal)) or 1.0
        scaled = signal / peak * gain
    else:
        scaled = signal * gain
    data = np.clip(scaled * 32767, -32767, 32767).astype("<i2").tobytes()
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(data)


def read_wav(path):
    with wave.open(path, "rb") as f:
        rate, channels = f.getframerate(), f.getnchannels()
        data = np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float64) / 32768
    return data.reshape(-1, channels).mean(axis=1), rate


def horn_crank():
    """The start of the game's own "Car Failing" horn, faded out for the crossfade.
    None if the horn or the converter is missing."""
    seconds = HORN_CRANK_SECONDS
    path = os.path.join(SOUNDS_DIR, "horn_crank_%dms_x%d.wav" % (round(seconds * 1000), round(HORN_SPEED * 100)))
    if os.path.exists(path):
        return path
    full = os.path.join(SOUNDS_DIR, "horns", "Car_Failing.wav")
    if not os.path.exists(full):
        if GAME_DIR is None:
            return None
        bank = os.path.join(GAME_DIR, "media", "Audio", "FMODBanks", "Horn_SFX_Car_Failing.assets.bank")
        if not (os.path.exists(VGMSTREAM) and os.path.exists(bank)):
            return None
        os.makedirs(os.path.dirname(full), exist_ok=True)
        subprocess.run([VGMSTREAM, "-o", full, bank], capture_output=True)
        if not os.path.exists(full):
            return None
    signal, rate = read_wav(full)
    positions = np.arange(int(rate * seconds)) * HORN_SPEED
    positions = positions[positions < signal.size - 1]
    clip = np.interp(positions, np.arange(signal.size), signal)
    fade = int(rate * min(HORN_CROSSFADE, seconds))
    clip[-fade:] *= np.linspace(1.0, 0.0, fade)
    write_wav(path, clip, rate)
    return path


def wav_seconds(path):
    with wave.open(path, "rb") as f:
        return f.getnframes() / float(f.getframerate())


def clicked_custom_shutoff():
    """Your shutoff.wav with the key-turn click in front of it, at half its loudness."""
    stamp = int(os.path.getmtime(CUSTOM_SHUTOFF_WAV))
    path = os.path.join(SOUNDS_DIR, "shutoff_custom_%d.wav" % stamp)
    if not os.path.exists(path):
        signal, rate = read_wav(CUSTOM_SHUTOFF_WAV)
        level = np.max(np.abs(signal)) or 1.0
        click = make_click(int(SAMPLE_RATE * CLICK_LEAD))
        click = np.interp(np.arange(int(rate * CLICK_LEAD)) * SAMPLE_RATE / rate, np.arange(click.size), click)
        click *= 0.5 * level / (np.max(np.abs(click)) or 1.0)
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        write_wav(path, np.concatenate([click, signal]), rate, gain=level)
    return path


def click_sound():
    """The key-turn click on its own, as loud as it is in the generated shutoff sound."""
    path = os.path.join(SOUNDS_DIR, "click_v%d.wav" % round(SHUTOFF_GAIN * 100))
    if not os.path.exists(path):
        shutoff = make_shutoff()
        click = _fade(0.3 * make_click(int(SAMPLE_RATE * 0.08)), 0.001, 0.02)
        level = np.max(np.abs(click)) / np.max(np.abs(shutoff))
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        write_wav(path, click, gain=SHUTOFF_GAIN * level)
    return path


def shutoff_sound():
    """shutoff.wav next to the program if you put one there (with the click put in
    front), else the generated one at the shutoff_volume setting."""
    if os.path.exists(CUSTOM_SHUTOFF_WAV):
        try:
            return clicked_custom_shutoff()
        except (OSError, wave.Error, EOFError) as e:
            print("shutoff.wav could not be read (%s); using the built-in sound." % e)
    path = os.path.join(SOUNDS_DIR, "shutoff_v%d.wav" % round(SHUTOFF_GAIN * 100))
    if not os.path.exists(path):
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        write_wav(path, make_shutoff(), gain=SHUTOFF_GAIN)
    return path


def _car_ordinal(data, media):
    """The id in front of the car's name inside its carbin. Data Out sends this id."""
    token = media.lower().encode("ascii")
    at = data.find(token)
    if at < 8:
        return None
    if struct.unpack_from("<I", data, at - 4)[0] != len(token):
        return None
    return struct.unpack_from("<I", data, at - 8)[0]


def startup_index():
    """Car id to the game's AV_STARTUP recording, from the audio config and each carbin."""
    if GAME_DIR is None:
        return {}
    config_path = os.path.join(GAME_DIR, "media", "Audio", "Cars", "ModularCarConfig.xml")
    models_dir = os.path.join(GAME_DIR, "media", "Audio", "ModularCars")
    cars_dir = os.path.join(GAME_DIR, "media", "Cars")
    try:
        config_mtime = os.path.getmtime(config_path)
    except OSError:
        return {}
    if os.path.exists(STARTUP_INDEX_PATH) and os.path.getmtime(STARTUP_INDEX_PATH) >= config_mtime:
        try:
            with open(STARTUP_INDEX_PATH, encoding="utf-8") as handle:
                return {int(key): value for key, value in json.load(handle).items()}
        except (OSError, ValueError):
            pass
    try:
        config = open(config_path, encoding="utf-8").read()
    except OSError:
        return {}
    print("Matching each car to its startup sound...")
    groups = {}
    for match in re.finditer(r'<PropertyTemplate Name="(Forzavista/[^"]+)"([^>]*)>', config):
        startup = re.search(r'AV_STARTUP="([^"]+)"', match.group(2))
        if startup and startup.group(1) != "None":
            groups[match.group(1)] = startup.group(1)
    index = {}
    for model_path in glob.glob(os.path.join(models_dir, "*-Model.xml")):
        media = os.path.basename(model_path)[: -len("-Model.xml")]
        try:
            model = open(model_path, encoding="utf-8").read()
        except OSError:
            continue
        source = re.search(r'PropertyTemplate Source="(Forzavista/[^"]+)"', model)
        bank = groups.get(source.group(1)) if source else None
        engine_path = os.path.join(models_dir, media + "-Engine.xml")
        if os.path.exists(engine_path):
            try:
                engine = open(engine_path, encoding="utf-8").read()
            except OSError:
                engine = ""
            override = re.search(r'<Property Name="AV_STARTUP" Value="([^"]+)"', engine)
            if override and override.group(1) != "None":
                bank = override.group(1)
        if not bank:
            continue
        carbin = os.path.join(cars_dir, media + ".zip")
        try:
            with zipfile.ZipFile(carbin) as archive:
                ordinal = _car_ordinal(archive.read(media + ".carbin"), media)
        except (OSError, KeyError, zipfile.BadZipFile):
            continue
        if ordinal is not None:
            index[ordinal] = bank
    try:
        with open(STARTUP_INDEX_PATH, "w", encoding="utf-8") as handle:
            json.dump({str(key): value for key, value in index.items()}, handle)
    except OSError:
        pass
    return index


def game_startup_wav(car):
    """The game's own startup recording for this car, converted once and cached."""
    bank = startup_index().get(car)
    if not bank or GAME_DIR is None or not os.path.exists(VGMSTREAM):
        return None
    path = os.path.join(SOUNDS_DIR, "startup", "%s_g%d_flat.wav" % (bank, round(STARTUP_CRANK_GAIN * 100)))
    if os.path.exists(path):
        return path
    source = os.path.join(GAME_DIR, "media", "Audio", "FMODBanks", bank + ".assets.bank")
    if not os.path.exists(source):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    full = path + ".full.wav"
    subprocess.run([VGMSTREAM, "-o", full, source], capture_output=True)
    if not os.path.exists(full):
        return None
    signal, rate = read_wav(full)
    write_wav(path, shape_crank(signal), rate, gain=STARTUP_CRANK_GAIN, normalize=False)
    try:
        os.remove(full)
    except OSError:
        pass
    return path if os.path.exists(path) else None


def startup_sound(telemetry):
    """The car's own recording if there is one, else the game's startup for that car's
    group, else startup.wav, else a starter generated for the car's cylinder count."""
    car = telemetry.car if telemetry is not None else None
    if car is not None:
        path = os.path.join(SOUNDS_DIR, "car_%d_startup.wav" % car)
        if os.path.exists(path):
            return path
        path = game_startup_wav(car)
        if path:
            return path
    if not PLAY_STARTUP_SOUND:
        return None
    if os.path.exists(STARTUP_WAV):
        return STARTUP_WAV
    horn = horn_crank()
    if horn is not None:
        return horn
    cylinders = (telemetry.cylinders if telemetry is not None else None) or DEFAULT_CYLINDERS
    path = os.path.join(SOUNDS_DIR, "crank_%dcyl_%dms.wav" % (cylinders, round(CRANK_SECONDS * 1000)))
    if not os.path.exists(path):
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        write_wav(path, make_crank(cylinders, CRANK_SECONDS))
    return path


def shape_crank(signal):
    """The whole crank, at an even level until it ends."""
    return signal.copy()


def startup_overlap(path):
    """How long before the startup sound ends the engine starts coming back."""
    if os.path.basename(path).startswith("horn_crank_"):
        return HORN_CROSSFADE
    if "_flat" in os.path.basename(path):
        return wav_seconds(path) * STARTUP_CRANK_OVERLAP
    return CUSTOM_SOUND_OVERLAP


def play(path):
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


def envelope_at(points, t):
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


class Engine:
    """Engine on/off state. Sequences run on a worker thread so the keyboard hook on
    the main thread keeps answering."""

    def __init__(self, game, session, telemetry, pad):
        self.game = game
        self.session = session
        self.telemetry = telemetry
        self.pad = pad
        self.candidates = [tuple(c) for c in session["candidates"]]
        self.state = "off" if session["muted"] else "on"
        self.busy = threading.Lock()
        self.startup_path = None
        self.catching = False
        self.lookahead = {}
        self.shifting = list(session.get("shifting", []))
        self.shift_restore = None
        self.hold_with_brake = False
        self._said_switch = False
        self.launch_offsets = list(session.get("launch_offsets", []))
        self.launch_was_on = False
        self.shutoff_path = shutoff_sound()
        self.click_path = click_sound()
        if pad is not None:
            pad.mode = self.pad_mode
            pad.driving = lambda: telemetry is not None and telemetry.fresh()
            pad.on_toggle = lambda: self.toggle() if game_focused() else None
            if PARK_HOLD:
                threading.Thread(target=self._park_hold, daemon=True).start()

    def moving(self):
        """Still rolling faster than PARK_HOLD_SPEED, by Data Out."""
        tm = self.telemetry
        return tm is not None and tm.fresh() and tm.speed is not None and tm.speed >= PARK_HOLD_SPEED

    def pad_mode(self):
        if not self.blocking:
            return "on"
        return "moving" if self.moving() else "stopped"

    def _park_hold(self):
        """Hold the virtual handbrake while the engine is off and the car is all but
        stopped. The startup sequence takes the controller over while it runs."""
        while True:
            tm = self.telemetry
            if not self.catching:
                self.pad.handbrake = (
                    self.state != "on"
                    and tm is not None
                    and tm.fresh()
                    and tm.speed is not None
                    and tm.speed < PARK_HOLD_SPEED
                )
            time.sleep(0.02)

    @property
    def blocking(self):
        return self.state != "on"

    def toggle(self):
        if not self.busy.acquire(blocking=False):
            return
        step = self._start if self.state == "off" else self._stop
        threading.Thread(target=self._run, args=(step,), daemon=True).start()

    def _run(self, step):
        try:
            step()
        finally:
            self.busy.release()

    def _save(self):
        save_session(self.game, self.session)

    def _set_level(self, restore, fraction):
        for (addr, rep), level in zip(self.candidates, restore):
            if level is not None:
                self.game.write_f32(addr, min(level * fraction, REPS[rep][2]))

    def _envelope(self, points, restore):
        start = time.perf_counter()
        while True:
            t = time.perf_counter() - start
            self._set_level(restore, envelope_at(points, t))
            if t >= points[-1][0]:
                return
            time.sleep(ENVELOPE_STEP)

    def _stop(self):
        restore = []
        for addr, rep in self.candidates:
            value = self.game.read_f32(addr)
            restore.append(value if plausible(value, rep) else None)
        if all(v is None for v in restore):
            print("The Cars volume is no longer where it was. Run with --recalibrate.")
            return
        self.state = "stopping"
        self.session.update(muted=True, restore=restore)
        self._save()
        play(self.shutoff_path)
        time.sleep(CLICK_LEAD)
        self._envelope(SHUTOFF_FADE, restore)
        self.state = "off"
        self._disable_launch()
        if PLAY_STARTUP_SOUND:
            self.startup_path = startup_sound(self.telemetry)
        car = self.telemetry.car if self.telemetry is not None else None
        if car is not None:
            print("Engine off. Car number %d; its own startup sound would be sounds\\car_%d_startup.wav" % (car, car))
        else:
            print("Engine off.")

    def _start(self):
        restore = self.session["restore"]
        self.state = "starting"
        self._choose_hold()
        play(self.click_path)
        time.sleep(CLICK_LEAD)
        sound = self.startup_path if PLAY_STARTUP_SOUND else None
        sound = sound or startup_sound(self.telemetry)
        if sound:
            print("Starting: playing %s" % os.path.relpath(sound, HERE))
            play(sound)
            length = wav_seconds(sound)
            overlap = startup_overlap(sound)
            ready_at = time.perf_counter() + max(0.0, length - overlap)
            while time.perf_counter() < ready_at:
                if self.hold_with_brake:
                    self._manual_now()
                time.sleep(0.002)
            rev_for = length * (STARTUP_CRANK_OVERLAP + STARTUP_REV_AFTER) if "_flat" in os.path.basename(sound) else None
        else:
            print("Starting.")
            overlap = None
            rev_for = None
        winmm.timeBeginPeriod(1)
        self.catching = True
        try:
            self._catch(restore, overlap, rev_for)
            self._restore_launch()
            self.session.update(muted=False, restore=[])
            self._save()
            self.state = "on"
        finally:
            winmm.timeEndPeriod(1)
            self.catching = False
        print("Engine running.")

    def _launch_addresses(self):
        return [base + offset for base in self.shifting for offset in self.launch_offsets]

    def _set_launch(self, on):
        wrote = 0
        for addr in self._launch_addresses():
            current = self.game.read_bits(addr)
            if current not in (0, 1):
                continue
            if self.game.write_u32(addr, 1 if on else 0):
                wrote += 1
        return wrote

    def _disable_launch(self):
        """Turn Launch Control off while the engine is off, and remember if it was on."""
        if not self.launch_offsets:
            return
        self.launch_was_on = any(self.game.read_bits(addr) == 1 for addr in self._launch_addresses())
        if self.launch_was_on and self._set_launch(False):
            print("  Launch control off.")

    def _restore_launch(self):
        """Turn Launch Control back on after the startup rev, when it was on before."""
        if self.launch_was_on and self._set_launch(True):
            print("  Launch control on.")
        self.launch_was_on = False

    def _shifting_now(self):
        """What each Shifting place holds now, and the setting most of them agree on.
        Not every copy the game keeps follows the setting in use."""
        snapshot = {addr: self.game.read_bits(addr) for addr in self.shifting}
        votes = collections.Counter(w for w in snapshot.values() if w in SHIFTING_VALUES)
        return snapshot, (votes.most_common(1)[0][0] if votes else None)

    def _choose_hold(self):
        """Hold the car with the brake during the startup rev when it is in Manual,
        switching it there from Automatic for the rev; otherwise with the handbrake.
        At a standstill in Automatic the brake is the reverse throttle."""
        self.hold_with_brake = False
        if self.pad is None or not self.shifting:
            return
        snapshot, current = self._shifting_now()
        if current == SHIFTING_CLUTCH:
            return
        self.hold_with_brake = True
        self._said_switch = False
        if any(word == SHIFTING_AUTOMATIC for word in snapshot.values()):
            self.shift_restore = snapshot

    def _manual_now(self):
        """Write Manual over every Automatic copy and report whether any Automatic remains.
        The brake must wait until this is clear, because a held brake in Automatic reverses."""
        if not self.shifting:
            return False
        snapshot, _ = self._shifting_now()
        if self.shift_restore is None and any(word == SHIFTING_AUTOMATIC for word in snapshot.values()):
            self.shift_restore = snapshot
        automatic = False
        for addr, word in snapshot.items():
            if word == SHIFTING_AUTOMATIC:
                automatic = True
                self.game.write_u32(addr, SHIFTING_MANUAL)
        if automatic and not self._said_switch:
            self._said_switch = True
            print("  Switched to Manual for the rev.")
        return not automatic

    def _restore_shifting(self):
        """Put every Shifting place back exactly as it was before the rev."""
        if self.shift_restore is not None:
            for addr, word in self.shift_restore.items():
                if word is not None:
                    self.game.write_u32(addr, word)
            print("  Restored the previous Shifting setting.")
            self.shift_restore = None

    def _measuring(self):
        tm = self.telemetry
        return tm is not None and tm.fresh()

    def _rev_target(self):
        """75% of this car's redline. The throttle comes off as soon as the revs get there."""
        tm = self.telemetry
        if tm is not None and tm.max_rpm:
            return 0.75 * tm.max_rpm
        if tm is not None and tm.idle_rpm:
            return FLARE_IDLE_MULTIPLE * tm.idle_rpm
        return None

    def _flare_done(self, elapsed, lookahead):
        if elapsed >= FLARE_MAX_SECONDS:
            return True
        if not self._measuring():
            return elapsed >= FLARE_SECONDS
        tm = self.telemetry
        target = self._rev_target()
        if target is None:
            return elapsed >= FLARE_SECONDS
        return tm.rpm() >= target

    def _learn_lookahead(self, car, lookahead, peak, target, release_rate):
        """Let go earlier next time by however long the revs kept climbing past the
        target, or later if they fell short."""
        if release_rate <= 0:
            return
        miss = (peak - target) / release_rate
        self.lookahead[car] = min(FLARE_LOOKAHEAD_MAX, max(0.0, lookahead + LOOKAHEAD_LEARNING * miss))

    def _settled(self, since_release):
        if since_release >= HANDBRAKE_TAIL_MAX_SECONDS:
            return True
        if not self._measuring():
            return since_release >= HANDBRAKE_TAIL
        tm = self.telemetry
        target = self._rev_target()
        return tm.rpm() <= tm.idle_rpm + SETTLED_FRACTION * (target - tm.idle_rpm)

    def _catch(self, restore, overlap=None, rev_for=None):
        """Fade the engine audio back in while the brake and handbrake are both held and
        the throttle revs the engine. Both are released once the revs are back near idle.
        overlap is how long the crank is still fading as this begins. rev_for, when set,
        is how long the throttle stays in before it is released."""
        rise = overlap if overlap else CATCH_RISE
        if overlap:
            envelope = ((0.0, STARTUP_LOUDNESS), (STARTUP_BOOST_HOLD, STARTUP_LOUDNESS),
                        (STARTUP_BOOST_HOLD + STARTUP_BOOST_FADE, 1.0))
        else:
            envelope = ((0.0, 0.0), (rise, STARTUP_LOUDNESS), (rise + STARTUP_BOOST_HOLD, STARTUP_LOUDNESS),
                        (rise + STARTUP_BOOST_HOLD + STARTUP_BOOST_FADE, 1.0))
        pad = self.pad if game_focused() else None
        if self.pad is not None and pad is None:
            print("  The game was not in front, so the engine was not revved.")
        tm = self.telemetry
        before = tm.rpm() if tm is not None and tm.fresh() else None
        peak = before
        flare_end = None
        settled_at = None
        release_rate = 0.0
        car = tm.car if tm is not None else None
        lookahead = self.lookahead.get(car, FLARE_LOOKAHEAD)
        ramp = TRIGGER_RAMP if before is None else 0.0
        start = time.perf_counter()
        try:
            while True:
                t = time.perf_counter() - start
                if before is not None and tm.fresh():
                    peak = max(peak, tm.rpm())
                self._set_level(restore, envelope_at(envelope, t))
                if pad is not None:
                    if self.hold_with_brake and not self._manual_now():
                        if t > 1.0:
                            print("  Left the rev. Shifting was still Automatic, and the brake would reverse the car.")
                            return
                        pad.throttle = 0.0
                        pad.brake = None
                        pad.handbrake = False
                        time.sleep(0.002)
                        continue
                    if flare_end is None and self._flare_done(t, lookahead):
                        flare_end = t
                        if self._measuring():
                            release_rate = tm.rate(RATE_WINDOW)
                    if flare_end is not None and settled_at is None and self._settled(t - flare_end):
                        settled_at = t
                    if flare_end is not None:
                        pad.throttle = 0.0
                    else:
                        pad.throttle = TRIGGER_MAX * STARTUP_REV_RATE * (min(1.0, t / ramp) if ramp else 1.0)
                    holding = settled_at is None
                    pad.brake = 1.0 if holding else None
                    pad.handbrake = holding
                pad_done = pad is None or settled_at is not None
                if t >= envelope[-1][0] and pad_done:
                    return
                time.sleep(0.002)
        finally:
            self._set_level(restore, 1.0)
            if pad is not None:
                pad.throttle = None
                pad.brake = None
                pad.handbrake = False
            if self.shift_restore is not None:
                time.sleep(SHIFT_BACK_DELAY)
                self._restore_shifting()
            if pad is not None:
                if before is None:
                    print("  Revved for %.2f s; no Data Out, so the revs could not be measured." % (flare_end or 0.0))
                else:
                    target = self._rev_target()
                    print("  Revs went from %.0f to a peak of %.0f (target %.0f): up in %.2f s, back"
                          " near idle %.2f s later." % (
                              before, peak, target, flare_end or 0.0,
                              (settled_at - flare_end) if settled_at is not None and flare_end is not None else 0.0))
                    self._learn_lookahead(car, lookahead, peak, target, release_rate)

    def restore_now(self):
        self._restore_shifting()
        if self.session["muted"]:
            self._set_level(self.session["restore"], 1.0)
            self.session.update(muted=False, restore=[])
            self._save()
            self.state = "on"


def install_key_block(engine):
    """Swallow throttle and brake key presses while the engine is off. Key releases
    always pass, so a key held at shutoff is never left stuck down in the game."""

    held = set()

    def on_key(n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.flags & LLKHF_UP:
                held.discard(kb.vkCode)
            elif (
                engine.blocking
                and (kb.vkCode in THROTTLE_KEYS or (kb.vkCode in BRAKE_KEYS and not engine.moving()))
                and not kb.flags & LLKHF_INJECTED
                and game_focused()
            ):
                if kb.vkCode not in held:
                    held.add(kb.vkCode)
                    print("  blocked key 0x%02X" % kb.vkCode)
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    proc = HOOKPROC(on_key)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    return proc


def service_running(name):
    out = subprocess.run(["sc", "query", name], capture_output=True, text=True).stdout
    return "RUNNING" in out


def wait_for_telemetry(telemetry, seconds=3.0):
    deadline = time.time() + seconds
    while telemetry is not None and not telemetry.fresh() and time.time() < deadline:
        time.sleep(0.1)
    return telemetry is not None and telemetry.fresh()


def steam_input_disabled():
    """True if Steam Input is switched off for the game in the most recently used Steam
    account, False if not, None if this is not a Steam copy or Steam cannot be read."""
    if GAME_DIR is None or os.path.basename(os.path.dirname(os.path.dirname(GAME_DIR))).lower() != "steamapps":
        return None
    steam = os.path.dirname(os.path.dirname(os.path.dirname(GAME_DIR)))
    configs = glob.glob(os.path.join(steam, "userdata", "*", "config", "localconfig.vdf"))
    if not configs:
        return None
    try:
        with open(max(configs, key=os.path.getmtime), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    found = re.search(r'"%s"\s*\{[^{}]*?"UseSteamControllerConfig"\s*"(\d+)"' % STEAM_APP_ID, text)
    return found is not None and found.group(1) == "0"


def check_setup(telemetry):
    """Print what is ready and what each missing piece switches off. Returns whether
    the virtual controller driver is there."""
    vigem = service_running("ViGEmBus")
    hidhide = os.path.exists(HIDHIDE_CLI) and service_running("HidHide")
    data_out = wait_for_telemetry(telemetry)
    print("\nSetup check")
    print("  [ok] Forza Horizon 6 is running (%s)" % (GAME_DIR or "folder unknown"))
    if data_out:
        print("  [ok] Data Out telemetry: %.0f rpm idle, %.0f redline" % (telemetry.idle_rpm, telemetry.max_rpm))
    elif telemetry is None:
        print("  [--] Data Out: port %d is in use by another program; close it and restart." % TELEMETRY_PORT)
    else:
        print("  [--] Data Out not received yet. In the game: Settings > HUD and Gameplay >")
        print("       Data Out = On, IP 127.0.0.1, port %d, then drive. Until then the startup rev" % TELEMETRY_PORT)
        print("       is a fixed length, and the minimum brake and parking hold are off.")
    if vigem:
        print("  [ok] ViGEmBus virtual controller driver")
    else:
        print("  [--] ViGEmBus is not installed: no startup rev, minimum brake or parking hold.")
        print("       Install it from %s and restart this program." % VIGEMBUS_URL)
    if hidhide:
        print("  [ok] HidHide")
    else:
        print("  [--] HidHide is not installed: your controller's triggers are not cut while the")
        print("       engine is off. Install it from %s and restart this program." % HIDHIDE_URL)
    if steam_input_disabled() is False:
        print("  [--] Steam Input may be on for this game, which passes your controller to the game")
        print("       around this tool. In Steam: right-click Forza Horizon 6 > Properties >")
        print("       Controller > Disable Steam Input, then restart the game.")
    print()
    return vigem


def hold_single_instance():
    """Stop a second copy opening. The watcher uses the same mark to see this one is open."""
    global _instance
    _instance = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(_instance)
        _instance = None
        print("FH6 Engine Toggle is already open.")
        return False
    return True


def mod_is_open():
    handle = kernel32.OpenMutexW(0x00100000, False, INSTANCE_MUTEX)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def launch_mod():
    """A visible window of this program, without the watcher flag."""
    if FROZEN:
        command = [sys.executable]
    else:
        command = ["py", "-3.8", os.path.abspath(__file__)]
    subprocess.Popen(command, creationflags=0x00000010, cwd=HERE)


def app_enabled():
    """False only when settings.json says the tool is turned off."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            return json.load(handle).get("enabled", True) is not False
    except (OSError, ValueError):
        return True


def watch_for_game():
    """Stay running in the background and open the toggle whenever the game starts."""
    launched = 0.0
    while True:
        if app_enabled() and find_game_pid() is not None and not mod_is_open() and time.time() - launched > 15:
            launch_mod()
            launched = time.time()
        time.sleep(2)


def watcher_command():
    """The hidden command that waits for the game and opens this program."""
    if FROZEN:
        return '"%s" --watch' % sys.executable
    return 'py -3.8 "%s" --watch' % os.path.abspath(__file__)


def install_watcher():
    """Run the watcher at every Windows sign-in, with no window of its own."""
    startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    path = os.path.join(startup, "FH6 Engine Toggle.vbs")
    command = watcher_command().replace('"', '""')
    content = 'CreateObject("Wscript.Shell").Run "%s", 0, False\r\n' % command
    os.makedirs(startup, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _tool_processes():
    """Other copies of this program, by process id."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*fh6_car_audio_mem*' -or $_.CommandLine -like '*FH6EngineToggle*' } | Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True,
    ).stdout
    return [int(line) for line in out.split() if line.isdigit() and int(line) != os.getpid()]


def _stop_process(pid):
    kernel32.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)
    kernel32.TerminateProcess.restype = wt.BOOL
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return
    kernel32.TerminateProcess(handle, 0)
    kernel32.CloseHandle(handle)


def turn_off():
    """Stop the open tool and leave it stopped until Turn On."""
    load_settings()
    save_setting("enabled", False)
    user32.FindWindowW.argtypes = (wt.LPCWSTR, wt.LPCWSTR)
    user32.FindWindowW.restype = wt.HWND
    user32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    window = user32.FindWindowW(None, "FH6 Engine Toggle")
    if window:
        user32.PostMessageW(window, 0x0010, 0, 0)
        for _ in range(20):
            if not mod_is_open():
                break
            time.sleep(0.2)
    if os.path.exists(HIDHIDE_CLI):
        unroute_controller()
    for pid in _tool_processes():
        _stop_process(pid)
    print("Turned off. Your controller is yours again.")
    print("Double-click FH6 Turn On.bat when you want it back.")


def turn_on():
    """Let the tool open with the game again, and open it now if the game is running."""
    load_settings()
    save_setting("enabled", True)
    if not _tool_processes():
        if FROZEN:
            command = [sys.executable, "--watch"]
        else:
            command = ["py", "-3.8", os.path.abspath(__file__), "--watch"]
        subprocess.Popen(command, creationflags=0x08000000, cwd=HERE)
    print("Turned on. It opens when Forza Horizon 6 is running.")


def write_debug_report():
    """A text file of this PC's setup, for sending when something goes wrong.
    No memory dump and no controller identifiers."""
    load_settings()
    lines = [
        "FH6 Engine Toggle debug report",
        "version: %s" % VERSION,
        "packaged: %s" % FROZEN,
        "windows: %s" % (sys.getwindowsversion(),),
        "enabled: %s" % app_enabled(),
        "settings: hotkey=%s button=%s start_game=%s rev=%s rev_multiple=%s "
        "manual_for_rev=%s min_brake=%s park_hold=%s cut_controller=%s "
        "telemetry_port=%s" % (
            HOTKEY_NAME, TOGGLE_BUTTON_NAME, START_GAME, REV_WITH_PAD, FLARE_IDLE_MULTIPLE,
            SWITCH_TO_MANUAL, OFF_BRAKE, PARK_HOLD, PASS_THROUGH, TELEMETRY_PORT,
        ),
        "vigem_running: %s" % service_running("ViGEmBus"),
        "hidhide_installed: %s" % os.path.exists(HIDHIDE_CLI),
        "hidhide_running: %s" % service_running("HidHide"),
        "vgmstream: %s" % os.path.exists(VGMSTREAM),
    ]
    try:
        with open(STARTUP_INDEX_PATH, encoding="utf-8") as handle:
            lines.append("startup_index_cars: %d" % len(json.load(handle)))
    except (OSError, ValueError):
        lines.append("startup_index_cars: missing")
    lines.append("volume_fingerprints: %s" % os.path.exists(FINGERPRINTS_PATH))
    lines.append("shifting_fingerprints: %s" % os.path.exists(SHIFTING_FINGERPRINTS_PATH))
    lines.append("launch_control_file: %s" % os.path.exists(LAUNCH_PATH))
    pid = find_game_pid()
    lines.append("game_running: %s" % (pid or "no"))
    if pid:
        try:
            game = Game(pid)
            folder = game_folder(game.handle)
            lines.append("game_folder: %s" % folder)
            global GAME_DIR
            GAME_DIR = folder
            lines.append("steam_input_disabled: %s" % steam_input_disabled())
        except OSError as error:
            lines.append("game_open_failed: %s" % error)
    telemetry = start_telemetry()
    if telemetry is None:
        lines.append("data_out: port %s is in use or could not be opened" % TELEMETRY_PORT)
    else:
        lines.append("data_out: %s" % ("receiving" if wait_for_telemetry(telemetry, 2.0) else "no packets yet"))
        if telemetry.fresh():
            lines.append("data_out_rpm: idle=%s max=%s now=%s" % (
                telemetry.idle_rpm, telemetry.max_rpm, telemetry.rpm()))
    try:
        with open(SESSION_PATH, encoding="utf-8") as handle:
            session = json.load(handle)
        lines.append("session: places=%s muted=%s pid=%s" % (
            len(session.get("candidates") or []), session.get("muted"), session.get("pid")))
    except (OSError, ValueError):
        lines.append("session: none")
    path = os.path.join(HERE, "FH6-debug-report.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("Wrote %s" % path)
    print("Send that file when you ask for a fix. It has no memory dump.")
    return path


def main():
    global GAME_DIR
    if "--watch" in sys.argv:
        watch_for_game()
        return 0
    if "--off" in sys.argv:
        turn_off()
        return 0
    if "--on" in sys.argv:
        turn_on()
        return 0
    if "--debug-report" in sys.argv:
        write_debug_report()
        return 0
    kernel32.SetConsoleTitleW("FH6 Engine Toggle")
    load_settings()
    if not app_enabled():
        print("FH6 Engine Toggle is turned off.")
        print("Double-click FH6 Turn On.bat to use it again.")
        return 0
    if "--restore-controller" in sys.argv:
        if os.path.exists(HIDHIDE_CLI):
            unroute_controller()
            print("Controller hiding is off. Turn your controller off and on if a game still cannot see it.")
        else:
            print("HidHide is not installed, so there is nothing to restore.")
        return 0
    if not hold_single_instance():
        return 0
    install_watcher()
    telemetry = start_telemetry()
    pad = open_pad() if service_running("ViGEmBus") else None
    if pad is not None:
        print("Virtual controller plugged in.")
    routed = route_controller(pad)
    engine = None

    def on_console_event(_event):
        if engine is not None:
            engine.restore_now()
        if routed:
            unroute_controller()
        return False

    console_handler = CTRL_HANDLER(on_console_event)
    kernel32.SetConsoleCtrlHandler(console_handler, True)
    try:
        game = wait_for_game(telemetry, routed)
        if game is None:
            return 0 if not app_enabled() else 1
        GAME_DIR = game_folder(game.handle)
        session = open_session(game, pad is not None)
        if session is None:
            return 1
        check_setup(telemetry)
        engine = Engine(game, session, telemetry, pad)
        return run_engine(engine, game, telemetry, pad, routed)
    finally:
        if routed:
            unroute_controller()


def wait_for_game(telemetry, routed):
    """The running game, started first if it is not running. The Cars volume from last
    time is only found once you are actually driving, so this waits for that."""
    pid = find_game_pid()
    if pid is None:
        if START_GAME:
            print("Starting Forza Horizon 6 through Steam...")
            os.startfile("steam://rungameid/" + STEAM_APP_ID)
        print("Waiting for Forza Horizon 6 to start (close this window to cancel)...")
        while pid is None:
            if not app_enabled():
                print("Turned off.")
                return None
            time.sleep(1)
            pid = find_game_pid()
    elif routed and FORCE_RECONNECT:
        print("Reconnecting your controller so the game lets go of it (approve the Windows prompt)...")
        print("Controller reconnected." if reconnect_controllers(routed) else "The reconnect was cancelled or failed.")
    game = Game(pid)
    if telemetry is None:
        print("Data Out is not available, so the Cars volume from last time cannot be found on its own.")
        print("Press Enter when you are in a car and driving.")
        while True:
            if not app_enabled():
                print("Turned off.")
                return None
            if msvcrt.kbhit() and msvcrt.getwch() in "\r\n":
                return game
            if not game.running():
                print("The game closed before getting into a car.")
                return None
            time.sleep(0.2)
    print("Waiting until you are driving. The Cars volume from last time is picked up then.")
    while not telemetry.fresh():
        if not app_enabled():
            print("Turned off.")
            return None
        if not game.running():
            print("The game closed before you started driving.")
            return None
        time.sleep(0.2)
    return game


def open_session(game, revs):
    """This launch's Cars volume places, and the Shifting setting's when the startup rev
    can use them, each saved for this game run, found again from last launch's
    fingerprints, or calibrated. None if the volume calibration failed."""
    session = open_volume_session(game)
    if session is None:
        return None
    if revs and SWITCH_TO_MANUAL and ("--recalibrate" in sys.argv or not session.get("shifting")):
        places = None
        if "--recalibrate" not in sys.argv:
            places = find_from_fingerprints(game, SHIFTING_FINGERPRINTS_PATH, "the Shifting setting", shifting_valid)
        if places is None:
            places = calibrate_shifting(game)
            if places:
                capture_fingerprints(game, places, SHIFTING_FINGERPRINTS_PATH, shifting_valid)
        session["shifting"] = [a for a, _ in places]
        save_session(game, session)
    shifting = session.get("shifting") or []
    if revs and shifting and ("--recalibrate" in sys.argv or not os.path.exists(LAUNCH_PATH)):
        offsets = calibrate_launch(game, shifting)
        save_launch_offsets(offsets)
    session["launch_offsets"] = load_launch_offsets()
    return session


def open_volume_session(game):
    """This launch's Cars volume places: saved for this game run, found again from
    last launch's fingerprints, or calibrated. None if calibration failed."""
    recalibrate = "--recalibrate" in sys.argv
    session = None if recalibrate else load_session(game)
    if session is None:
        candidates = None if recalibrate else find_from_fingerprints(game)
        if candidates is not None:
            print("If the car's sound does not go off with %s, run Recalibrate.bat." % HOTKEY_NAME)
        else:
            candidates = calibrate(game)
            if candidates is None:
                return None
            capture_fingerprints(game, candidates)
        session = {"candidates": candidates, "muted": False, "restore": []}
        save_session(game, session)
    else:
        print("Using this session's saved calibration (%d place(s))." % len(session["candidates"]))
        if not session["muted"] and not os.path.exists(FINGERPRINTS_PATH):
            capture_fingerprints(game, [tuple(c) for c in session["candidates"]])
    return session


def run_engine(engine, game, telemetry, pad, routed):
    """Answer the hotkey until the game or this window closes."""
    hook_proc = install_key_block(engine)
    if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, HOTKEY_VK):
        print("Could not claim %s; another program is using it. Close that program or pick a" % HOTKEY_NAME)
        print("different \"hotkey\" in settings.json, then open this again.")
        return 1
    button = " or %s on the controller" % TOGGLE_BUTTON_NAME if routed and TOGGLE_BUTTON else ""
    print("\nRunning. %s%s turns the engine off and on while the game window is focused." % (HOTKEY_NAME, button))
    print("While it is off, accelerate is cut (throttle keys%s). While the car is still rolling" % (
        " and the right trigger" if routed else ""))
    print("the brakes stay on at least %d%% and you can brake harder; once it is nearly" % round(OFF_BRAKE * 100))
    print("stopped the brake is cut so it cannot reverse. Steering%s still works." % (
        ", both sticks and buttons" if routed else ""))
    if not routed:
        print("Your controller is NOT routed through the tool, so its triggers are not cut.")
    if pad is not None and PARK_HOLD:
        print("While it is off and the car is stopped, the virtual controller also holds the")
        print("handbrake.")
        if telemetry is None or not telemetry.fresh():
            print("  (This needs Data Out; it starts working once telemetry arrives.)")
    print("The engine is currently %s." % engine.state)
    print("Double-click FH6 Turn Off.bat to stop the tool. FH6 Turn On.bat brings it back.")
    user32.SetTimer(None, 0, 1000, None)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and game_focused():
            engine.toggle()
        elif msg.message == WM_TIMER and not app_enabled():
            print("Turned off. Giving your controller back.")
            engine.restore_now()
            return 0
        elif msg.message == WM_TIMER and not game.running():
            print("Forza Horizon 6 closed; giving your controller back.")
            return 0
    return 0


def run():
    """main(), keeping the window open on errors so a double-clicked exe does not vanish
    before the message can be read."""
    try:
        code = main()
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    if code and FROZEN:
        input("\nPress Enter to close.")
    return code


if __name__ == "__main__":
    sys.exit(run())
