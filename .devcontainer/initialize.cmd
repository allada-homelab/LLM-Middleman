@echo off
rem Windows host counterpart of .devcontainer/initialize (resolved via PATHEXT).
rem Keep the two scripts in step: devcontainer.json mounts .gitcommon and
rem .gitentry unconditionally, so both hosts must produce them. See the long
rem comment in .devcontainer/initialize for what they are and why.
rem
rem UNTESTED ON WINDOWS — no Windows host has run this. The post-create git
rem self-check is what actually guarantees a broken setup fails loudly rather
rem than handing anyone a silently empty repository.
setlocal enabledelayedexpansion

if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if not exist "%USERPROFILE%\.gitconfig" type nul > "%USERPROFILE%\.gitconfig"

set "DC=%~dp0"
set "DC=%DC:~0,-1%"
for %%I in ("%DC%") do set "WS=%%~dpI"
set "WS=%WS:~0,-1%"

where git >nul 2>&1
if errorlevel 1 (
    echo initialize: git not found on the host; cannot prepare the container's git mounts. 1>&2
    exit /b 1
)

set "COMMON="
set "GITDIR="
for /f "delims=" %%I in ('git -C "%WS%" rev-parse --path-format^=absolute --git-common-dir 2^>nul') do set "COMMON=%%I"
for /f "delims=" %%I in ('git -C "%WS%" rev-parse --path-format^=absolute --git-dir 2^>nul') do set "GITDIR=%%I"
if not defined COMMON (
    echo initialize: "%WS%" is not a git working tree. 1>&2
    exit /b 1
)
rem git reports forward slashes; mklink needs backslashes.
set "COMMON=%COMMON:/=\%"
set "GITDIR=%GITDIR:/=\%"

rem Replace rather than update: a previous run, or Docker auto-creating a missing
rem bind source, may have left either path as the wrong type.
rem
rem Dispatch on the type, and never `del` a directory. `del <dir>` means
rem `del <dir>\*.*`, so against a junction it would delete the CONTENTS of the real
rem git dir it points at — HEAD, config, index, packed-refs, every worktree's
rem metadata. `rmdir /s /q` removes a junction as a link without following it, and
rem `if exist "path\"` (trailing backslash) is the directory test that keeps the two
rem apart.
call :replace "%DC%\.gitcommon" || exit /b 1
call :replace "%DC%\.gitentry" || exit /b 1

rem Junction, not a symlink: /J needs no administrator rights or developer mode.
mklink /J "%DC%\.gitcommon" "%COMMON%" >nul
if errorlevel 1 (
    echo initialize: could not link "%DC%\.gitcommon" to "%COMMON%". 1>&2
    echo   Remove that path by hand if it exists and start the container again. 1>&2
    exit /b 1
)

rem Branch on what .git actually IS, matching .devcontainer/initialize: a
rem --separate-git-dir checkout has .git as a file while GITDIR equals COMMON.
if exist "%WS%\.git\" (
    mklink /J "%DC%\.gitentry" "%GITDIR%" >nul
    if errorlevel 1 (
        echo initialize: could not link "%DC%\.gitentry" to "%GITDIR%". 1>&2
        exit /b 1
    )
) else if /i "%GITDIR%"=="%COMMON%" (
    > "%DC%\.gitentry" echo gitdir: /gitcommon
) else (
    for %%I in ("%GITDIR%") do set "WTNAME=%%~nxI"
    > "%DC%\.gitentry" echo gitdir: /gitcommon/worktrees/!WTNAME!
)
exit /b 0

rem Remove one path whatever type it currently has, without ever following a
rem junction. Returns 1 if the path survives.
:replace
if not exist "%~1" exit /b 0
if exist "%~1\" (
    rmdir /s /q "%~1" 2>nul
) else (
    del /q /f "%~1" 2>nul
)
if exist "%~1" (
    echo initialize: could not remove "%~1". Delete it by hand and start the 1>&2
    echo   container again. 1>&2
    exit /b 1
)
exit /b 0
