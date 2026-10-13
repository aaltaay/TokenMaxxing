# Say which program is in front and how long since the last keyboard or mouse
# input, once every few seconds. The window title goes to the HUD only so it can
# tell a claude.ai tab, or a terminal running an agent, from any other; the HUD
# never keeps or forwards it. Never reads window contents or sends input.
param([int]$ParentId = 0, [int]$IntervalMs = 5000, [switch]$Once)
$ErrorActionPreference = 'Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class FocusProbe {
    [StructLayout(LayoutKind.Sequential)]
    struct LastInputInfo { public uint Size; public uint Time; }
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LastInputInfo info);
    [DllImport("kernel32.dll")] static extern ulong GetTickCount64();
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    const uint QueryLimitedInformation = 0x1000;

    // The last input is stamped with the 32-bit tick count, so the difference is
    // taken in 32 bits too; that stays right across its 49.7-day wrap.
    public static long IdleMs() {
        var info = new LastInputInfo();
        info.Size = (uint)Marshal.SizeOf(typeof(LastInputInfo));
        if (!GetLastInputInfo(ref info)) return -1;
        return unchecked((uint)GetTickCount64() - info.Time);
    }
    public static IntPtr Window() { return GetForegroundWindow(); }
    public static string Title(IntPtr window, int max) {
        var text = new StringBuilder(max + 1);
        GetWindowText(window, text, max + 1);
        return text.ToString();
    }
    // The executable's full path. Limited-information access opens elevated
    // processes too, and it is all a path needs.
    public static string ImagePath(IntPtr window) {
        uint id;
        GetWindowThreadProcessId(window, out id);
        if (id == 0) return null;
        IntPtr process = OpenProcess(QueryLimitedInformation, false, id);
        if (process == IntPtr.Zero) return null;
        try {
            var name = new StringBuilder(1024);
            uint size = (uint)name.Capacity;
            return QueryFullProcessImageName(process, 0, name, ref size) ? name.ToString() : null;
        } finally { CloseHandle(process); }
    }
}
'@
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
# Sleeping on the parent's handle ends this loop the moment the HUD exits, even
# when it crashes and never gets to kill this process.
$parent = $null
if ($ParentId -gt 0) {
    try { $parent = [System.Diagnostics.Process]::GetProcessById($ParentId) } catch { exit }
}
do {
    $exe = $null
    $path = $null
    $title = $null
    $idle = $null
    try {
        $idle = [FocusProbe]::IdleMs()
        $window = [FocusProbe]::Window()
        if ($window -ne [IntPtr]::Zero) {
            $path = [FocusProbe]::ImagePath($window)
            if ($path) { $exe = [System.IO.Path]::GetFileNameWithoutExtension($path) }
            $title = [FocusProbe]::Title($window, 200)
        }
    } catch { $exe = $null; $path = $null; $title = $null }
    # A closed pipe makes this write throw, which ends the script.
    [ordered]@{exe=$exe; path=$path; title=$title; idle_ms=$idle} | ConvertTo-Json -Compress | ForEach-Object { [Console]::WriteLine($_) }
    if ($Once) { break }
    if ($parent) { if ($parent.WaitForExit($IntervalMs)) { break } }
    else { Start-Sleep -Milliseconds $IntervalMs }
} while ($true)
