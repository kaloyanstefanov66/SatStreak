@echo off
REM SatStreak on Windows, via WSL.
REM
REM Plate solving needs Linux, so the work happens inside WSL while this wrapper
REM keeps the command a single word on the Windows side.
REM
REM The executable is invoked directly rather than through a login shell. That
REM avoids two separate problems: a non-interactive `wsl` does not read .bashrc,
REM so anything installed under ~/.local/bin is not on its PATH; and passing a
REM Windows path through `bash -lc` would let bash eat the backslashes.
REM
REM Windows paths are passed through unchanged -- satstreak translates them.
REM
REM Set SATSTREAK_WSL_BIN to the full Linux path of the satstreak executable,
REM e.g.  setx SATSTREAK_WSL_BIN /home/you/satstreak-solver/bin/satstreak

if "%SATSTREAK_WSL_BIN%"=="" goto :nobin
wsl.exe -e %SATSTREAK_WSL_BIN% %*
exit /b %ERRORLEVEL%

:nobin
echo SATSTREAK_WSL_BIN is not set.
echo.
echo It must hold the full Linux path to the satstreak executable inside WSL.
echo Find it with:    wsl -e bash -lc "command -v satstreak"
echo Then set it:     setx SATSTREAK_WSL_BIN /home/you/.local/bin/satstreak
exit /b 2
