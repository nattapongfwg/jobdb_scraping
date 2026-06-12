# Windows-native setup for the SEEK scraper.
# Run in PowerShell from the project folder:
#   cd E:\jobdb_scraping
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
$ErrorActionPreference = "Stop"

Write-Host "==> [1/4] Locating Python" -ForegroundColor Cyan
$py = "py"
try { & $py --version } catch { $py = "python" ; & $py --version }

Write-Host "==> [2/4] Creating virtual environment (.venv)" -ForegroundColor Cyan
& $py -m venv .venv

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "==> [3/4] Installing Python dependencies" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt

Write-Host "==> [4/4] Installing Playwright Chromium" -ForegroundColor Cyan
& $venvPy -m playwright install chromium

Write-Host ""
Write-Host "==> DONE. Next steps:" -ForegroundColor Green
Write-Host "    1) Copy .env.example to .env and review settings (Windows Auth is default)."
Write-Host "    2) Test DB:   .\.venv\Scripts\python.exe -c `"import db; db.ping()`""
Write-Host "    3) Dry run:   .\.venv\Scripts\python.exe main.py --headed --job-id 91934584 --limit 3"
Write-Host "       (solve the CAPTCHA in the browser window that opens)"
Write-Host "    4) Full run:  .\.venv\Scripts\python.exe main.py --headed"
