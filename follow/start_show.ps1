# Show day from the laptop, one command (Windows PowerShell):
#
#   powershell -ExecutionPolicy Bypass -File D:\Microduck-Project\follow\start_show.ps1
#
# Opens the laptop "ears" (speech recognition, follow/voice/asr_bridge.py under WSL) in a second
# window, then runs the show on the duck in this one. The ears wait for the show and reconnect by
# themselves, so the order does not matter. If the duck is in boot standby, this opens its
# console (follow/console.sh) instead. Default: scripted (no camera, no sudo) — f / "Let's go"
# walks straight on stage, h / "Let's go home" turns round and walks straight back.
param(
  [string]$Duck = "radxa@192.168.50.237",
  [string]$HostKeyAlias = "192.168.50.135",
  [double]$WalkM = 2.0,
  [switch]$Camera,     # follow the ArUco marker instead of walking a fixed line
  [switch]$NoEars      # keyboard only
)
$model = "~/mdev/asr/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06"
$bridge = "/mnt/d/Microduck-Project/follow/voice/asr_bridge.py"
if (-not $NoEars) {
  $cmd = "wsl.exe -e bash -lc `"~/mdev/speech-venv/bin/python $bridge --duck $Duck --ssh-opt HostKeyAlias=$HostKeyAlias --model $model`""
  Start-Process powershell -ArgumentList @("-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='DUCK EARS'; $cmd")
}
$mode = if ($Camera) { "" } else { "--scripted --walk-m $WalkM" }
$voice = "--no-voice --command-pipe /tmp/microduck-voice"
# Boot standby (follow/standby.sh, turned on by standby_install.sh) already running on the duck?
# Then open its console instead of starting a second show; type yes there to unlock.
$f = "~/microduck-follow/current/follow"
$remote = "if pgrep -f 'follow.app .*--standby' >/dev/null; then exec bash $f/console.sh; else exec bash $f/show.sh $voice $mode; fi"
ssh -t -o HostKeyAlias=$HostKeyAlias $Duck $remote
