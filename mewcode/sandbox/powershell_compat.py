"""Runspace-only Windows PowerShell compatibility, never host execution.

Windows PowerShell 5.1's current-filesystem-location deletion check can walk
ungranted volume ancestors even for an explicit, authorized child path. The
adapter resolves targets in the caller's location, preserves its ancestor
guard, and invokes the original cmdlet from a non-filesystem provider. The
process token, Job, ACLs and literal target are unchanged. Module-qualified
cmdlet calls are deliberately not intercepted or rewritten.
"""


def remove_item_wrapper() -> str:
    # Keep core types/cmdlets only: this also runs in ConstrainedLanguage.
    # Static parameters match Windows PowerShell's cmdlet, including the
    # FileSystem Stream parameter. Other providers use the original command.
    return r"""
function global:Remove-Item {
    [CmdletBinding(DefaultParameterSetName='Path', SupportsShouldProcess=$true,
                   ConfirmImpact='Medium', SupportsTransactions=$true)]
    param(
        [Parameter(ParameterSetName='Path', Mandatory=$true, Position=0,
                   ValueFromPipeline=$true, ValueFromPipelineByPropertyName=$true)]
        [string[]]$Path,
        [Parameter(ParameterSetName='LiteralPath', Mandatory=$true,
                   ValueFromPipelineByPropertyName=$true)]
        [Alias('PSPath')][string[]]$LiteralPath,
        [string]$Filter, [string[]]$Include, [string[]]$Exclude,
        [switch]$Recurse, [switch]$Force,
        [Parameter(ValueFromPipelineByPropertyName=$true)][pscredential]$Credential,
        [string[]]$Stream
    )
    process {
        $mewOriginalLocation = Microsoft.PowerShell.Management\Get-Location
        $mewForward = @{}
        foreach ($mewKey in $PSBoundParameters.Keys) {
            if ($mewKey -ne 'Path' -and $mewKey -ne 'LiteralPath') {
                $mewForward[$mewKey] = $PSBoundParameters[$mewKey]
            }
        }
        $mewResolve = @{}
        if ($PSCmdlet.ParameterSetName -eq 'LiteralPath') { $mewResolve['LiteralPath'] = $LiteralPath }
        else { $mewResolve['Path'] = $Path }
        if ($PSBoundParameters['ErrorAction']) { $mewResolve['ErrorAction'] = $PSBoundParameters['ErrorAction'] }
        $mewResolved = @(Microsoft.PowerShell.Management\Resolve-Path @mewResolve)
        foreach ($mewItem in $mewResolved) {
            if ($mewItem.Provider.Name -ne 'FileSystem') {
                Microsoft.PowerShell.Management\Remove-Item -LiteralPath $mewItem.Path @mewForward
                continue
            }
            $mewTarget = $mewItem.ProviderPath.TrimEnd('\')
            if ($mewOriginalLocation.Provider.Name -eq 'FileSystem') {
                $mewHere = $mewOriginalLocation.ProviderPath.TrimEnd('\')
                if ($mewHere -eq $mewTarget -or $mewHere.StartsWith($mewTarget + '\', 'OrdinalIgnoreCase')) {
                    throw 'Cannot remove the current location or its ancestor.'
                }
            }
            $mewLiteral = 'Microsoft.PowerShell.Core\FileSystem::' + $mewItem.ProviderPath
            $mewPushed = $false
            try {
                Microsoft.PowerShell.Management\Push-Location -LiteralPath 'Variable:\' -ErrorAction Stop
                $mewPushed = $true
                Microsoft.PowerShell.Management\Remove-Item -LiteralPath $mewLiteral @mewForward
            }
            finally {
                if ($mewPushed) { Microsoft.PowerShell.Management\Pop-Location -ErrorAction Stop }
            }
        }
    }
};
"""
