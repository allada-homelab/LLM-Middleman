@echo off
rem Windows host counterpart of .devcontainer/initialize (resolved via PATHEXT).
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if not exist "%USERPROFILE%\.gitconfig" type nul > "%USERPROFILE%\.gitconfig"
