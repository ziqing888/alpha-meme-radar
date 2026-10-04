$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs
python .\work\alpha-pool-radar\alpha_radar_dashboard.py --out-dir .\outputs

$Dashboard = Resolve-Path ".\outputs\alpha-radar-dashboard.html"
Write-Host "Dashboard refreshed: $Dashboard"
