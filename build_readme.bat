@echo off
REM ---------------------------------------------------------------------------
REM Build README.pdf from README.md.
REM
REM Edit README.md, then run this file -- from a terminal or by double-clicking
REM it -- to regenerate the PDF that is submitted with the project.
REM
REM Any extra arguments are passed straight to tools\md2pdf.py, so
REM     build_readme.bat --keep-html
REM also leaves the intermediate HTML behind for inspection.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM Prefer the Windows launcher, and fall back to python on PATH.
set "PY=py -3"
where py >nul 2>&1 || set "PY=python"

echo Building README.pdf from README.md ...
%PY% tools\md2pdf.py README.md README.pdf %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo README.pdf was NOT built. See the message above.
)

REM Keep the window open when this was started by double-clicking it.
echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause

exit /b %RC%
