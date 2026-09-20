# Read only the Codex document title and the Claude desktop app's open-session
# header. Never read chat messages or send input.
param([switch]$Once)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ActiveChatWindow {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = [System.Windows.Automation.AutomationElement]::RootElement
$document = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')
# The Claude desktop app labels the open session's title control
# "<title>, rename session"; sidebar rows carry status prefixes instead, so
# this one control names the session on screen and nothing else.
$button = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
function Read-ClaudeTitle($windows, $foreground) {
    $ids = @(Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { $_.Id })
    if (-not $ids.Count) { return $null }
    $found = @()
    foreach ($window in $windows) {
        if ($window.Current.ProcessId -notin $ids -or $window.Current.IsOffscreen) { continue }
        if ($window.Current.Name -ne 'Claude') { continue }
        $focused = ($window.Current.NativeWindowHandle -eq $foreground)
        foreach ($control in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $button)) {
            $name = $control.Current.Name
            if ($control.Current.IsOffscreen -or $name -notmatch ',\s*rename session$') { continue }
            $found += @{title=($name -replace ',\s*rename session$', ''); focused=$focused}
        }
    }
    $front = @($found | Where-Object { $_.focused })
    if ($front.Count -eq 1) { return $front[0].title }
    if ($found.Count -eq 1) { return $found[0].title }
    return $null
}
do {
    $title = $null
    $claude = $null
    try {
        $ids = @(Get-Process -Name ChatGPT,Codex -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -match '[\\/]OpenAI\.Codex_[^\\/]+[\\/]|[\\/]OpenAI[\\/]Codex[\\/]|[\\/]Codex[\\/]Codex\.exe$'
        } | ForEach-Object { $_.Id })
        $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition)
        $matches = @()
        $foreground = [ActiveChatWindow]::GetForegroundWindow().ToInt64()
        foreach ($window in $windows) {
            if ($window.Current.ProcessId -notin $ids -or $window.Current.IsOffscreen) { continue }
            if ($window.Current.Name -notin @('ChatGPT','Codex')) { continue }
            $doc = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $document)
            if ($null -ne $doc -and $doc.Current.Name) {
                $matches += @{title=$doc.Current.Name; focused=($window.Current.NativeWindowHandle -eq $foreground)}
            }
        }
        $focused = @($matches | Where-Object { $_.focused })
        if ($focused.Count -eq 1) { $title = $focused[0].title }
        elseif ($matches.Count -eq 1) { $title = $matches[0].title }
    } catch { $title = $null }
    try { $claude = Read-ClaudeTitle $windows $foreground } catch { $claude = $null }
    @{title=$title; claude=$claude} | ConvertTo-Json -Compress | ForEach-Object { [Console]::WriteLine($_) }
    if (!$Once) { Start-Sleep -Milliseconds 1000 }
} while (!$Once)
