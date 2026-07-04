Param(
    [string]$KeyPath,
    [int]$Lifetime = 3600,
    [switch]$Quiet
)

$prefix = "[setup-ssh-agent]"

function Write-Status($msg) {
    if (-not $Quiet) { Write-Host "$prefix $msg" }
}

function Write-Err($msg) {
    Write-Host "$prefix ERROR: $msg" -ForegroundColor Red
}

# --- Check the ssh-agent Windows service exists ---
Write-Status "Checking ssh-agent service..."

$service = Get-Service ssh-agent -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Err "ssh-agent service not found. Install the OpenSSH Client optional feature."
    Write-Host ""
    Write-Host "  Settings > Apps > Optional Features > Add: OpenSSH Client"
    Write-Host ""
    exit 1
}

# --- Check the service is running ---
if ($service.Status -ne 'Running') {
    Write-Err "ssh-agent service is not running."
    Write-Host ""
    Write-Host "Run the following ONCE in an elevated (Admin) PowerShell:"
    Write-Host "  Set-Service -Name ssh-agent -StartupType Automatic" -ForegroundColor Cyan
    Write-Host "  Start-Service ssh-agent" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Then re-run this script in a normal PowerShell / VS Code terminal."
    exit 1
}

Write-Status "ssh-agent service is running. Checking keys..."

# --- Check for loaded keys ---
# ssh-add -l exit codes:
#   0 = agent has keys
#   1 = agent running, no keys
#   2 = cannot contact agent
$null = ssh-add -l 2>&1
$status = $LASTEXITCODE

if ($status -eq 0) {
    Write-Status "Agent already has keys loaded."
    if (-not $Quiet) {
        # Re-run to display the key list (first call was discarded)
        ssh-add -l
    }
    exit 0
}

if ($status -eq 2) {
    Write-Err "Cannot communicate with ssh-agent despite service running."
    Write-Host "Try restarting the service: Restart-Service ssh-agent"
    exit 1
}

# status 1: agent running, no keys
Write-Status "No keys loaded. Adding key(s) (lifetime=${Lifetime}s)..."

$addArgs = @("-t", "$Lifetime")
if ($KeyPath) {
    if (-not (Test-Path $KeyPath)) {
        Write-Err "Key file not found: $KeyPath"
        exit 1
    }
    $addArgs += $KeyPath
}

ssh-add @addArgs

if ($LASTEXITCODE -ne 0) {
    Write-Err "'ssh-add' failed (exit code $LASTEXITCODE). Check your key path or passphrase."
    exit $LASTEXITCODE
}

Write-Status "Done. Key(s) added successfully."
