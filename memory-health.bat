@echo off
setlocal EnableDelayedExpansion

set "DB=%USERPROFILE%\.copilot\memory.db"
set "CTX=%USERPROFILE%\.copilot\memory-context.md"
set "DRIVER=%~dp0memory_driver.py"
set "PASS=0"
set "FAIL=0"

echo.
echo =========================================
echo   Memory System Health Check
echo =========================================
echo.

:: --- 1. Python available ---
echo [1/6] Python available ...
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo       OK - %%v
    set /a PASS+=1
) else (
    echo       FAIL - python not found in PATH
    set /a FAIL+=1
)

:: --- 2. Driver file exists ---
echo [2/6] Driver file exists ...
if exist "%DRIVER%" (
    echo       OK - %DRIVER%
    set /a PASS+=1
) else (
    echo       FAIL - Missing: %DRIVER%
    set /a FAIL+=1
)

:: --- 3. Database file exists ---
echo [3/6] Database file exists ...
if exist "%DB%" (
    for %%F in ("%DB%") do set "DBSIZE=%%~zF"
    set /a DBKB=!DBSIZE!/1024
    echo       OK - %DB% (!DBKB! KB^)
    set /a PASS+=1
) else (
    echo       FAIL - Missing: %DB%
    echo             Run: python "%~dp0seed_memory.py" to initialise
    set /a FAIL+=1
)

:: --- 4. Context markdown file exists ---
echo [4/6] Context file exists ...
if exist "%CTX%" (
    for %%F in ("%CTX%") do set "CTXSIZE=%%~zF"
    set /a CTXKB=!CTXSIZE!/1024
    echo       OK - %CTX% (!CTXKB! KB^)
    set /a PASS+=1
) else (
    echo       WARN - Missing: %CTX%
    echo             Run: Export-MemoryContext in PowerShell to regenerate
    set /a FAIL+=1
)

:: --- 5. Driver responds to get_stats ---
echo [5/6] Driver get_stats ...
if exist "%DRIVER%" (
    for /f "tokens=*" %%r in ('echo {"op":"get_stats"} ^| python "%DRIVER%" 2^>^&1') do set "STATS=%%r"
    echo !STATS! | findstr /c:"topics" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       OK - !STATS!
        set /a PASS+=1
    ) else (
        echo       FAIL - Unexpected response: !STATS!
        set /a FAIL+=1
    )
) else (
    echo       SKIP - Driver missing
    set /a FAIL+=1
)

:: --- 6. FTS search returns results (use temp file to avoid cmd quoting issues) ---
echo [6/6] FTS search (smoke test) ...
if exist "%DRIVER%" (
    set "TMPJSON=%TEMP%\mem_search_test.json"
    echo {"op":"search_memory","query":"role","limit":1}> "!TMPJSON!"
    for /f "tokens=*" %%r in ('python "%DRIVER%" ^< "!TMPJSON!" 2^>^&1') do set "SEARCH=%%r"
    del "!TMPJSON!" >nul 2>&1
    echo !SEARCH! | findstr /c:"rank" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       OK - Search returned ranked results
        set /a PASS+=1
    ) else (
        echo       WARN - No results for test query (DB may be empty^)
        echo             Response: !SEARCH!
        set /a FAIL+=1
    )
) else (
    echo       SKIP - Driver missing
    set /a FAIL+=1
)

:: --- Summary ---
echo.
echo =========================================
if !FAIL! EQU 0 (
    echo   RESULT: ALL CHECKS PASSED [!PASS!/6]
) else (
    echo   RESULT: !PASS! passed, !FAIL! failed
)
echo =========================================
echo.
echo   DB Path  : %DB%
echo   Context  : %CTX%
echo   Driver   : %DRIVER%
echo.

if !FAIL! GTR 0 (
    echo   To fix issues:
    echo     Init DB  : python "%~dp0memory_driver.py"   ^(auto-inits on first run^)
    echo     Seed data: python "%~dp0seed_memory.py"
    echo     Regen ctx: Import-Module "%~dp0memory.ps1"; Export-MemoryContext
    echo.
)

endlocal
