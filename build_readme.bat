@echo off
REM ---------------------------------------------------------------------------
REM Build README.pdf from README.md.
REM
REM Edit README.md and then run this file, either from a terminal or by
REM double-clicking it, to build the PDF that we submit with the project.
REM
REM This passes any extra arguments straight to tools\md2pdf.py, so
REM     build_readme.bat --keep-html
REM also keeps the HTML file so that we can look at it.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM We use the Windows launcher first, and python on the PATH if it is missing.
set "PY=py -3"
where py >nul 2>&1 || set "PY=python"

echo Building README.pdf from README.md ...
%PY% tools\md2pdf.py README.md README.pdf %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo README.pdf was NOT built. See the message above.
)

REM We keep the window open when someone started this by double-clicking it.
echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause

exit /b %RC%
