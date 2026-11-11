# FH6 Engine Toggle

Turn your car's engine off and on in Forza Horizon 6 with one key (F10, or D-pad down on the controller).

**Engine off**
- The car's sound fades out with a soft click and shudder.
- Accelerate is cut on the keyboard and on the controller's right trigger.
- While the car is still rolling, the brakes stay on at least 5% and you can brake harder.
- Once the car is nearly stopped, the handbrake is held, so it can't drive or reverse.
- Steering, both thumbsticks and every other button keep working.

**Engine on**
- A key click plays, then that car's own startup sound.
- The engine revs to 75% of its max revs, then drops straight back down. The brake and handbrake are held during the rev and released once the revs fall.
- If you drive Automatic, the tool switches to Manual for the rev so the brake does not reverse the car, then switches back.

> **Use it offline only.** This tool reads and writes the game's memory to change the Cars volume. Online, that could break the game's rules and risk a ban. You use it at your own risk.

## Requirements

- Windows 10 or 11, 64-bit
- Forza Horizon 6 (Steam)
- [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases): virtual controller driver. Needed for the startup rev, the minimum brake and the parking hold.
- [HidHide](https://github.com/nefarius/HidHide/releases): needed to cut your controller's triggers while the engine is off. Restart Windows after installing it.

Without the drivers, the tool still mutes and unmutes the car's sound, and it shows what's missing when it starts.

## Setup

1. Install ViGEmBus and HidHide, then restart Windows.
2. Unzip this folder anywhere, for example `Documents\FH6EngineToggle`. It doesn't need Program Files.
3. In Steam, right-click **Forza Horizon 6**, choose **Properties > Controller**, and pick **Disable Steam Input**. Otherwise Steam passes your controller to the game around the tool.
4. In the game, go to **Settings > HUD and Gameplay** and set:
   - **Data Out**: On
   - **Data Out IP Address**: `127.0.0.1`
   - **Data Out IP Port**: `5300`

## Each time you play

1. Run `FH6EngineToggle.exe`, or just start the game. A watcher opens the tool whenever Forza Horizon 6 starts, including after you sign in to Windows.
2. If the tool started the game, get into a car and drive. The tool carries on once Data Out shows you driving. Without Data Out, press Enter in the tool's window.
3. **The first time only, calibrate.** After that, the tool remembers what the memory around the Cars volume looks like and finds it again on its own each launch, which takes a short automatic search. It asks you to calibrate again only if it can't find it, for example after you change the Cars slider or a game update. The tool walks you through calibration:
   1. Open **Settings > Audio** and note the **Cars** slider's level.
   2. Type that number in the tool's window.
   3. Move the slider to a different level and type the new number. Repeat until it says it found the volume.
   4. Set the slider back to your normal level, then press Enter. The car's sound goes off; check that you can hear the change.

   The result is saved in `session.json` and `volume_fingerprints.json`.

   Next, it offers a one-time **Shifting setup**. You switch **Settings > Difficulty > Shifting** between Automatic, Manual and Manual with Clutch a few times, as it asks. After that, if you drive Automatic, the startup rev switches to Manual for a moment so it can hold the car with the brake, then switches back. Without this, the rev holds the car with the handbrake, which can set off launch control. This setup is saved in `shifting_fingerprints.json`.
4. Press **F10**, or **D-pad down** on the controller, to turn the engine off and on. While you're driving, D-pad down goes only to the tool; in menus it works as normal.

To stop, close the tool's window or quit the game. Either way, the tool hands your controller back.

## Controls while the engine is off

| Input | What happens |
| --- | --- |
| Right trigger, Up, W | Blocked |
| Left trigger, Down, S | At least 5% brake while rolling; blocked once nearly stopped |
| Steering (sticks, Left/Right, A/D) | Works |
| All other buttons | Work |

## Settings

`settings.json` is created next to the exe the first time it runs. Edit it with Notepad, then restart the tool. Each setting is described inside the file under `_help`. If a value is invalid, the tool reports it and uses the default.

| Setting | Default | What it does |
| --- | --- | --- |
| `hotkey` | `"F10"` | Key that turns the engine off and on (F1–F12) |
| `controller_toggle_button` | `"DpadDown"` | Controller button that turns the engine off and on while driving (`"None"` to turn off) |
| `start_game` | `true` | Start the game through Steam if it isn't running |
| `rev_on_startup` | `true` | Rev the engine on startup |
| `rev_multiple_of_idle` | `8.0` | Unused. The rev now goes to 75% of the car's max revs. |
| `rev_trigger_strength` | `1.0` | How hard the rev presses the throttle, before the 20% slower climb (0–1) |
| `switch_to_manual_for_rev` | `true` | In Automatic, switch to Manual during the rev and hold the car with the brake |
| `minimum_brake_when_off` | `0.05` | Brake applied while rolling with the engine off (0–1) |
| `park_hold` | `true` | Hold the handbrake while stopped with the engine off |
| `park_hold_below_kmh` | `7.0` | Speed below which the car counts as stopped |
| `cut_controller_when_off` | `true` | Route the controller through the tool so its triggers can be cut |
| `auto_reconnect_controller` | `false` | If the game was already running, try reconnecting the controller (admin prompt; may not work) |
| `throttle_keys` | `["Up", "W"]` | Keys blocked while the engine is off |
| `brake_keys` | `["Down", "S"]` | Keys allowed only while the car is still rolling |
| `play_starter_sound` | `false` | Play a starter sound before the rev |
| `startup_volume` | `1.6` | Engine loudness while starting, as a multiple of your Cars volume (1–3) |
| `shutoff_volume` | `0.15` | Loudness of the shutoff sound (0–1) |
| `telemetry_port` | `5300` | Must match the game's Data Out port |

**Custom sounds:** put a `shutoff.wav` next to the exe to replace the shutoff sound. The key-turn click is still played in front of it. The startup crank is taken from the game's own recording for that kind of car. A `sounds\car_<number>_startup.wav` replaces it for one car. The tool prints the car's number when you turn its engine off.

## Turning the tool off

Double-click **FH6 Turn Off.bat** to stop it and leave your controller alone. It will not open again when the game starts.

Double-click **FH6 Turn On.bat** to bring it back. It opens again while Forza Horizon 6 is running.

## Sending a bug report

Double-click **FH6 Debug Report.bat** while the problem is happening, ideally with the game running and Data Out on. It writes `FH6-debug-report.txt` in the tool folder. Send that file. It lists the setup checks only. It does not copy game memory.

## Troubleshooting

**Controller missing, or showing up twice after the tool closed or crashed.** Run `Restore Controller.bat`, then switch the controller off and on.

**The car's sound doesn't go off, or "The Cars volume is no longer where it was."** The automatic search picked the wrong place, or the game moved the volume. Run `Recalibrate.bat`.

**The car speeds up with the engine off.** The game can sometimes still read your controller's trigger directly. The tool counters this with an equal amount of brake while you're rolling, so the car shouldn't gain speed. If it does, check that Steam Input is off for this game:
1. In Steam, right-click **Forza Horizon 6** and choose **Properties**.
2. Under **Controller**, choose **Disable Steam Input**.

Also check that the startup lines show `[ok] HidHide`.

**No startup rev, or the brake/parking hold doesn't work.** Check that Data Out is set as in Setup, then drive for a moment. The startup lines should show `[ok] Data Out telemetry`.

**"Could not claim F10".** Another program is using that key. Pick a different `hotkey` in `settings.json`.

**Windows SmartScreen warns about the exe.** The exe isn't code-signed. Choose **More info > Run anyway**.

**Keyboard keys aren't blocked.** The tool only blocks keys while the game window is focused.

## Uninstall

1. Run `Restore Controller.bat`.
2. Delete the folder.
3. Optionally, uninstall HidHide and ViGEmBus from **Settings > Apps**.
4. Delete `FH6 Engine Toggle.vbs` from the Windows Startup folder if you do not want the tool to open with the game.

See `THIRD-PARTY-NOTICES.txt` for credits and licences. No game files are included.
