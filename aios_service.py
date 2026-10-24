"""Run `aios daemon` as a service the operating system restarts when it dies.

    python aios.py service systemd --home ~/aios-data -- --policy policy.toml --provider openai --model llama3.1
    python aios.py service launchd --home ~/aios-data -- ...
    python aios.py service windows --home C:\\aios-data -- ...

aios prints the service definition and the commands to install it; it installs nothing itself, so you
see exactly what will run at login. Restarting is the operating system's job (systemd Restart=,
launchd KeepAlive, Task Scheduler restart-on-failure): a supervisor inside the process it supervises
dies with it. `aios status` tells you whether the daemon is alive.

API keys never go into the service file (those are often readable by other users and get copied
into bug reports). systemd and launchd read them from `<home>/aios.env` (KEY=value lines; make it
readable by you only); on Windows, set them as user environment variables.
"""
import posixpath
import shlex

KINDS = ("systemd", "launchd", "windows")
NAME = "aios-daemon"
ENV_FILE = "aios.env"
REFUSED = ("--once", "--home")  # a service runs for good, and --home is given once, outside


def check_args(args: list[str]) -> list[str]:
    args = args[1:] if args[:1] == ["--"] else list(args)
    for a in args:
        if a.split("=", 1)[0] in REFUSED:
            raise ValueError(f"{a.split('=', 1)[0]} cannot be passed through: "
                             + ("a service must keep running" if a.startswith("--once") else "use service --home"))
    return args


def command(python: str, script: str, home: str, args: list[str]) -> list[str]:
    return [python, script, "daemon", "--home", home, *check_args(args)]


def systemd(python: str, script: str, home: str, args: list[str]) -> str:
    # systemd expands %specifiers and $VARS in ExecStart: double them so paths and flags stay literal
    cmd = " ".join(shlex.quote(c) for c in command(python, script, home, args)).replace("%", "%%").replace("$", "$$")
    home = home.replace("%", "%%")
    return f"""# Save as ~/.config/systemd/user/{NAME}.service, then:
#   systemctl --user daemon-reload
#   systemctl --user enable --now {NAME}
#   loginctl enable-linger "$USER"     # keep it running after you log out
#   journalctl --user -u {NAME} -f     # its log (alerts appear here too)
# API keys: put KEY=value lines in {posixpath.join(home, ENV_FILE)} and run: chmod 600 {posixpath.join(home, ENV_FILE)}
[Unit]
Description=aios daemon ({home})
After=network-online.target

[Service]
Type=simple
WorkingDirectory={home}
EnvironmentFile=-{posixpath.join(home, ENV_FILE)}
ExecStart={cmd}
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def launchd(python: str, script: str, home: str, args: list[str]) -> str:
    cmd = " ".join(shlex.quote(c) for c in command(python, script, home, args))
    shell = f"set -a; [ -f {ENV_FILE} ] && . ./{ENV_FILE}; set +a; exec {cmd}"
    log = posixpath.join(home, "daemon.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Save as ~/Library/LaunchAgents/com.aios.daemon.plist, then:
       launchctl load -w ~/Library/LaunchAgents/com.aios.daemon.plist
     Log: {_xml(str(log))}   API keys: KEY=value lines in {_xml(str(posixpath.join(home, ENV_FILE)))} (chmod 600) -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.aios.daemon</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>-c</string><string>{_xml(shell)}</string></array>
  <key>WorkingDirectory</key><string>{_xml(home)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>{_xml(str(log))}</string>
</dict>
</plist>
"""


def _ps(s: str) -> str:  # PowerShell single-quoted string
    return "'" + s.replace("'", "''") + "'"


def windows(python: str, script: str, home: str, args: list[str]) -> str:
    rest = command(python, script, home, args)[1:]
    if any('"' in a for a in rest):
        raise ValueError('an argument contains a double quote, which Task Scheduler cannot pass safely')
    argline = " ".join(f'"{a}"' if (" " in a or not a) else a for a in rest)
    return f"""# Run in PowerShell (as yourself, not as Administrator). Restarts the daemon if it exits with an error.
# API keys: set them once as user environment variables, e.g.  setx ANTHROPIC_API_KEY "sk-ant-..."
$action = New-ScheduledTaskAction -Execute {_ps(python)} -Argument {_ps(argline)} -WorkingDirectory {_ps(home)}
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -StartWhenAvailable
Register-ScheduledTask -TaskName {_ps(NAME)} -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName {_ps(NAME)}
# Remove later with:  Unregister-ScheduledTask -TaskName {_ps(NAME)}
"""


def render(kind: str, home: str, args: list[str], python: str, script: str) -> str:
    return {"systemd": systemd, "launchd": launchd, "windows": windows}[kind](python, script, home, args)
