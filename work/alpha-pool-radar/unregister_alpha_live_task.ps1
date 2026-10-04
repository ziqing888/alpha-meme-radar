param(
  [string]$TaskName = "AlphaRadarLiveRefresh"
)

$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Output "unregistered:$TaskName"
} else {
  Write-Output "not_found:$TaskName"
}
