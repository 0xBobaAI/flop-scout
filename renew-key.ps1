# renew-key.ps1 — refresh the FLOP devnet gateway key (Windows)
#
# The devnet key expires every 24h. Either run this by hand or schedule it:
#   powershell.exe -ExecutionPolicy Bypass -File <path>\renew-key.ps1
#
# The mnemonic is read from the WALLET_MNEMONIC env var. Set it once for the
# current user — never put it in this file:
#   [Environment]::SetEnvironmentVariable('WALLET_MNEMONIC', 'your twelve words', 'User')
#
# On Linux/macOS use renew_key.py directly and point a systemd timer at it;
# deploy/flop-scout-renew.timer does exactly that.

$ErrorActionPreference = 'Stop'

# Work from the script's own directory so this works wherever the repo lives.
Set-Location -LiteralPath $PSScriptRoot

$mnemonic = [Environment]::GetEnvironmentVariable('WALLET_MNEMONIC', 'User')
if (-not $mnemonic) {
    Write-Host 'WALLET_MNEMONIC env var is not set. See the header of this file.'
    exit 1
}

$env:WALLET_MNEMONIC = $mnemonic
python renew_key.py
