@echo off
setlocal enabledelayedexpansion
title Stock Screening - Copy Results

set SERVER=192.168.140.43
set SERVER_USER=claude
set SERVER_PATH=/data/frame

REM --- Detect this PC's Desktop path automatically (any user / OneDrive or not) ---
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul') do set "DESKTOP=%%B"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"

set "LOCAL_PATH=%DESKTOP%\stock"

echo ============================================
echo  Stock Screening Results Copy
echo  Server : %SERVER_USER%@%SERVER%
echo  Target : %LOCAL_PATH%
echo ============================================
echo.

if not exist "%LOCAL_PATH%" mkdir "%LOCAL_PATH%"

scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/momentum_screen_latest.xlsx   "%LOCAL_PATH%\momentum_screen_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/kr_paradigm_latest.xlsx        "%LOCAL_PATH%\kr_paradigm_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/us_paradigm_latest.xlsx        "%LOCAL_PATH%\us_paradigm_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/sector_screen_latest.xlsx      "%LOCAL_PATH%\sector_screen_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/theme_screen_latest.xlsx       "%LOCAL_PATH%\theme_screen_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/theme_daily_latest.xlsx        "%LOCAL_PATH%\theme_daily_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/factor_result_us_latest.xlsx   "%LOCAL_PATH%\factor_result_us_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/sector_dashboard_latest.xlsx   "%LOCAL_PATH%\sector_dashboard_latest.xlsx"
scp %SERVER_USER%@%SERVER%:%SERVER_PATH%/monthly_top50_report.xlsx      "%LOCAL_PATH%\monthly_top50_report.xlsx"

echo.
echo Done. Opening folder...
start "" "%LOCAL_PATH%"
pause
