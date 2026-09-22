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
rem known_hosts is bind-mounted as a second trust file, so it must exist as a
rem FILE. No agent check here: the Windows agent is a named pipe VS Code bridges,
rem and SSH_AUTH_SOCK is normally unset, which devcontainer.json maps to /dev/null.
if exist "%USERPROFILE%\.ssh\known_hosts\" (
    echo initialize: "%USERPROFILE%\.ssh\known_hosts" is a directory, not a file. Remove it and start the container again. 1>&2
    exit /b 1
)
if not exist "%USERPROFILE%\.ssh\known_hosts" type nul > "%USERPROFILE%\.ssh\known_hosts"
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

rem Git identity. devcontainer.json binds .devcontainer\.gitconfig.host rather
rem than %USERPROFILE%\.gitconfig, because that file is often only an [include] /
rem [includeIf] shim naming paths the container cannot see, which leaves git in
rem there with no user.email. Flatten the host's effective global config into it;
rem .devcontainer/initialize carries the full reasoning and must stay in step.
rem Values containing `!` are mangled by delayed expansion — untested, like the
rem rest of this file; post-create.sh warns when no identity survived the copy.
call :replace "%DC%\.gitconfig.host" || exit /b 1
type nul > "%DC%\.gitconfig.host"
rem Listed to a file first: `for /f` over a command cannot see git's exit code, and
rem a failed read must stop here rather than start a container with no identity.
set "CFGLIST=%TEMP%\gitconfig-host-%RANDOM%.list"
git -C "%WS%" config --global --includes --list > "%CFGLIST%"
if errorlevel 1 (
    del /q "%CFGLIST%" 2>nul
    echo initialize: 'git config --global --includes --list' failed; cannot write "%DC%\.gitconfig.host". 1>&2
    exit /b 1
)
for /f "usebackq delims=" %%L in ("%CFGLIST%") do call :copycfg "%%L"
del /q "%CFGLIST%"
exit /b 0

rem Copy one `section.key=value` entry into .gitconfig.host, splitting on the
rem FIRST `=` so values may contain more of them.
:copycfg
set "CFGLINE=%~1"
set "CFGKEY="
set "CFGVAL="
for /f "tokens=1* delims==" %%A in ("%~1") do (
    set "CFGKEY=%%A"
    set "CFGVAL=%%B"
)
rem A valueless boolean ([core] bare) lists as a bare key with no `=`; git reads
rem it as true. An explicit empty value (`helper =`) still has its `=`.
if "!CFGKEY!"=="!CFGLINE!" set "CFGVAL=true"
rem Already resolved above, and their targets are not mounted.
if /i "!CFGKEY:~0,8!"=="include." exit /b 0
if /i "!CFGKEY:~0,10!"=="includeif." exit /b 0
rem Credential HELPERS are host-specific: the editor forwards its own via
rem /etc/gitconfig, and a copied `gh auth setup-git` block clears that list
rem (blank `helper =`) and names a host-only binary. Other credential.*
rem keys (useHttpPath) still come across. See initialize.
if /i "!CFGKEY:~0,11!"=="credential." if /i "!CFGKEY:~-7!"==".helper" exit /b 0
rem Keys naming host binaries (delta and the like) break `git log` / `add -p`.
if /i "!CFGKEY!"=="core.pager" exit /b 0
if /i "!CFGKEY:~0,6!"=="pager." exit /b 0
if /i "!CFGKEY!"=="interactive.difffilter" exit /b 0
git config -f "%DC%\.gitconfig.host" --add "!CFGKEY!" "!CFGVAL!" >nul
if errorlevel 1 echo initialize: could not copy git config "!CFGKEY!" to "%DC%\.gitconfig.host". 1>&2
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
