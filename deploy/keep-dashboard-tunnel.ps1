param(
    [string]$RemoteHost = "<vps-ip>",
    [string]$RemoteUser = "ubuntu",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\<ssh-key>.pem",
    [int]$LocalPort = 8920,
    [int]$RemotePort = 8920,
    [string]$LogPath = "$env:TEMP\polyvote-dashboard-tunnel.log"
)

$ErrorActionPreference = "Continue"
$sshExe = (Get-Command ssh -ErrorAction Stop).Source
$sshAttemptLog = "$LogPath.ssh"

function Write-TunnelLog {
    param([string]$Message)

    $line = "{0} {1}{2}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($LogPath, $line)
}

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    Write-TunnelLog "SSH key not found: $KeyPath"
    exit 2
}

Write-TunnelLog "dashboard tunnel watchdog started"

while ($true) {
    $existingListener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    if ($existingListener) {
        Start-Sleep -Seconds 10
        continue
    }

    Write-TunnelLog "opening 127.0.0.1:$LocalPort -> $RemoteHost`:$RemotePort"

    try {
        Remove-Item -LiteralPath $sshAttemptLog -Force -ErrorAction SilentlyContinue
        $arguments = @(
            "-N",
            "-L", "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}",
            "-i", $KeyPath,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "${RemoteUser}@${RemoteHost}"
        )
        $process = Start-Process `
            -FilePath $sshExe `
            -ArgumentList $arguments `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -RedirectStandardError $sshAttemptLog
        if (Test-Path -LiteralPath $sshAttemptLog) {
            Get-Content -LiteralPath $sshAttemptLog -ErrorAction SilentlyContinue |
                ForEach-Object { Write-TunnelLog "ssh: $_" }
        }
        $exitCode = $process.ExitCode
    }
    catch {
        Write-TunnelLog "failed to start ssh: $($_.Exception.Message)"
        $exitCode = -1
    }

    Write-TunnelLog "ssh exited with code $exitCode; retrying in 3 seconds"
    Start-Sleep -Seconds 3
}
